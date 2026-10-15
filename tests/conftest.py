import os

os.environ.setdefault("ENV", "test")
os.environ["EMAIL_PROVIDER"] = "memory"
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["TIMEZONE"] = "America/Los_Angeles"
import tempfile  # noqa: E402

os.environ["UPLOAD_DIR"] = tempfile.mkdtemp(prefix="uploads-")
os.environ["STAFF_NOTIFY_EMAIL"] = "shop@csuchico.edu"

from datetime import datetime, timezone  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import clock, db as dbm, emailer  # noqa: E402
from app.models import Base  # noqa: E402

# Wednesday 2026-03-04 10:00 Pacific (PST, UTC-8) = 18:00 UTC
T0 = datetime(2026, 3, 4, 18, 0, tzinfo=timezone.utc)


PG_URL = os.environ.get("TEST_DATABASE_URL")  # run the same suite against real Postgres: python -m scripts.pg_test


@pytest.fixture(autouse=True)
def fresh_db():
    eng = dbm.configure(PG_URL or "sqlite://")
    if PG_URL:
        Base.metadata.drop_all(eng)
    Base.metadata.create_all(eng)
    clock.freeze(T0)
    emailer.SENT.clear()
    from app.services import lifecycle
    lifecycle.reset_throttle()
    yield
    clock.freeze(None)


@pytest.fixture
def db():
    s = dbm.SessionLocal()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def client():
    from app.main import create_app
    app = create_app()
    with TestClient(app, base_url="http://testserver") as c:
        yield c


@pytest.fixture
def demo(db):
    from scripts.seed import seed_demo
    info = seed_demo(db)
    db.commit()
    return info
