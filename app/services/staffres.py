"""Staff-side reservation management (manual create, edit, release, conflict detection)."""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.errors import UserError
from datetime import timedelta as _td  # noqa: F401
from app.models import RES_ACTIVE, Machine, Reservation, ShopSession, Student
from app.services import audit, lifecycle, notify, reservations
from app.services import settings as shop_settings


def manual_create(db: Session, actor: str, email: str, machine_id: int, start: datetime, hours: float,
                  approval_number: str, override: bool, reason: str) -> Reservation:
    stu = db.scalar(select(Student).where(Student.email == email.strip().lower()))
    if stu is None:
        raise UserError("No student with that email has verified yet. Ask them to verify at /verify first.", field="email")
    if override and len(reason.strip()) < 3:
        raise UserError("A reason is required when ignoring booking rules.", field="reason")
    if not override and not approval_number.strip():
        raise UserError("Enter the approval number, or tick 'ignore booking rules'.", field="approval")
    slot = shop_settings.load(db).slot_minutes
    end = start + timedelta(minutes=round(hours * 60 / slot) * slot)
    r = reservations.create(db, stu, approval_number or None, machine_id, start, end, waiver_accepted=override,
                            actor=actor, override=override, allow_started=True)
    if override:
        audit.record(db, actor, "reservation.override_rules", "reservation", r.id, after={"reason": reason})
    return r


def set_window(db: Session, actor: str, r: Reservation, machine_id: int, start: datetime, end: datetime,
               reason: str) -> Reservation:
    """Change how long (and when) a student may be on a machine, within the limits of THEIR approval:
    remaining approval hours/sessions, approval expiry, the longest single session for their access group, shop
    hours, supervisor cover, maintenance/holds and other bookings. A student who is on the machine keeps their start."""
    if r.status not in ("confirmed", "checked_in"):
        raise UserError("Only a current or upcoming reservation can be edited.")
    if len((reason or "").strip()) < 3:
        raise UserError("Give a short reason (it is recorded and the student is told).", field="reason")
    now = clock.now()
    if r.status == "checked_in":
        start, machine_id = r.start_at, r.machine_id  # they're already on it: only the end time can change
        if end <= now:
            raise UserError("The new end time must be in the future (to end the session now use Force check-out).")
    db.execute(select(Machine.id).where(Machine.id == machine_id).with_for_update())
    reservations.validate(db, r.student, r.approval.number if r.approval else None, machine_id, start, end, now=now,
                          exclude_id=r.id, allow_started=True, waiver_accepted=True, contact_accepted=True,
                          override=r.approval_id is None, staff_edit=True)
    before = {"machine_id": r.machine_id, "start": r.start_at, "end": r.end_at}
    old = (r.machine_id, r.start_at, r.end_at)
    r.machine_id, r.start_at, r.end_at = machine_id, start, end
    sess = db.scalar(select(ShopSession).where(ShopSession.reservation_id == r.id,
                                               ShopSession.status.in_(("active", "pending"))))
    if sess:
        sess.expected_end_at = max(end, now + timedelta(minutes=1))
        sess.overdue_notified_at = None
    db.flush()
    db.refresh(r)
    audit.record(db, actor, "reservation.edit", "reservation", r.id, before,
                 {"machine_id": machine_id, "start": start, "end": end, "reason": reason.strip()})
    notify.reservation_confirmed(db, r, extended=True)
    if old[2] > end and old[0] == machine_id:  # time was freed up
        reservations.promote_waitlist(db, machine_id, max(end, now), old[2])
    elif old[0] != machine_id:
        reservations.promote_waitlist(db, old[0], old[1], old[2])
    return r


def move(db: Session, actor: str, r: Reservation, machine_id: int, start: datetime, end: datetime,
         reason: str = "staff edit") -> None:
    set_window(db, actor, r, machine_id, start, end, reason)


def mark_no_show(db: Session, actor: str, r: Reservation) -> None:
    if r.status != "confirmed":
        raise UserError("Only confirmed reservations can be marked as no-shows.")
    lifecycle.mark_no_show(db, r, clock.now())
    audit.record(db, actor, "reservation.manual_no_show", "reservation", r.id)


def conflicts(db: Session) -> list[tuple[Reservation, Reservation]]:
    """Pairs of active future reservations that overlap on the same machine (e.g. after edits/maintenance)."""
    now = clock.now()
    rows = db.scalars(select(Reservation).where(Reservation.status.in_(RES_ACTIVE), Reservation.end_at > now)
                      .order_by(Reservation.machine_id, Reservation.start_at)).all()
    out = []
    for i, a in enumerate(rows):
        for b in rows[i + 1:]:
            if b.machine_id != a.machine_id or b.start_at >= a.end_at:
                break
            out.append((a, b))
    return out
