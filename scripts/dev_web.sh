#!/usr/bin/env bash
#
# One-command development startup for the RELION cryo-ET web agent.
#
# Starts two things and keeps them together:
#
#   * the FastAPI agent, imported straight from backend/ (no install needed),
#     with uvicorn's reloader watching the Python sources;
#   * the Vite dev server, which serves the React UI with hot reload and proxies
#     /api and /ws to the agent.
#
# So editing a .py restarts the agent and editing a .tsx refreshes the browser,
# with no manual rebuild of the committed bundle under
# backend/relion_webui/static.
#
# Nothing here needs RELION to be compiled: the agent is pure Python and the
# only RELION binary it may call is relion_pipeliner, for the clean/scheduler
# actions, which degrade with a clear message when it is absent.
#
# Usage:
#   scripts/dev_web.sh                        # 127.0.0.1:8765 + vite on 5173
#   scripts/dev_web.sh -p /path/to/project    # open a project on startup
#   scripts/dev_web.sh --api-only             # agent only, no Vite
#   scripts/dev_web.sh --ui-only              # Vite only, agent already running
#   scripts/dev_web.sh --auth                 # require the sshd login
#   scripts/dev_web.sh --install              # uv pip install the dev extras first
#   scripts/dev_web.sh --host 0.0.0.0         # reachable from the LAN
#
# Any option not recognised here is passed to the agent, so `--root`,
# `--project`, `--poll-interval` and friends work as usual. The agent always
# runs with --reload; the Vite side hot-reloads on its own.

set -euo pipefail

# This script lives in webui/scripts/, so the webui project root is one level
# up and the RELION checkout (when there is one) is one level above that.
readonly WEBUI_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly FRONTEND_DIR="$WEBUI_ROOT/frontend"
readonly SOURCE_DIR="$WEBUI_ROOT/backend"
readonly VENV_DIR="${RELION_WEB_VENV:-$WEBUI_ROOT/.venv}"

# 127.0.0.1 by default: --no-auth is refused on any other address, and this
# script disables auth unless --auth is given.
HOST="127.0.0.1"
API_PORT="${RELION_WEB_PORT:-8765}"
UI_PORT="${RELION_WEB_UI_PORT:-5173}"
RUN_API=1
RUN_UI=1
DO_LOGIN=0
DO_INSTALL=0

# Everything not consumed here is forwarded to the agent.
API_ARGS=()

usage() {
    sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

die() {
    printf '\033[31merror\033[0m %s\n' "$*" >&2
    exit 1
}

note() {
    printf '\033[36m%s\033[0m\n' "$*"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)            usage 0 ;;
        --api-only)           RUN_UI=0; shift ;;
        --ui-only)            RUN_API=0; shift ;;
        --auth)               DO_LOGIN=1; shift ;;
        --install)            DO_INSTALL=1; shift ;;
        --host)               HOST="${2:?--host needs a value}"; shift 2 ;;
        --api-port)           API_PORT="${2:?--api-port needs a value}"; shift 2 ;;
        --ui-port)            UI_PORT="${2:?--ui-port needs a value}"; shift 2 ;;
        # The agent has its own --host/--port; these are the script's, so keep
        # them from being forwarded twice.
        -p|--project|--root|--poll-interval|--config-directory|--session-hours)
            API_ARGS+=("$1" "${2:?$1 needs a value}")
            shift 2
            ;;
        *)                    API_ARGS+=("$1"); shift ;;
    esac
done

# ------------------------------------------------------------------ toolchain
# Bun runs the toolchain and also hosts the `bun install` that reads bun.lock.
command -v bun >/dev/null 2>&1 || NODE_MISSING=1

# uv is the Python toolchain: it creates the virtualenv, installs into it, and
# resolves from pyproject.toml. It is much faster than pip and, more usefully
# here, it rewrites nothing outside the virtualenv it is pointed at.
UV="$(command -v uv 2>/dev/null || true)"
if [[ -z "$UV" ]]; then
    die "uv is not on PATH; install it with 'curl -LsSf https://astral.sh/uv/install.sh | sh'"
fi

PYTHON="${RELION_WEB_PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
    if [[ -x "$VENV_DIR/bin/python" ]]; then
        PYTHON="$VENV_DIR/bin/python"
    elif command -v python3 >/dev/null 2>&1; then
        PYTHON="$(command -v python3)"
    else
        die "no python3 on PATH; set RELION_WEB_PYTHON"
    fi
fi

have_python_deps() {
    "$PYTHON" - <<'PY' >/dev/null 2>&1
import importlib.util, sys
needed = ('fastapi', 'uvicorn', 'numpy', 'pandas', 'starfile', 'mrcfile', 'typer')
missing = [name for name in needed if importlib.util.find_spec(name) is None]
sys.exit(1 if missing else 0)
PY
}

