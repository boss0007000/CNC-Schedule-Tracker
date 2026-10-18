"""Persists time-driven state changes. Safe to call at any time, from any process, any number of
times (idempotent): the scheduler calls it every minute and busy pages call `lazy_sweep`, so a sleeping
free-tier host catches up on the first request after it wakes."""
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.models import RES_CANCELLED, RES_CHECKED_IN, RES_COMPLETED, RES_CONFIRMED, RES_NO_SHOW, Reservation, ShopSession, Student
from app.services import audit, holds, notify, reservations, status
from app.services import settings as shop_settings

_last_run: datetime | None = None
LAZY_INTERVAL = timedelta(seconds=15)


def reset_throttle() -> None:
    global _last_run
    _last_run = None


def _excusal(db: Session, r: Reservation, now: datetime, cfg) -> str | None:
    """Reasons that are the shop's fault, never the student's: an emergency close or the machine being unusable
    at any point from the start of the slot until now. Returns a sentence for the student, or None."""
    from app.models import AuditLog, MaintenanceRecord
    m = r.machine
    until = max(now, r.grace_anchor + timedelta(minutes=cfg.checkin_grace_minutes))
    if cfg.emergency_active or db.scalar(select(AuditLog.id).where(
            AuditLog.action.in_(("emergency.close", "emergency.reopen")), AuditLog.at >= r.start_at, AuditLog.at <= until).limit(1)):
        return "the shop was closed unexpectedly (emergency close)"
    if m.maintenance_state != "none" or m.override_status in ("under_maintenance", "awaiting_inspection", "on_hold"):
        return f"the {m.name} was out of service"
    broke = db.scalar(select(MaintenanceRecord.id).where(
        MaintenanceRecord.machine_id == m.id, MaintenanceRecord.state_set.is_not(None), MaintenanceRecord.opened_at <= until,
        (MaintenanceRecord.closed_at.is_(None)) | (MaintenanceRecord.closed_at > r.start_at)).limit(1))
    scheduled = db.scalar(select(MaintenanceRecord.id).where(
        MaintenanceRecord.machine_id == m.id, MaintenanceRecord.window_start.is_not(None),
        MaintenanceRecord.window_start < until, MaintenanceRecord.window_end > r.start_at).limit(1))
    if broke or scheduled:
        return f"the {m.name} was out of service"
    return None


def mark_no_show(db: Session, r: Reservation, now: datetime) -> str | None:
    """At the end of the grace period, record a no-show strike only after re-reading the database (a check-in that
    landed a moment ago must never be penalised) and ruling out the shop's own fault. Returns what happened:
    "no_show", "excused" (cancelled without penalty), "attended" (they did check in), or None (nothing to do)."""
    db.refresh(r, with_for_update=True)  # fresh read; waits for a check-in that is mid-flight
    if r.status != RES_CONFIRMED:
        return None
    cfg = shop_settings.load(db)
    sess = db.scalar(select(ShopSession).where(ShopSession.reservation_id == r.id).order_by(ShopSession.id.desc()).limit(1))
    if sess is not None:  # they did arrive; the reservation status just hadn't caught up
        if sess.status == "active":
            r.status = RES_CHECKED_IN
        elif sess.status in ("closed", "auto_closed", "force_closed"):
            r.status = RES_COMPLETED
        audit.record(db, "system", "reservation.reconciled", "reservation", r.id, {"status": RES_CONFIRMED}, {"status": r.status})
        return "attended"
    why = _excusal(db, r, now, cfg)
    if why:
        r.status, r.cancelled_at, r.cancel_reason = RES_CANCELLED, now, f"excused (no penalty): {why}"
        audit.record(db, "system", "reservation.excused", "reservation", r.id, {"status": RES_CONFIRMED}, {"reason": why})
        notify.reservation_excused(db, r, why)
        if r.end_at > now:
            reservations.promote_waitlist(db, r.machine_id, max(r.start_at, now), r.end_at, now)
        return "excused"
    r.status = RES_NO_SHOW
    audit.record(db, "system", "reservation.no_show", "reservation", r.id, {"status": RES_CONFIRMED},
                 {"status": RES_NO_SHOW})
    stu: Student = r.student
    db.flush()
    from app.services import strikes
    _, suspended_until = strikes.give(
        db, stu, "system", f"Did not check in for {r.machine.name} ({reservations.fmt(r.start_at)}, {r.ref}) within "
                           f"{cfg.checkin_grace_minutes} minutes of the start.",
        kind="no_show", reservation_id=r.id, now=now, notify_student=False)
    notify.no_show(db, r, suspended_until)
    if r.end_at > now:
        reservations.promote_waitlist(db, r.machine_id, max(r.start_at, now), r.end_at, now)
    from app.services import sessions
    sessions.notify_next_up(db, r.machine_id, now)
    return "no_show"


