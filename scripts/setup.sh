#!/usr/bin/env bash
set -e

# Resolve repository root
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

# Check Python version
if ! command -v python3 &>/dev/null; then
    echo "[!] Error: python3 is required but not installed. Please install Python 3.10+."
    exit 1
fi

exec python3 setup_wizard.py "$@"
