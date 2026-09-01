#!/usr/bin/env bash
# Starts the service.
#
# Listens on $PORT (default 8080) and reads the upstream base URL from
# $FX_UPSTREAM_BASE. The default for that lives in app/config.py; no upstream
# host appears anywhere in this script.
set -euo pipefail
cd "$(dirname "$0")"

. ./bootstrap.sh

# exec so that signals reach uvicorn directly and Ctrl-C shuts down cleanly.
exec "$VENV_BIN/python" -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}"
