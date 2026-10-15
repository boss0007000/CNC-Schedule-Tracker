#!/bin/sh
# One-command local start: venv, migrate, seed demo data, run with auto-reload.
set -e
[ -d .venv ] || (uv venv --python 3.12 .venv && uv pip install -r requirements-dev.txt)
mkdir -p var
.venv/bin/alembic upgrade head
.venv/bin/python -m scripts.seed || true
exec .venv/bin/uvicorn app.main:app --reload --port 8000
