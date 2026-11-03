#!/usr/bin/env bash
#
# Run every check a change to the web UI should pass, in one command.
#
#   ./scripts/dev_test.sh              # everything that can run here
#   ./scripts/dev_test.sh --quick      # agent tests only (seconds)
#   ./scripts/dev_test.sh --fix        # regenerate the tables, then check
#   ./scripts/dev_test.sh --list       # show the checks and why each may skip
#
# The checks fall into four groups, and each skips itself - with a reason in the
# summary - when its prerequisite is absent, so the same script works in a
# standalone clone and in a RELION checkout:
#
#   tests     the agent's pytest suite; needs only the Python extras (uv)
#   generated the three extracted tables must match RELION's C++ sources;
#             needs a RELION checkout (RELION_SOURCE, default: the parent dir)
#   format    RELION's own STAR reader must accept what the agent writes and
#             vice versa; needs the checkout plus a C++ compiler
#   ui        the React app must typecheck and build; needs node/npm
#
# Exit code is 0 only when nothing failed. Skips are not failures.

set -uo pipefail

readonly WEBUI_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly BACKEND="$WEBUI_ROOT/backend"
readonly FRONTEND_DIR="$WEBUI_ROOT/flowgram-app"
readonly TESTS_DIR="$WEBUI_ROOT/tests"
readonly VENV_DIR="${RELION_WEB_VENV:-$WEBUI_ROOT/.venv}"

QUICK=0
FIX=0
LIST=0
ONLY=""

usage() {
    sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

die()  { printf '\033[31merror\033[0m %s\n' "$*" >&2; exit 2; }
note() { printf '\033[36m%s\033[0m\n' "$*"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        -h|--help)   usage 0 ;;
        --quick)     QUICK=1; shift ;;
        --fix)       FIX=1; shift ;;
        --list)      LIST=1; shift ;;
        --only)      ONLY="${2:?--only needs a group: tests|generated|format|ui}"; shift 2 ;;
        tests|generated|format|ui)
                     ONLY="$1"; shift ;;
        *)           die "unknown option: $1 (try --help)" ;;
    esac
done

# ------------------------------------------------------------------ toolchain
# Pick an interpreter that can actually run the suite. A bare `python3` may be a
# system interpreter without the extras, and reporting "pytest not installed"
# from it would look like a skip in a project where nothing is wrong.
python_has_deps() {
    "$1" -c 'import importlib.util as u, sys
sys.exit(1 if any(u.find_spec(n) is None for n in
    ("pytest", "fastapi", "numpy", "pandas", "starfile", "mrcfile")) else 0)' \
        >/dev/null 2>&1
}

pick_python() {
    local candidate
    for candidate in "${RELION_WEB_PYTHON:-}" "$VENV_DIR/bin/python" \
                     "$(command -v python3 2>/dev/null)" \
                     "$(command -v python 2>/dev/null)"; do
        [[ -n "$candidate" && -x "$candidate" ]] || continue
        if python_has_deps "$candidate"; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    # Nothing has the extras; fall back so the tests report the skip themselves,
    # which names the missing package.
    for candidate in "${RELION_WEB_PYTHON:-}" "$VENV_DIR/bin/python" \
                     "$(command -v python3 2>/dev/null)"; do
        [[ -n "$candidate" && -x "$candidate" ]] && { printf '%s' "$candidate"; return 0; }
    done
    return 1
}

PYTHON="$(pick_python)" || die "no python3 found; set RELION_WEB_PYTHON"

# The generators and the drift tests read the C++ sources from here. Same rule
# as tests/_relion_source.py: an explicit override, else a `relion` checkout
# beside this one (this package is developed next to RELION, not inside it).
RELION_SOURCE="${RELION_SOURCE:-}"
if [[ -z "$RELION_SOURCE" ]]; then
    for candidate in "$WEBUI_ROOT/../relion" "$WEBUI_ROOT/../../relion"; do
        if [[ -f "$candidate/src/pipeline_jobs.cpp" ]]; then
            RELION_SOURCE="$(cd "$candidate" && pwd)"
            break
        fi
    done
