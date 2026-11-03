#!/usr/bin/env bash
#
# Development startup for the FlowGram canvas.
#
#   scripts/dev_flowgram.sh                 # agent :8765, Vite :1420 (hot reload)
#   scripts/dev_flowgram.sh -p /path/proj   # open a project on startup
#   scripts/dev_flowgram.sh --build         # production-style: agent only, no Vite
#
# The agent serves the canvas at http://127.0.0.1:8765/ from a built bundle
# (backend/relion_webui/static) and answers its API on the same `/api` it uses
# for everything else. Vite in development proxies /api and /mcp so edits
# hot-reload, but nothing about the app needs Vite: one Python process is a
# complete deployment.

set -euo pipefail
readonly ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly API_PORT="${RELION_WEB_PORT:-8765}"
readonly UI_PORT="${RELION_WEB_UI_PORT:-1420}"
PROJECT=""
MCP_FLAGS=(--mcp)
DEV=1
while [[ $# -gt 0 ]]; do
  case "$1" in
    -p|--project) PROJECT="$2"; shift 2 ;;
    --build) DEV=0; shift ;;
    --mcp-read-only) MCP_FLAGS=(--mcp --mcp-read-only); shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

export PATH="$HOME/.bun/bin:$HOME/miniforge3/bin:$PATH"
export RELION_WEB_API="http://127.0.0.1:${API_PORT}"

cleanup() { kill 0 2>/dev/null || true; }
trap cleanup EXIT INT TERM

if [[ "$DEV" -eq 0 ]]; then
  echo "building the canvas into backend/relion_webui/static ..."
  (cd "$ROOT/flowgram-app" && bun run build)
fi

(
  cd "$ROOT/backend"
  PYTHONPATH=. exec "$ROOT/.venv/bin/python" -m relion_webui._cli \
    --host 127.0.0.1 --port "$API_PORT" --no-auth \
    ${PROJECT:+--project "$PROJECT"} "${MCP_FLAGS[@]}"
) &

for _ in $(seq 1 60); do
  curl -sf "http://127.0.0.1:${API_PORT}/api/health" >/dev/null 2>&1 && break
  sleep 0.5
done

if [[ "$DEV" -eq 0 ]]; then
  echo "canvas: http://127.0.0.1:${API_PORT}/"
  echo "MCP:    http://127.0.0.1:${API_PORT}/mcp"
  wait
else
  cd "$ROOT/flowgram-app"
  exec bun run dev
fi
