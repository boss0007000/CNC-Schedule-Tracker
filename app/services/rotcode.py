"""Rotating 4-digit shop code proving physical presence. Codes live server-side in the DB."""
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import clock, security
from app.errors import UserError
from app.models import RotatingCode
from app.services import settings as shop_settings


def _interval(cfg) -> timedelta:
    return timedelta(minutes=max(1, int(cfg.rotating_code_interval_min)))


def _bucket_start(now: datetime, interval: timedelta) -> datetime:
    secs = int(interval.total_seconds())
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(epoch - epoch % secs, tz=clock.UTC)


def _ensure(db: Session, start: datetime, interval: timedelta) -> RotatingCode:
    row = db.scalar(select(RotatingCode).where(RotatingCode.starts_at == start))
    if row:
        return row
    prev = db.scalar(select(RotatingCode).where(RotatingCode.starts_at < start)
                     .order_by(RotatingCode.starts_at.desc()).limit(1))
    code = f"{secrets.randbelow(10000):04d}"
    while prev and code == prev.code:
        code = f"{secrets.randbelow(10000):04d}"
    row = RotatingCode(code=code, starts_at=start, ends_at=start + interval)
    try:
        with db.begin_nested():  # two requests may race to create the new slot's code: the loser re-reads
            db.add(row)
            db.flush()
    except IntegrityError:
        db.expire_all()
        return db.scalar(select(RotatingCode).where(RotatingCode.starts_at == start))
    return row


def current(db: Session, now: datetime | None = None) -> dict:
    """The code to display on the TV, plus seconds until it rotates."""
    now = now or clock.now()
    cfg = shop_settings.load(db)
    iv = _interval(cfg)
    row = _ensure(db, _bucket_start(now, iv), iv)
    return {"code": row.code, "expires_at": row.ends_at, "seconds_left": int((row.ends_at - now).total_seconds())}


def valid_codes(db: Session, now: datetime | None = None) -> set[str]:
    """Current code, plus the previous one for `overlap` minutes after rotation."""
    now = now or clock.now()
    cfg = shop_settings.load(db)
    iv = _interval(cfg)
    cur = _ensure(db, _bucket_start(now, iv), iv)
    codes = {cur.code}
    overlap = timedelta(minutes=cfg.rotating_code_overlap_min)
    for r in db.scalars(select(RotatingCode).where(RotatingCode.ends_at <= now,
                                                   RotatingCode.ends_at > now - overlap)):
        codes.add(r.code)
    return codes


def verify(db: Session, submitted: str, ip: str, ref: str, now: datetime | None = None) -> None:
    """Raise UserError unless `submitted` is a currently-valid shop code. Rate-limited per IP and
    per reference; only *failed* guesses count against the limits, so honest retries aren't punished."""
    ip_key, ref_key = f"codefail:ip:{ip}", f"codefail:ref:{ref}"
    if security.rate_count(db, ip_key, 600) >= 10 or security.rate_count(db, ref_key, 600) >= 5:
        raise UserError("Too many wrong shop codes. Wait 10 minutes or ask a staff member to check you in.",
                        code="rate_limited", persist=True)
    submitted = (submitted or "").strip()
    if submitted not in valid_codes(db, now):
        security.rate_hit(db, ip_key)
        security.rate_hit(db, ref_key)
        raise UserError("That shop code is wrong or has expired. Look at the TV in the shop for the current code.",
                        code="bad_shop_code", field="shop_code", persist=True)
