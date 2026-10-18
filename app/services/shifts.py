"""Supervisors must clock in to their scheduled shift (with the shop's rotating code) to count as on duty."""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.errors import UserError
from app.models import ShiftSchedule, StaffUser
from app.services import audit, notify, rotcode
from app.services import settings as shop_settings

EARLY = timedelta(minutes=15)  # may clock in this long before the shift starts
LATE_ALERT = timedelta(minutes=10)  # alert admins when still not in this long after the start


def my_shift(db: Session, user: StaffUser, now: datetime | None = None) -> ShiftSchedule | None:
    now = now or clock.now()
    return db.scalar(select(ShiftSchedule).where(ShiftSchedule.staff_id == user.id, ShiftSchedule.start_at <= now + EARLY,
                                                 ShiftSchedule.end_at > now).order_by(ShiftSchedule.start_at))


def clock_in(db: Session, user: StaffUser, code: str, ip: str, now: datetime | None = None) -> ShiftSchedule:
    now = now or clock.now()
    sh = my_shift(db, user, now)
    if sh is None:
        raise UserError("You don't have a shift scheduled right now. Ask an admin to add one.")
    if sh.checked_in_at and not sh.checked_out_at:
        return sh
    if shop_settings.load(db).require_shift_clockin:
        rotcode.verify(db, code, ip, f"staff:{user.id}", now)  # the code shown on the shop TV proves you are in the shop
    sh.checked_in_at, sh.checked_out_at = now, None
    audit.record(db, f"staff:{user.username}", "shift.clock_in", "shift_schedule", sh.id, ip=ip)
    return sh


def clock_out(db: Session, user: StaffUser, now: datetime | None = None) -> None:
    now = now or clock.now()
    sh = db.scalar(select(ShiftSchedule).where(ShiftSchedule.staff_id == user.id, ShiftSchedule.checked_in_at.is_not(None),
                                               ShiftSchedule.checked_out_at.is_(None), ShiftSchedule.end_at > now))
    if sh is None:
        raise UserError("You are not clocked in.")
    sh.checked_out_at = now
    audit.record(db, f"staff:{user.username}", "shift.clock_out", "shift_schedule", sh.id)


def alert_missing(db: Session, now: datetime | None = None) -> int:
    """Shift started more than LATE_ALERT ago and nobody clocked in: nudge the person once and tell the admins."""
    from app import emailer
    from app.config import get_config
    now = now or clock.now()
    if not shop_settings.load(db).require_shift_clockin:
        return 0
    n = 0
    rows = db.scalars(select(ShiftSchedule).where(ShiftSchedule.start_at <= now - LATE_ALERT, ShiftSchedule.end_at > now,
                                                  ShiftSchedule.checked_in_at.is_(None), ShiftSchedule.alerted_at.is_(None)))
    for sh in rows:
        sh.alerted_at = now
        when = clock.to_local(sh.start_at).strftime("%a %I:%M %p").replace(" 0", " ")
        if sh.staff.email:
            emailer.queue(db, sh.staff.email, "Please clock in to your shop shift",
                          f"Your supervisor shift started at {when} but you haven't clocked in. Sign in and enter the code shown on "
                          f"the shop TV: {get_config().base_url}/admin")
        notify.to_staff(db, f"{sh.staff.display_name} has not clocked in (shift began {when})",
                        f"{sh.staff.display_name} was scheduled from {when} but hasn't clocked in. Students may be waiting for a "
                        f"supervisor. Please check the floor.", perm="admin")
        audit.record(db, "system", "shift.no_clock_in", "shift_schedule", sh.id)
        n += 1
    return n
