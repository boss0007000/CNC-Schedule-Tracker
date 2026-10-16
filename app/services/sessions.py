"""Check-in / check-out and staff overrides."""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import clock
from app.errors import UserError
from app.models import (
    RES_CHECKED_IN, RES_COMPLETED, RES_CONFIRMED, DamageReport, MaintenanceRecord, Reservation, ShopSession, Student,
)
from app.services import access, audit, files, holds, notify, numbers, reservations, rotcode
from app.services import settings as shop_settings

CHECKLIST_ITEMS = [("machine_cleaned", "Machine cleaned"), ("tools_returned", "Tools returned"),
                   ("workstation_clean", "Workstation left clean")]


def active_session_for(db: Session, student_id: int) -> ShopSession | None:
    return db.scalar(select(ShopSession).where(ShopSession.student_id == student_id, ShopSession.status == "active"))


def _find_reservation(db: Session, student: Student, ref: str) -> Reservation:
    r = db.scalar(select(Reservation).where(Reservation.ref == numbers.normalize_ref(ref, "RSV")))
    if r is None or r.student_id != student.id:
        raise UserError("We couldn't find that reservation under your email. Check the reference in your "
                        "confirmation email (it looks like RSV-7K3Q9F).", code="ref_not_found", field="ref")
    return r


# ----------------------------------------------------------------------- check-in
def check_in(db: Session, student: Student, ref: str, shop_code: str, ip: str, *, now: datetime | None = None) -> ShopSession:
    now = now or clock.now()
    cfg = shop_settings.load(db)
    r = _find_reservation(db, student, ref)
    rotcode.verify(db, shop_code, ip, r.ref, now)  # proves physical presence; failures are rate-limited
    name = r.machine.name
    if r.status == RES_CHECKED_IN:
        raise UserError(f"You're already checked in to the {name}.", code="already_checked_in")
    if r.status != RES_CONFIRMED:
        raise UserError(f"Reservation {r.ref} is {r.status.replace('_', ' ')}, so it can't be used to check in.",
                        code="not_confirmed")
    if cfg.emergency_active:
        raise UserError("The shop is closed for an emergency. Please follow staff instructions.", code="emergency")
    if clock.to_local(now).date() != clock.to_local(r.start_at).date():
        raise UserError(f"This reservation is for {reservations.fmt(r.start_at)}, not today.", code="wrong_day")
    access.check_access(db, student, r.machine)  # still on the list and still allowed on this machine
    opens = r.start_at - timedelta(minutes=cfg.checkin_early_minutes)
    if now < opens:
        free_from = early_window_opens(db, r, now, cfg)
        if free_from is None or now < free_from:
            raise UserError(f"Too early to check in. Check-in opens at {reservations.fmt_time(opens)}"
                            f"{' (earlier if the machine is free)' if cfg.early_checkin_max_minutes else ''}.",
                            code="too_early")
    if now > r.grace_anchor + timedelta(minutes=cfg.checkin_grace_minutes):
        from app.services import lifecycle
        lifecycle.mark_no_show(db, r, now)
        raise UserError(f"You're more than {cfg.checkin_grace_minutes} minutes late, so this reservation was marked "
                        f"as a no-show and the slot was released. Please book a new time.", code="late", persist=True)
    if active_session_for(db, student.id):
        raise UserError("You're already checked in on another machine. Check out of it first.", code="has_active")
    if r.machine.maintenance_state != "none":
        raise UserError(f"The {name} is not available right now ({r.machine.maintenance_state.replace('_', ' ')}). "
                        f"Please see shop staff.", code="maintenance")
    if r.machine.override_status in ("under_maintenance", "awaiting_inspection"):
        raise UserError(f"The {name} is not available right now. Please see shop staff.", code="maintenance")
    h = holds.active_hold(db, r.machine_id, now)
    if h and h.student_id != student.id:
        raise UserError(f"The {name} has {h.student.public_name}'s {'setup' if h.kind == 'setup' else 'tools'} in it (please don't "
                        f"touch it) until {reservations.fmt(h.expires_at)}. Call them from the schedule page, or ask a supervisor.",
                        code="machine_on_hold")
    busy = db.scalar(select(ShopSession).where(ShopSession.machine_id == r.machine_id, ShopSession.status == "active"))
    if busy:
        raise UserError(f"The {name} still shows as in use by the previous person. Ask shop staff to clear it.",
                        code="machine_busy")
    return _start_session(db, r, now, actor=f"student:{student.email}")


