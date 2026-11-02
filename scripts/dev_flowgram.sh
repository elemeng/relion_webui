#!/usr/bin/env bash
#
# Development startup for the FlowGram-based UI, with MCP on.
#
#   scripts/dev_flowgram.sh                 # agent :8765 (+ /mcp), UI :1420
#   scripts/dev_flowgram.sh -p /path/proj   # open a project on startup
#   scripts/dev_flowgram.sh --mcp-read-only # expose only the read tools
#
# The agent runs with --mcp because the assistant is external in this branch:
# zcode / opencode / Claude Code connect to http://127.0.0.1:8765/mcp. The
# canvas's "外部 agent" button shows the exact configuration and context.
#
# MCP refuses to run with --no-auth on a non-loopback address, so this script
# pins the agent to 127.0.0.1. Use dev_web.sh if you need a LAN-reachable UI.

set -euo pipefail
readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly API_PORT="${RELION_WEB_PORT:-8765}"
readonly UI_PORT="${RELION_WEB_UI_PORT:-1420}"
PROJECT=""
MCP_FLAGS=(--mcp)
while [[ $# -gt 0 ]]; do
  case "$1" in
    -p|--project) PROJECT="$2"; shift 2 ;;
    --mcp-read-only) MCP_FLAGS=(--mcp --mcp-read-only); shift ;;
    -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

export PATH="$HOME/.bun/bin:$HOME/miniforge3/bin:$PATH"
export RELION_WEB_API="http://127.0.0.1:${API_PORT}"

cleanup() { kill 0 2>/dev/null || true; }
trap cleanup EXIT INT TERM

(
  cd "$ROOT/backend"
  PYTHONPATH=. exec "$ROOT/.venv/bin/python" -m relion_webui._cli \
    --host 127.0.0.1 --port "$API_PORT" --no-auth \
    ${PROJECT:+--project "$PROJECT"} "${MCP_FLAGS[@]}"
) &

# Wait for the agent before the UI starts proxying to it.
for _ in $(seq 1 60); do
  curl -sf "http://127.0.0.1:${API_PORT}/api/health" >/dev/null 2>&1 && break
  sleep 0.5
done

cd "$ROOT/flowgram-app"
exec bun run dev
