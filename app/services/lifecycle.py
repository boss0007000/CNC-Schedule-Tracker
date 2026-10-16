"""Persists time-driven state changes. Safe to call at any time, from any process, any number of
times (idempotent): the scheduler calls it every minute and busy pages call `lazy_sweep`, so a sleeping
free-tier host catches up on the first request after it wakes."""
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.models import RES_CONFIRMED, RES_COMPLETED, RES_NO_SHOW, Reservation, ShopSession, Student
from app.services import audit, holds, notify, reservations, status
from app.services import settings as shop_settings

_last_run: datetime | None = None
LAZY_INTERVAL = timedelta(seconds=15)


def reset_throttle() -> None:
    global _last_run
    _last_run = None


def mark_no_show(db: Session, r: Reservation, now: datetime) -> None:
    """Flag a reservation No-Show, release the slot (waitlist), maybe suspend repeat offenders."""
    if r.status != RES_CONFIRMED:
        return
    cfg = shop_settings.load(db)
    r.status = RES_NO_SHOW
    audit.record(db, "system", "reservation.no_show", "reservation", r.id, {"status": RES_CONFIRMED},
                 {"status": RES_NO_SHOW})
    stu: Student = r.student
    since = now - timedelta(days=cfg.noshow_window_days)
    if stu.noshow_forgiven_at:
        since = max(since, stu.noshow_forgiven_at)
    db.flush()
    count = db.scalar(select(func.count()).select_from(Reservation).where(
        Reservation.student_id == stu.id, Reservation.status == RES_NO_SHOW, Reservation.start_at > since)) or 0
    suspended_until = None
    if count >= cfg.noshow_threshold and not (stu.suspended_until and stu.suspended_until > now):
        suspended_until = now + timedelta(days=cfg.suspension_days)
        stu.suspended_until = suspended_until
        stu.suspension_reason = f"{count} no-shows in {cfg.noshow_window_days} days"
        audit.record(db, "system", "student.auto_suspend", "student", stu.id, after={
            "until": suspended_until, "reason": stu.suspension_reason})
    notify.no_show(db, r, suspended_until)
    if r.end_at > now:
        reservations.promote_waitlist(db, r.machine_id, max(r.start_at, now), r.end_at, now)
    from app.services import sessions
    sessions.notify_next_up(db, r.machine_id, now)


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
            mark_no_show(db, r, now)
            out["no_shows"] += 1

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
