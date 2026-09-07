#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

if [ -d ".venv" ]; then
    source ".venv/bin/activate"
elif [ -d "venv" ]; then
    source "venv/bin/activate"
fi

echo "=================================================="
echo "      Running HorusOps Git Auto Sync"
echo "=================================================="

exec python3 run_auto_sync.py "$@"
