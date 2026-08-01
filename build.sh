#!/usr/bin/env bash
# Build the Carrot one-click installer on macOS or Linux.
# Output: gui/dist/Carrot-<version>.dmg (macOS) or .AppImage + .deb (Linux).
set -euo pipefail
cd "$(dirname "$0")"

echo "[1/3] Installing Python dependencies…"
python3 -m pip install -e . --quiet
python3 -m pip install pyinstaller --quiet

echo "[2/3] Building installer for $(uname -s) $(uname -m)…"
python3 scripts/build_installer.py

echo "[3/3] Done. Installer output in gui/dist/"
