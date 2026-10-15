# Windows one-command local start (PowerShell):  .\scripts\dev.ps1
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)
if (-not (Test-Path .venv)) {
    py -3.12 -m venv .venv
    .\.venv\Scripts\python -m pip install -r requirements-dev.txt
}
New-Item -ItemType Directory -Force var | Out-Null
.\.venv\Scripts\python -m alembic upgrade head
.\.venv\Scripts\python -m scripts.seed
.\.venv\Scripts\python -m uvicorn app.main:app --reload --port 8000
