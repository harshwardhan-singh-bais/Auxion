#!/usr/bin/env bash
# Start the Auxion merchant backend + dashboard.
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python -m venv .venv
  ./.venv/Scripts/python.exe -m pip install --upgrade pip 2>/dev/null || ./.venv/bin/pip install --upgrade pip
  ./.venv/Scripts/python.exe -m pip install -r requirements.txt 2>/dev/null || ./.venv/bin/pip install -r requirements.txt
fi
PY=./.venv/Scripts/python.exe
[ -x "$PY" ] || PY=./.venv/bin/python
"$PY" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
