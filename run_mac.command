#!/bin/bash
# Run Paraq straight from the source folder (no app bundle needed).
cd "$(dirname "$0")"
bash setup_mac.sh && exec .venv/bin/python run_paraq.py "$@"
