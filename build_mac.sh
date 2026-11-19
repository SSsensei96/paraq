#!/bin/bash
# Build dist/Paraq.app on macOS.   Usage:  bash build_mac.sh
set -euo pipefail
cd "$(dirname "$0")"

bash setup_mac.sh
.venv/bin/python -m pip install --quiet pyinstaller

printf "\n\033[1m%s\033[0m\n" "App icon"
ICONSET=packaging/Paraq.iconset
mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s paraq/resources/icon.png --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  d=$((s * 2))
  sips -z $d $d paraq/resources/icon.png --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o packaging/Paraq.icns

printf "\n\033[1m%s\033[0m\n" "Building Paraq.app (takes a few minutes)"
.venv/bin/python -m PyInstaller --noconfirm --log-level WARN Paraq.spec

printf "\n\033[1m%s\033[0m\n" "Checking the app"
dist/Paraq.app/Contents/MacOS/Paraq --selftest || true
echo
echo "Finished: $(pwd)/dist/Paraq.app"
echo "Drag it into Applications. Homebrew's Tesseract must stay installed on this Mac."
open dist
