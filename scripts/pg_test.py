"""Run the whole test suite against a REAL Postgres (embedded via the `pgserver` pip package).

    uv pip install pgserver && python -m scripts.pg_test
"""
import os
import subprocess
import sys
import tempfile

import pgserver


def main() -> int:
    d = tempfile.mkdtemp(prefix="pg-shop-")
    srv = pgserver.get_server(d, cleanup_mode="stop")
    uri = srv.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    import psycopg
    with psycopg.connect(srv.get_uri()) as c:
        print("Postgres:", c.execute("select version()").fetchone()[0][:60])
    env = {**os.environ, "TEST_DATABASE_URL": uri}
    try:
        return subprocess.call([sys.executable, "-m", "pytest", "-q", *sys.argv[1:]], env=env)
    finally:
        srv.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