fi
export RELION_SOURCE

have_relion_source() {
    [[ -n "${RELION_SOURCE:-}" && -f "$RELION_SOURCE/src/pipeline_jobs.cpp" ]]
}

have_compiler() {
    command -v g++ >/dev/null 2>&1 || command -v c++ >/dev/null 2>&1
}

# Bun is the project's toolchain; node/npm is accepted as a fallback so a
# machine that only has Node can still run the checks. Whichever is found is
# used consistently for install and build, because the two lockfiles are not
# interchangeable.
have_node() {
    command -v bun >/dev/null 2>&1 \
        || { command -v node >/dev/null 2>&1 && command -v npm >/dev/null 2>&1; }
}

# The runner to use for package scripts.
js_runner() {
    if command -v bun >/dev/null 2>&1; then printf 'bun'; else printf 'npm'; fi
}

want() {
    [[ -z "$ONLY" || "$ONLY" == "$1" ]]
}

# ---------------------------------------------------------------- reporting
# One line per check: PASS, FAIL, or SKIP with the reason. Collected while the
# checks run and printed at the end, so a failure in the middle does not hide
# what came after it.
RESULTS=()
FAILED=0

record() {
    # record <status> <name> [detail]
    RESULTS+=("$1|$2|${3:-}")
    [[ "$1" == FAIL ]] && FAILED=1
    return 0
}

summary() {
    printf '\n'
    printf '%-6s %-22s %s\n' STATUS CHECK DETAIL
    printf '%-6s %-22s %s\n' ------ --------------------- ------------------------------
    local row status name detail colour
    for row in "${RESULTS[@]}"; do
        IFS='|' read -r status name detail <<< "$row"
        case "$status" in
            PASS) colour=$'\033[32m' ;;
            FAIL) colour=$'\033[31m' ;;
            SKIP) colour=$'\033[33m' ;;
            *)    colour='' ;;
        esac
        printf '%s%-6s\033[0m %-22s %s\n' "$colour" "$status" "$name" "$detail"
    done
    printf '\n'
    local skipped=0
    for row in "${RESULTS[@]}"; do
        [[ "$row" == SKIP* ]] && skipped=$((skipped + 1))
    done
    if (( FAILED )); then
        printf '\033[31m%s check(s) failed\033[0m\n' "$FAILED"
    elif (( skipped )); then
        printf '\033[32mno failures\033[0m (%s skipped; see reasons above)\n' "$skipped"
    else
        printf '\033[32mall checks passed\033[0m\n'
    fi
}

if (( LIST )); then
    cat <<TEXT
Checks and their prerequisites:

  tests      the agent's pytest suite
             needs: python with the [dev] extras
  generated  generated tables match RELION's C++ sources
             needs: a RELION checkout (RELION_SOURCE or ../)
  format     RELION's STAR reader accepts the agent's files and vice versa
             needs: a RELION checkout and g++
  ui         the React app typechecks and builds
             needs: bun (or node + npm as a fallback)
  ui-layout  the pipeline layout maths (ranks, loops, zoom floor)
             needs: node 22+ for --experimental-strip-types

Relationships:
  --quick   runs "tests" only
  --fix     regenerates the tables before checking them

Environment:
  RELION_WEB_PYTHON  interpreter to use   (now: $PYTHON)
  RELION_SOURCE      RELION checkout      (now: ${RELION_SOURCE:-<none>})
TEXT
    exit 0
fi

# --------------------------------------------------------------------- tests
if want tests; then
    note "== agent tests"
    if ! "$PYTHON" -c 'import pytest' >/dev/null 2>&1; then
        record SKIP "tests" "pytest not installed (uv pip install -e '.[dev]')"
    else
        # PYTHONPATH rather than requiring an editable install, so a fresh clone
        # can run the suite before installing anything.
        if ( cd "$WEBUI_ROOT" && PYTHONPATH="$BACKEND" \
                "$PYTHON" -m pytest "$TESTS_DIR" -q -p no:cacheprovider ); then
            record PASS "tests"
        else
            record FAIL "tests" "see output above"
        fi
    fi
