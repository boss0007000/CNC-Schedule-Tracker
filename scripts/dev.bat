@echo off
REM Windows one-command local start (cmd.exe):  scripts\dev.bat
cd /d "%~dp0\.."
if not exist .venv (
  py -3.12 -m venv .venv || exit /b 1
  .venv\Scripts\python -m pip install -r requirements-dev.txt || exit /b 1
)
if not exist var mkdir var
.venv\Scripts\python -m alembic upgrade head || exit /b 1
.venv\Scripts\python -m scripts.seed
.venv\Scripts\python -m uvicorn app.main:app --reload --port 8000
