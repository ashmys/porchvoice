#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1

if [ ! -x ".venv/bin/python" ]; then
  echo "[ERROR] Setup has not run yet."
  echo "Run ./setup.sh first - it creates .venv, downloads the LiveKit server,"
  echo "and fetches your Whisper model and Piper voice."
  exit 1
fi

if [ ! -f "python/server.py" ]; then
  echo "[ERROR] python/server.py is missing - re-clone / re-extract the kit completely."
  exit 1
fi

exec .venv/bin/python -u python/server.py