def sweep(db: Session, now: datetime | None = None) -> dict:
    now = now or clock.now()
    cfg = shop_settings.load(db)
    grace = timedelta(minutes=cfg.checkin_grace_minutes)
    buffer = timedelta(minutes=cfg.autoclose_buffer_minutes)
    out = {"no_shows": 0, "overdue_warnings": 0, "auto_closed": 0, "offers_expired": 0}

    arrivals: dict[int, datetime] = {}
    for rid, at_, in db.execute(select(ShopSession.reservation_id, ShopSession.requested_at).where(
            ShopSession.status.in_(("pending", "expired", "denied")), ShopSession.requested_at.is_not(None))):
        arrivals[rid] = min(at_, arrivals.get(rid, at_))
    for r in db.scalars(select(Reservation).where(Reservation.status == RES_CONFIRMED, Reservation.start_at < now)):
        if r.id in arrivals and arrivals[r.id] <= r.grace_anchor + grace:
            # They showed up on time and asked to check in. Whether a supervisor got to them is not their fault:
            # never a no-show. If their whole slot passes without an approved session, release it quietly.
            if now > r.end_at and not db.scalar(select(ShopSession.id).where(ShopSession.reservation_id == r.id,
                                                                              ShopSession.status == "pending")):
                r.status, r.cancelled_at, r.cancel_reason = "cancelled", now, "no supervisor approval before the slot ended"
                audit.record(db, "system", "reservation.unapproved_release", "reservation", r.id)
            continue
        if now > r.grace_anchor + grace:
            res = mark_no_show(db, r, now)
            if res == "no_show":
                out["no_shows"] += 1
            elif res == "excused":
                out["excused"] = out.get("excused", 0) + 1

    for s in db.scalars(select(ShopSession).where(ShopSession.status == "active", ShopSession.expected_end_at < now)):
        deadline = status.session_deadline(db, s, now)  # later than the booked end if nobody is waiting
        if now <= deadline:
            continue  # open-ended use: keep going, no warning, no auto-close
        if s.overdue_notified_at is None:
            s.overdue_notified_at = now
            notify.overdue_warning(db, s)
            audit.record(db, "system", "session.overdue", "session", s.id)
            out["overdue_warnings"] += 1
        if now > deadline + buffer:
            from app.services import sessions
            s.auto_closed, s.needs_review = True, True
            sessions._close(db, s, now, status="auto_closed", end_time=deadline)
            notify.auto_closed(db, s)
            out["auto_closed"] += 1

    out["offers_expired"] = reservations.expire_offers(db, now)
    from app.services import shifts
    out["shift_alerts"] = shifts.alert_missing(db, now)
    from app.services import sessions as sessions_svc
    out["checkins_expired"] = sessions_svc.expire_pending(db, now)
    out["holds_expired"] = holds.expire_due(db, now)
    out["hold_reminders"] = holds.daily_reminders(db, now)
    out["closing_prompts"] = holds.closing_prompts(db, now)
    from app.services import changes
    out["requests_expired"] = changes.expire_old(db, now)
    db.flush()
    return out


def lazy_sweep(db: Session) -> None:
    """Cheap, throttled sweep for request handlers (never required for correctness)."""
    global _last_run
    now = clock.now()
    if _last_run is not None and timedelta(0) <= now - _last_run < LAZY_INTERVAL:
        return
    _last_run = now
    sweep(db, now)
