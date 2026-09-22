#!/usr/bin/env bash
#
# run.sh - Launch the BusyPanel web UI
#
# Usage:
#   ./run.sh                  # Open the web UI (http://127.0.0.1:8090)
#   ./run.sh web --port 8090  # Web UI on another port
#   ./run.sh passwd           # Set the password that gates the UI
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ ! -d ".venv" ]]; then
    echo "Virtual environment not found. Run setup first:"
    echo "  uv sync --extra dev"
    exit 1
fi

source .venv/bin/activate

if [[ $# -eq 0 ]]; then
    busypanel web
else
    busypanel "$@"
fi
