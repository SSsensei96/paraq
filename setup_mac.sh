#!/bin/bash
# One-time setup on macOS: Tesseract, OCR language data, Python environment.
# Safe to run again; it only installs what is missing.
set -euo pipefail
cd "$(dirname "$0")"

say() { printf "\n\033[1m%s\033[0m\n" "$*"; }

if ! command -v brew >/dev/null 2>&1; then
  for b in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [ -x "$b" ]; then eval "$("$b" shellenv)"; fi
  done
fi
if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew is required. Install it from https://brew.sh, then run this script again."
  exit 1
fi

say "Tesseract OCR"
brew list tesseract >/dev/null 2>&1 || brew install tesseract

# TESSDATA=best (default, most accurate) or TESSDATA=fast (2-3x faster, slightly less accurate)
QUALITY="${TESSDATA:-best}"
say "OCR language data: Russian, Kazakh, English, Turkish ($QUALITY)"
mkdir -p paraq/resources/tessdata
if [ -f paraq/resources/tessdata/.quality ] && [ "$(cat paraq/resources/tessdata/.quality)" != "$QUALITY" ]; then
  rm -f paraq/resources/tessdata/*.traineddata
fi
for lang in rus kaz eng tur; do
  f="paraq/resources/tessdata/$lang.traineddata"
  if [ ! -s "$f" ]; then
    echo "  downloading $lang"
    curl -L --fail --silent --show-error -o "$f.part" \
      "https://github.com/tesseract-ocr/tessdata_${QUALITY}/raw/main/$lang.traineddata"
    mv "$f.part" "$f"
  fi
done
echo "$QUALITY" > paraq/resources/tessdata/.quality

say "Python environment"
PY=""
for c in python3.13 python3.12 python3.11 python3.10; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
  brew install python@3.12
  PY="$(brew --prefix python@3.12)/bin/python3.12"
fi
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt
# Optional smarter translator:  PARAQ_AI=gemma-12b bash build_mac.sh   (or gemma-4b)
if [ -n "${PARAQ_AI:-}" ] && [ "${PARAQ_AI}" != "none" ]; then
  say "Smarter translator: TranslateGemma ($PARAQ_AI) in Ollama"
  if [ ! -d /Applications/Ollama.app ] && ! command -v ollama >/dev/null 2>&1; then
    brew install ollama
  fi
  .venv/bin/python -m paraq.translate --download "$PARAQ_AI" || echo "  Could not download now; choose it later in Tools > Translation Model."
fi
echo "Setup complete."