fi

# ----------------------------------------------------------------- generated
if want generated && (( ! QUICK )); then
    note "== generated tables"
    if ! have_relion_source; then
        record SKIP "generated" "no RELION checkout (set RELION_SOURCE)"
    elif (( FIX )); then
        # Regenerate first; the drift test below would otherwise fail on a
        # change that is supposed to update them.
        status=0
        for script in generate_web_job_schema generate_web_cli_options generate_web_job_commands; do
            "$PYTHON" "$WEBUI_ROOT/scripts/$script.py" >/dev/null || status=1
        done
        if (( status )); then
            record FAIL "generated" "regeneration failed"
        else
            record PASS "generated" "regenerated"
        fi
    else
        status=0
        detail=""
        for script in generate_web_job_schema generate_web_cli_options generate_web_job_commands; do
            if ! out=$("$PYTHON" "$WEBUI_ROOT/scripts/$script.py" --check 2>&1); then
                status=1
                detail="${detail:+$detail; }$script out of date"
                printf '%s\n' "$out" >&2
            fi
        done
        if (( status )); then
            record FAIL "generated" "$detail -- run: $0 --fix"
        else
            record PASS "generated" "three tables in sync"
        fi
    fi
fi

# -------------------------------------------------------------------- format
if want format && (( ! QUICK )); then
    note "== format compatibility"
    if ! have_relion_source; then
        record SKIP "format" "no RELION checkout (set RELION_SOURCE)"
    elif ! have_compiler; then
        record SKIP "format" "no C++ compiler"
    else
        if ( cd "$WEBUI_ROOT" && PYTHON="$PYTHON" bash scripts/relion_compat_check/run.sh ); then
            record PASS "format" "both directions"
        else
            record FAIL "format" "see output above"
        fi
    fi
fi

# ------------------------------------------------------------------------ ui
if want ui && (( ! QUICK )); then
    note "== front end"
    if ! have_node; then
        record SKIP "ui" "bun (or node+npm) not on PATH"
    elif [[ ! -d "$FRONTEND_DIR/node_modules" ]]; then
        record SKIP "ui" "run 'bun install' in flowgram-app/ first"
    else
        RUNNER="$(js_runner)"
        # `run build` is `tsc -b && vite build`, so this typechecks and
        # produces the bundle the agent serves. The output is compared below:
        # a UI change that was never rebuilt would ship a stale bundle.
        if ( cd "$FRONTEND_DIR" && "$RUNNER" run build >/tmp/relion_webui-build.log 2>&1 ); then
            record PASS "ui" "typechecked and built"
        else
            tail -30 /tmp/relion_webui-build.log >&2
            record FAIL "ui" "see output above"
        fi

        # The built bundle is committed; if the build changed it, the change was
        # not committed either.
        if ( cd "$WEBUI_ROOT" && git diff --quiet -- backend/relion_webui/static ); then
            record PASS "ui-bundle" "committed bundle is current"
        else
            record FAIL "ui-bundle" "build output differs from the committed bundle"
        fi

        # Layout maths lives in Python now (flowgram/layout.py); the canvas
        # only consumes its output. A wrong column rank does not throw - it
        # silently produces an unreadable graph, which is what these assert.
        if ( cd "$WEBUI_ROOT" && PYTHONPATH=backend "$PYTHON" -m pytest \
                tests/test_flowgram_bridge.py -q >/tmp/relion_webui-layout.log 2>&1 ); then
            record PASS "ui-layout" "$(tail -1 /tmp/relion_webui-layout.log)"
        else
            tail -20 /tmp/relion_webui-layout.log >&2
            record FAIL "ui-layout" "see output above"
        fi
    fi
fi

summary
exit "$FAILED"