def early_window_opens(db: Session, r: Reservation, now: datetime, cfg) -> datetime | None:
    """If the machine is idle and nobody else is booked between now and this reservation, the student may start
    early (their end time stays the same). Returns the earliest allowed moment, or None."""
    if not cfg.early_checkin_max_minutes or now >= r.start_at:
        return None
    earliest = r.start_at - timedelta(minutes=cfg.early_checkin_max_minutes)
    if db.scalar(select(ShopSession.id).where(ShopSession.machine_id == r.machine_id, ShopSession.status == "active")):
        return None
    h = holds.active_hold(db, r.machine_id, now)
    if h is not None and h.student_id != r.student_id:
        return None
    between = [x for x in reservations.overlapping(db, r.machine_id, max(now, earliest), r.start_at, exclude_id=r.id)]
    if between or reservations._holds_blocking(db, r.machine_id, max(now, earliest), r.start_at, now, r.student_id):
        return None
    return earliest


def notify_next_up(db: Session, machine_id: int, now: datetime) -> None:
    """When a machine frees up early, tell ONLY the next student in line they may start early (once)."""
    from app.models import AuditLog
    cfg = shop_settings.load(db)
    nxt = db.scalar(select(Reservation).where(Reservation.machine_id == machine_id, Reservation.status == RES_CONFIRMED,
                                              Reservation.start_at > now).order_by(Reservation.start_at).limit(1))
    if nxt is None or clock.to_local(nxt.start_at).date() != clock.to_local(now).date():
        return
    opens = early_window_opens(db, nxt, now, cfg)
    if opens is None or now < opens:
        return
    if db.scalar(select(AuditLog.id).where(AuditLog.action == "early.notified", AuditLog.entity_id == str(nxt.id))):
        return
    notify.machine_free_early(db, nxt)
    audit.record(db, "system", "early.notified", "reservation", nxt.id)


def _start_session(db: Session, r: Reservation, now: datetime, actor: str, forced_by: str | None = None) -> ShopSession:
    s = ShopSession(reservation_id=r.id, student_id=r.student_id, machine_id=r.machine_id, checked_in_at=now,
                    expected_end_at=max(r.end_at, now + timedelta(minutes=1)), forced_by=forced_by)
    r.status = RES_CHECKED_IN
    try:
        with db.begin_nested():
            db.add(s)
            db.flush()
    except IntegrityError:  # double-scan race: unique active session per machine/student
        raise UserError("This check-in was already processed or the machine is in use.", code="duplicate") from None
    s.student, s.machine = r.student, r.machine
    audit.record(db, actor, "session.check_in" if not forced_by else "session.force_check_in", "session", s.id,
                 after={"reservation": r.ref, "machine": r.machine.name})
    return s


def force_check_in(db: Session, r: Reservation, actor: str, reason: str, now: datetime | None = None) -> ShopSession:
    now = now or clock.now()
    if not reason.strip():
        raise UserError("Give a reason for the override (it is recorded in the audit log).")
    if r.status != RES_CONFIRMED:
        raise UserError(f"Reservation {r.ref} is {r.status.replace('_', ' ')}.")
    if active_session_for(db, r.student_id):
        raise UserError("That student already has an active session.")
    if db.scalar(select(ShopSession.id).where(ShopSession.machine_id == r.machine_id, ShopSession.status == "active")):
        raise UserError("That machine has an active session. Close it first.")
    s = _start_session(db, r, now, actor, forced_by=actor.split(":", 1)[-1])
    audit.record(db, actor, "session.force_reason", "session", s.id, after={"reason": reason})
    return s


