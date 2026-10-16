"""Shop hours, holidays, supervisor shifts and slot grids. Local-time rules, UTC storage, DST-safe."""
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.models import ShiftSchedule, ShopHours, StaffUser


def hours_for_date(db: Session, d: date, location_id: int = 1) -> tuple[datetime, datetime] | None:
    """(open_utc, close_utc) for a local calendar date, or None if closed.
    A dated exception row wins over the weekly pattern."""
    exc = db.scalar(select(ShopHours).where(ShopHours.location_id == location_id, ShopHours.on_date == d))
    row = exc or db.scalar(select(ShopHours).where(ShopHours.location_id == location_id,
                                                   ShopHours.weekday == d.weekday()))
    if row is None or row.closed or row.open_time is None or row.close_time is None:
        return None
    start, end = clock.local_dt(d, row.open_time), clock.local_dt(d, row.close_time)
    return (start, end) if end > start else None


def is_open(db: Session, at: datetime, location_id: int = 1) -> bool:
    w = hours_for_date(db, clock.to_local(at).date(), location_id)
    return bool(w and w[0] <= at < w[1])


def within_hours(db: Session, start: datetime, end: datetime, location_id: int = 1) -> bool:
    w = hours_for_date(db, clock.to_local(start).date(), location_id)
    return bool(w and w[0] <= start and end <= w[1])


def day_label(db: Session, d: date, location_id: int = 1) -> str:
    w = hours_for_date(db, d, location_id)
    if not w:
        return "Closed"
    f = lambda x: clock.to_local(x).strftime("%I:%M %p").lstrip("0")  # noqa: E731
    return f"{f(w[0])} – {f(w[1])}"


def slot_grid(db: Session, d: date, slot_minutes: int, location_id: int = 1) -> list[tuple[datetime, datetime]]:
    w = hours_for_date(db, d, location_id)
    if not w:
        return []
    step = timedelta(minutes=slot_minutes)
    out, cur = [], w[0]
    while cur + step <= w[1]:
        out.append((cur, cur + step))
        cur += step
    return out


def nth_open_day_close(db: Session, start_day: date, n: int, location_id: int = 1) -> datetime:
    """Closing time of the n-th OPEN day, counting start_day as day 1 if the shop is open that day.
    (Weekends/holidays configured as closed are skipped: these are 'business days'.)"""
    d, count, last = start_day, 0, None
    for _ in range(120):
        w = hours_for_date(db, d, location_id)
        if w:
            count, last = count + 1, w[1]
            if count >= max(1, n):
                return last
        d += timedelta(days=1)
    return last or clock.local_dt(start_day + timedelta(days=7), datetime.min.time())


def business_day_number(db: Session, start_day: date, today: date, location_id: int = 1) -> int:
    """Which open day (1-based) `today` is, counting from start_day."""
    d, n = start_day, 0
    while d <= today:
        if hours_for_date(db, d, location_id):
            n += 1
        d += timedelta(days=1)
    return n


def supervisor_on_duty(db: Session, at: datetime, location_id: int = 1) -> StaffUser | None:
    shift = db.scalar(select(ShiftSchedule).where(ShiftSchedule.location_id == location_id,
                                                  ShiftSchedule.start_at <= at, ShiftSchedule.end_at > at)
                      .order_by(ShiftSchedule.start_at))
    return shift.staff if shift else None


def supervisor_covers(db: Session, start: datetime, end: datetime, location_id: int = 1) -> bool:
    """True if shifts (possibly back to back) cover the whole [start, end) interval."""
    shifts = db.scalars(select(ShiftSchedule).where(ShiftSchedule.location_id == location_id,
                                                    ShiftSchedule.start_at < end, ShiftSchedule.end_at > start)
                        .order_by(ShiftSchedule.start_at)).all()
    cursor = start
    for sh in shifts:
        if sh.start_at > cursor:
            break
        cursor = max(cursor, sh.end_at)
        if cursor >= end:
            return True
    return cursor >= end
