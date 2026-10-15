from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import get_config

_engine: Engine | None = None
_Session: sessionmaker | None = None


def configure(url: str | None = None) -> Engine:
    global _engine, _Session
    url = url or get_config().sqlalchemy_url
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite") and ":memory:" not in url and url != "sqlite://":
        from pathlib import Path
        Path(url.split("sqlite:///", 1)[-1]).resolve().parent.mkdir(parents=True, exist_ok=True)
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
    if _engine is not None:
        _engine.dispose()
    _engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(_engine, "connect")
        def _fk(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    _Session = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def engine() -> Engine:
    if _engine is None:
        configure()
    return _engine  # type: ignore[return-value]


def SessionLocal() -> Session:
    if _Session is None:
        configure()
    return _Session()  # type: ignore[misc]


@contextmanager
def session_scope():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def get_db():
    s = SessionLocal()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()
