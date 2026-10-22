#!/bin/bash
# Lance le bot (UI + watcher). Première fois : ./run.sh install
cd "$(dirname "$0")"
if [ "$1" = "install" ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
  .venv/bin/playwright install chromium
  [ -f .env ] || cp .env.example .env
  echo "OK. Remplis .env (open -e .env) puis ./run.sh"
  exit 0
fi
exec .venv/bin/python app.py
