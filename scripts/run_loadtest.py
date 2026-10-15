"""Spin up embedded Postgres, migrate, seed, start the app (1 worker), run the load test, print results."""
import os
import subprocess
import sys
import tempfile
import time

import httpx
import pgserver

srv = pgserver.get_server(tempfile.mkdtemp(prefix="pg-load-"), cleanup_mode="stop")
uri = srv.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
env = {**os.environ, "DATABASE_URL": uri, "SECRET_KEY": "load-test-secret-key-0123456789abcdef", "ENV": "dev",
       "EMAIL_PROVIDER": "memory", "BASE_URL": "http://127.0.0.1:8765", "SCHEDULER_ENABLED": "true"}
users = sys.argv[1] if len(sys.argv) > 1 else "50"
try:
    subprocess.check_call([sys.executable, "-m", "alembic", "upgrade", "head"], env=env)
    subprocess.check_call([sys.executable, "-m", "scripts.seed"], env=env)
    server = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", "8765", "--log-level", "warning"], env=env)
    try:
        for _ in range(60):
            try:
                if httpx.get("http://127.0.0.1:8765/healthz").status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.5)
        rc = subprocess.call([sys.executable, "-m", "scripts.loadtest", "--base", "http://127.0.0.1:8765", "--users", users,
                              "--seconds", "30", "--prepare"], env=env)
    finally:
        server.terminate()
        server.wait(10)
    print("exit", rc)
finally:
    srv.cleanup()
