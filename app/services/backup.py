"""Database backups: gzip'd JSON dump of every table (portable between SQLite and Postgres).

Destinations (all optional, all free):
  * BACKUP_DIR            a folder (mount a persistent disk / sync it with rclone to Google Drive)
  * BACKUP_PUT_URL        any HTTP PUT / WebDAV target (Nextcloud, Box WebDAV, a free object-storage presigned URL ...)
"""
import base64
import gzip
import json
import logging
from datetime import date, datetime, time
from pathlib import Path

import httpx
from sqlalchemy import LargeBinary, insert, select
from sqlalchemy import Date as SADate
from sqlalchemy import DateTime as SADateTime
from sqlalchemy import Time as SATime

from app import clock, db as dbm
from app.config import get_config
from app.models import Base, UTCDateTime

log = logging.getLogger("backup")
KEEP = 14
SKIP_TABLES = {"rate_events", "idempotency_keys"}


def _ser(v):
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (date, time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return {"__b64__": base64.b64encode(bytes(v)).decode()}
    return v


def dump(session) -> dict:
    data = {"version": 1, "created_at": clock.now().isoformat(), "tables": {}}
    for t in Base.metadata.sorted_tables:
        if t.name in SKIP_TABLES:
            continue
        rows = []
        for r in session.execute(select(t)).mappings():
            rows.append({k: _ser(v) for k, v in r.items()})
        data["tables"][t.name] = rows
    return data


def _de(col, v):
    if v is None:
        return None
    if isinstance(col.type, (UTCDateTime, SADateTime)):
        return datetime.fromisoformat(v)
    if isinstance(col.type, SADate):
        return date.fromisoformat(v)
    if isinstance(col.type, SATime):
        return time.fromisoformat(v)
    if isinstance(col.type, LargeBinary) and isinstance(v, dict):
        return base64.b64decode(v["__b64__"])
    return v


def restore(session, data: dict) -> dict:
    """Load a dump into an EMPTY database (run `alembic upgrade head` first)."""
    counts = {}
    for t in Base.metadata.sorted_tables:
        rows = data["tables"].get(t.name)
        if not rows:
            continue
        cols = {c.name: c for c in t.columns}
        session.execute(insert(t), [{k: _de(cols[k], v) for k, v in r.items() if k in cols} for r in rows])
        counts[t.name] = len(rows)
    session.flush()
    return counts


def backup_bytes() -> bytes:
    with dbm.session_scope() as s:
        return gzip.compress(json.dumps(dump(s)).encode(), compresslevel=6)


def run_backup() -> dict:
    cfg = get_config()
    blob = backup_bytes()
    name = f"backup-{clock.now().strftime('%Y%m%d-%H%M%S')}.json.gz"
    result = {"name": name, "bytes": len(blob), "local": None, "remote": None}
    d = Path(cfg.backup_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(blob)
    result["local"] = str(d / name)
    for old in sorted(d.glob("backup-*.json.gz"))[:-KEEP]:
        old.unlink(missing_ok=True)
    if cfg.backup_put_url:
        auth = tuple(cfg.backup_put_auth.split(":", 1)) if ":" in cfg.backup_put_auth else None
        try:
            r = httpx.put(cfg.backup_put_url.rstrip("/") + "/" + name, content=blob, auth=auth, timeout=60)
            result["remote"] = r.status_code
            if r.status_code >= 300:
                log.error("backup upload failed: %s", r.status_code)
        except httpx.HTTPError as e:
            result["remote"] = f"error: {e}"
            log.exception("backup upload failed")
    log.info("backup complete %s", result)
    return result


def list_backups() -> list[dict]:
    d = Path(get_config().backup_dir)
    if not d.exists():
        return []
    return [{"name": p.name, "bytes": p.stat().st_size,
             "when": datetime.fromtimestamp(p.stat().st_mtime, tz=clock.UTC)}
            for p in sorted(d.glob("backup-*.json.gz"), reverse=True)]


def backup_path(name: str) -> Path | None:
    d = Path(get_config().backup_dir).resolve()
    p = (d / name).resolve()
    if p.parent != d or not p.name.startswith("backup-") or not p.exists():
        return None
    return p
