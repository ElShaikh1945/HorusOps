#!/usr/bin/env bash
set -e

# Resolve repository root
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

echo "=================================================="
echo "      WAISoft-Reports Setup Wizard"
echo "=================================================="

# Check Python version
if ! command -v python3 &>/dev/null; then
    echo "[!] Error: python3 is required but not installed."
    exit 1
fi

PORT="${SETUP_PORT:-8585}"
URL="http://localhost:${PORT}"

echo "[*] Starting Setup Wizard on ${URL}..."
echo "[*] Open your browser at ${URL} to configure your system."
echo "[*] Press Ctrl+C to stop the wizard."

# Attempt to open browser in background
if command -v open &>/dev/null; then
    (sleep 1 && open "$URL") &
elif command -v xdg-open &>/dev/null; then
    (sleep 1 && xdg-open "$URL") &
fi

exec python3 setup_wizard.py --port "$PORT"
