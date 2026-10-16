"""Shop hours, holidays and supervisor shifts (admin)."""
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.errors import UserError
from app.models import ShiftSchedule, ShopHours, StaffUser
from app.services import audit

def ensure_base_data(db: Session) -> None:
    """Idempotent: the default location and Mon-Fri 08:00-22:00 hours. Runs on every boot, so a brand-new
    production database is usable immediately (machines reference location 1)."""
    from app.models import Location
    if db.get(Location, 1) is None:
        db.add(Location(id=1, name="Main Shop"))
        db.flush()
    if db.scalar(select(ShopHours.id).limit(1)) is None:
        for wd in range(7):
            weekday = wd < 5
            db.add(ShopHours(location_id=1, weekday=wd, open_time=time(8) if weekday else None,
                             close_time=time(22) if weekday else None, closed=not weekday))
        db.flush()


WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _t(raw: str | None) -> time | None:
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%H:%M").time()
    except ValueError:
        raise UserError("Times must look like 08:00.") from None


def save_weekly_hours(db: Session, actor: str, form) -> None:
    before = {}
    after = {}
    for wd in range(7):
        row = db.scalar(select(ShopHours).where(ShopHours.location_id == 1, ShopHours.weekday == wd))
        closed = form.get(f"closed_{wd}") == "on"
        o, c = _t(form.get(f"open_{wd}")), _t(form.get(f"close_{wd}"))
        if not closed and (o is None or c is None or c <= o):
            raise UserError(f"{WEEKDAYS[wd]}: closing time must be after opening time (or tick Closed).")
        if row is None:
            row = ShopHours(location_id=1, weekday=wd)
            db.add(row)
        before[WEEKDAYS[wd]] = "closed" if row.closed else f"{row.open_time}-{row.close_time}"
        row.closed, row.open_time, row.close_time = closed, None if closed else o, None if closed else c
        after[WEEKDAYS[wd]] = "closed" if closed else f"{o}-{c}"
    db.flush()
    audit.record(db, actor, "hours.weekly", "shop_hours", None, before, after)


def add_exception(db: Session, actor: str, on: str, closed: bool, open_t: str | None, close_t: str | None, label: str) -> ShopHours:
    try:
        d = datetime.strptime(on, "%Y-%m-%d").date()
    except ValueError:
        raise UserError("Enter the date of the holiday or exception.") from None
    o, c = _t(open_t), _t(close_t)
    if not closed and (o is None or c is None or c <= o):
        raise UserError("Special hours need an opening time and a later closing time (or tick Closed).")
    row = db.scalar(select(ShopHours).where(ShopHours.location_id == 1, ShopHours.on_date == d))
    if row is None:
        row = ShopHours(location_id=1, on_date=d)
        db.add(row)
    row.closed, row.open_time, row.close_time, row.label = closed, None if closed else o, None if closed else c, label[:80] or None
    db.flush()
    audit.record(db, actor, "hours.exception", "shop_hours", row.id, after={"date": d, "closed": closed, "label": label})
    return row


def delete_exception(db: Session, actor: str, row: ShopHours) -> None:
    if row.on_date is None:
        raise UserError("Weekly hours can't be deleted; edit them instead.")
    audit.record(db, actor, "hours.exception_delete", "shop_hours", row.id, before={"date": row.on_date})
    db.delete(row)


def add_shift(db: Session, actor: str, staff_id: int, start: datetime, end: datetime, repeat_weeks: int = 0,
              weekdays_only: bool = False) -> int:
    staff = db.get(StaffUser, staff_id)
    if staff is None or staff.role == "monitor":
        raise UserError("Choose a supervisor or admin for the shift.")
    if end <= start or (end - start) > timedelta(hours=16):
        raise UserError("A shift must end after it starts and last at most 16 hours.")
    n = 0
    for w in range(0, max(0, min(repeat_weeks, 26)) + 1):
        s, e = start + timedelta(weeks=w), end + timedelta(weeks=w)
        db.add(ShiftSchedule(location_id=1, staff_id=staff.id, start_at=s, end_at=e))
        n += 1
    db.flush()
    audit.record(db, actor, "shift.add", "shift_schedule", None, after={"staff": staff.username, "start": start, "end": end, "count": n})
    return n


def delete_shift(db: Session, actor: str, shift: ShiftSchedule) -> None:
    audit.record(db, actor, "shift.delete", "shift_schedule", shift.id,
                 before={"staff": shift.staff.username, "start": shift.start_at, "end": shift.end_at})
    db.delete(shift)
