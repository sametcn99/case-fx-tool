#!/usr/bin/env bash
# Runs the tests. They need no network: the upstream is faked in-process.
#
# FX_UPSTREAM_BASE is deliberately not set here, so that whatever the reviewer
# points it at — including a closed port — is what the suite runs against.
set -euo pipefail
cd "$(dirname "$0")"

. ./bootstrap.sh

exec "$VENV_BIN/python" -m pytest -q
