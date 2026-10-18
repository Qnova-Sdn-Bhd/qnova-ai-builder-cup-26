#!/bin/sh
set -eu
case "${1:-api}" in
  api) module=handoff_guardian.api:app ;;
  worker) module=handoff_guardian.worker:app ;;
  *) echo "Expected api or worker entry point" >&2; exit 64 ;;
esac
exec /app/.venv/bin/uvicorn "$module" --host 0.0.0.0 --port "${PORT:-8080}"
