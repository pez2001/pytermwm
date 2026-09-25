#!/bin/sh
# End-to-end smoke test (real daemon, CLI, HTTP API, MCP).  The logic lives in smoke.py so it also runs on Windows.
here=$(cd "$(dirname "$0")" && pwd)
exec "${PYTHON:-python3}" "$here/smoke.py" "$@"
