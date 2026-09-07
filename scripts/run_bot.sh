#!/usr/bin/env bash
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

# Activate venv if it exists
if [ -d ".venv" ]; then
    source ".venv/bin/activate"
elif [ -d "venv" ]; then
    source "venv/bin/activate"
fi

# Verify dependencies
if ! python3 -c "import requests" &>/dev/null; then
    echo "[!] Warning: 'requests' package not detected. Installing requirements..."
    pip3 install -r requirements.txt
fi

echo "=================================================="
echo "    Starting HorusOps Telegram Bot Daemon"
echo "=================================================="

exec python3 telegram_bot_daemon.py