# ----------------------------------------------------------------------- check-out
def check_out(db: Session, student: Student, ref: str | None, shop_code: str, checklist: dict[str, bool], ip: str, *,
              report_kind: str = "", report_text: str = "", photo=None, now: datetime | None = None,
              leave_state: str = "clear") -> ShopSession:
    now = now or clock.now()
    s = active_session_for(db, student.id)
    if s is None:
        raise UserError("You don't have an active session to check out of. If you already checked out, you're all set.",
                        code="no_session")
    if ref:
        r = _find_reservation(db, student, ref)
        if s.reservation_id != r.id:
            raise UserError("That reservation isn't your current session.", code="wrong_ref", field="ref")
    rotcode.verify(db, shop_code, ip, s.reservation.ref if s.reservation else f"S{s.id}", now)
    if leave_state not in ("clear", "tools", "setup"):
        raise UserError("Please tell us whether anything is staying in the machine.", code="leave_state", field="leave_state")
    missing = [label for key, label in CHECKLIST_ITEMS if not checklist.get(key)]
    if missing:
        raise UserError("Please complete the checklist: " + ", ".join(missing) + ".", code="checklist", field="checklist")
    if report_kind not in ("", "none", "damage", "maintenance"):
        raise UserError("Unknown report type.")
    has_report = report_kind in ("damage", "maintenance")
    if has_report and len(report_text.strip()) < 5:
        raise UserError("Describe the problem so staff know what to look at.", code="report_text", field="report_text")
    photo_stored = None
    if has_report and photo is not None and getattr(photo, "filename", ""):
        data = files.read_upload(photo)
        photo_stored = files.store(db, photo.filename, photo.content_type, data, allowed_ext=files.PHOTO_EXT)
    if leave_state == "clear":  # they've emptied it: lift any hold of theirs on this machine
        mine = holds.active_hold(db, s.machine_id, now)
        if mine is not None and mine.student_id == student.id:
            holds.release(db, mine, f"student:{student.email}", "owner", now)
    else:
        holds.declare(db, student, s.machine, leave_state, via="checkout", now=now)
    _close(db, s, now, status="closed", checklist=checklist)
    if has_report:
        file_report(db, s.machine, student, s, report_kind, report_text.strip(), photo_stored, f"student:{student.email}")
    return s


def file_report(db: Session, machine, student, session, kind: str, text: str, photo, actor: str) -> DamageReport:
    state = "under_maintenance" if kind == "maintenance" else "awaiting_inspection"
    rep = DamageReport(machine_id=machine.id, student_id=student.id if student else None,
                       session_id=session.id if session else None, severity=kind, description=text,
                       photo_key=photo.key if photo else None, photo_name=photo.original_name if photo else None)
    db.add(rep)
    machine.maintenance_state = state
    db.add(MaintenanceRecord(machine_id=machine.id, kind="issue", description=text, state_set=state,
                             opened_by=actor))
    db.flush()
    audit.record(db, actor, "machine.report", "machine", machine.id, after={"state": state, "report": rep.id})
    notify.to_staff(db, f"Machine reported: {machine.name} ({kind})",
                    f"{student.full_name if student else 'Staff'} reported an issue on {machine.name}:\n\n{text}")
    return rep


def _close(db: Session, s: ShopSession, now: datetime, status: str, checklist: dict | None = None,
           end_time: datetime | None = None) -> None:
    s.checked_out_at = end_time or now
    s.status = status
    s.checklist = checklist
    r = s.reservation
    if r is not None and r.status == RES_CHECKED_IN:
        r.status = RES_COMPLETED
    db.flush()
    audit.record(db, f"student:{s.student.email}" if status == "closed" else "system", f"session.{status}",
                 "session", s.id, after={"minutes": s.duration_minutes})
    if r is not None and now < r.end_at:  # left early: offer the freed time to the waiting list
        reservations.promote_waitlist(db, r.machine_id, now, r.end_at, now)
        notify_next_up(db, r.machine_id, now)


def force_check_out(db: Session, s: ShopSession, actor: str, reason: str, now: datetime | None = None) -> ShopSession:
    now = now or clock.now()
    if s.status != "active":
        raise UserError("That session is already closed.")
    if not reason.strip():
        raise UserError("Give a reason for the override (it is recorded in the audit log).")
    s.forced_by = actor.split(":", 1)[-1]
    _close(db, s, now, status="force_closed")
    s.needs_review = False
    audit.record(db, actor, "session.force_check_out", "session", s.id, after={"reason": reason})
    return s
