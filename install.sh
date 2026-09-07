#!/usr/bin/env bash
# HorusOps | Automated One-Step Installer & Setup Launcher
set -e

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "=================================================="
echo "      HorusOps Installation & Setup"
echo "=================================================="

# Check Python 3
if ! command -v python3 &>/dev/null; then
    echo "[!] Error: python3 is required. Please install Python 3.10+."
    exit 1
fi

echo "[*] Checking Python dependencies..."
if ! python3 -c "import requests" &>/dev/null; then
    echo "[*] Installing dependencies from requirements.txt..."
    python3 -m pip install --quiet -r requirements.txt || pip3 install -r requirements.txt
    echo "[OK] Dependencies installed successfully."
else
    echo "[OK] Dependencies are already installed."
fi

# Ensure executable permissions on all shell scripts
chmod +x scripts/*.sh 2>/dev/null || true

echo ""
echo "[*] Launching Configuration Wizard..."
exec ./scripts/setup.sh "$@"
