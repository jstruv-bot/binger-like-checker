#!/bin/bash
# Binger Like Checker - Quick launcher for macOS/Linux
# Installs dependencies and runs the app

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "Binger Like Checker"
echo "==================="

# Check Python
if command -v python3 &>/dev/null; then
    PY=python3
elif command -v python &>/dev/null; then
    PY=python
else
    echo "Error: Python 3 is not installed."
    echo "  macOS:  brew install python"
    echo "  Linux:  sudo apt install python3 python3-tk"
    exit 1
fi

echo "Using: $($PY --version)"

# Install dependencies
echo "Checking dependencies..."
$PY -m pip install --quiet --upgrade requests 2>/dev/null || \
    $PY -m pip install --quiet --upgrade --user requests

# Run
echo "Launching..."
$PY like_checker.py