if (( DO_INSTALL )) || ! have_python_deps; then
    note "installing the web extras into $PYTHON with uv"
    # `-e` installs this project plus its `dev` extra. The full dependency list
    # in pyproject.toml also pulls the napari/imod toolchains, which the agent
    # does not import, so the [dev] extra is the smaller set.
    "$UV" pip install -e "$WEBUI_ROOT[dev]" --python "$PYTHON" \
        || die "could not install; run: uv pip install -e '$WEBUI_ROOT[dev]' --python $PYTHON"
fi

if (( RUN_UI )); then
    [[ -z "${NODE_MISSING:-}" ]] || die "the UI needs bun on PATH (or use --api-only)"
    if [[ ! -d "$FRONTEND_DIR/node_modules" ]]; then
        note "installing frontend dependencies (first run)"
        # --frozen-lockfile so a first run cannot silently drift from bun.lock.
        (cd "$FRONTEND_DIR" && bun install --frozen-lockfile) \
            || die "bun install failed in $FRONTEND_DIR"
    fi
fi

# --------------------------------------------------------------------- cleanup
# Each child gets its own process group (via setsid), so stopping it means
# killing the group rather than one PID. That matters because both servers
# spawn grandchildren: uvicorn --reload keeps a supervisor plus the server, and
# `bun run dev` runs vite as its own child. Killing only the PID we recorded
# left the grandchild holding the port.
PIDS=()
cleanup() {
    trap - EXIT INT TERM
    for pid in "${PIDS[@]:-}"; do
        [[ -n "$pid" ]] || continue
        # Negative PID: signal the whole process group.
        kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
    done
    # Give them a moment to shut down, then insist. A reloader that ignores
    # TERM would otherwise keep the port.
    for _ in 1 2 3 4 5 6; do
        local alive=0
        for pid in "${PIDS[@]:-}"; do
            [[ -n "$pid" ]] || continue
            kill -0 -- "-$pid" 2>/dev/null && alive=1
        done
        (( alive )) || break
        sleep 0.5
    done
    for pid in "${PIDS[@]:-}"; do
        [[ -n "$pid" ]] || continue
        kill -KILL -- "-$pid" 2>/dev/null || true
        wait "$pid" 2>/dev/null || true
    done
}
trap cleanup EXIT INT TERM

# Start a command in its own process group and record its PID. `setsid` makes
# the child a group leader, so `kill -- -PID` reaches everything it starts.
spawn() {
    ( exec setsid "$@" ) &
    PIDS+=($!)
}

# ---------------------------------------------------------------------- launch
export PYTHONPATH="$SOURCE_DIR${PYTHONPATH:+:$PYTHONPATH}"

AUTH_FLAG=(--no-auth)
(( DO_LOGIN )) && AUTH_FLAG=()

# Both --project values become the agent's roots too, so the browser can browse
# the project it was started with without a separate --root.
# Typer promotes a single command to the root, so there is no "serve" here.
AGENT_CMD=(
    "$PYTHON" -m relion_webui._cli
    --host "$HOST" --port "$API_PORT"
    --poll-interval 0.5
    --reload
    "${AUTH_FLAG[@]}"
    "${API_ARGS[@]}"
)

if (( RUN_API )); then
    # A directory the browser may look at even before a project is added, so a
    # fresh checkout is usable immediately.
    if [[ ${#API_ARGS[@]} -eq 0 ]]; then
        AGENT_CMD+=(--root "$WEBUI_ROOT")
    fi
    note "agent  -> http://$HOST:$API_PORT/  (python: $PYTHON)"
    spawn "${AGENT_CMD[@]}"
fi

if (( RUN_UI )); then
    # Vite proxies to the agent's port; keep them in step when --api-port moves.
    VITE_ARGS=(--port "$UI_PORT" --strictPort)
    note "ui     -> http://127.0.0.1:$UI_PORT/  (hot reload; proxy -> $API_PORT)"
    # vite.config.ts reads RELION_WEB_API to point its /api and /ws proxy at the
    # agent. `env -C` would need coreutils 8.28+, so the cd happens in a
    # subshell that execs into the group leader instead.
    (
        cd "$FRONTEND_DIR"
        export RELION_WEB_API="http://127.0.0.1:$API_PORT"
        exec setsid bun run dev -- "${VITE_ARGS[@]}"
    ) &
    PIDS+=($!)
fi

(( ${#PIDS[@]} > 0 )) || die "nothing to start; drop --api-only/--ui-only"

# Wait for the first child to exit, then clean up the rest. `wait -n` needs bash
# 4.3, which is why the version is checked rather than assumed.
if (( BASH_VERSINFO[0] > 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] >= 3) )); then
    wait -n "${PIDS[@]}" || true
else
    wait "${PIDS[0]}" || true
fi
