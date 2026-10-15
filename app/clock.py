"""Single source of 'now' so tests can freeze time. All datetimes are tz-aware UTC."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import get_config

UTC = timezone.utc
_frozen: datetime | None = None


def now() -> datetime:
    return _frozen if _frozen is not None else datetime.now(UTC)


def freeze(dt: datetime | None) -> None:
    global _frozen
    if dt is not None and dt.tzinfo is None:
        raise ValueError("naive datetime")
    _frozen = dt.astimezone(UTC) if dt else None


def advance(**kw) -> datetime:
    global _frozen
    base = _frozen if _frozen is not None else datetime.now(UTC)
    _frozen = base + timedelta(**kw)
    return _frozen


def tz() -> ZoneInfo:
    return ZoneInfo(get_config().timezone)


def to_local(dt: datetime) -> datetime:
    return dt.astimezone(tz())


def local_dt(d, t) -> datetime:
    """Wall-clock local date+time -> aware UTC datetime (DST-safe via zoneinfo)."""
    return datetime.combine(d, t, tzinfo=tz()).astimezone(UTC)
