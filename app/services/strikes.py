"""Strikes: the only way a student gets suspended. Each strike is justified in writing, shown to the student,
expires on its own, and can be voided. Three active strikes trigger a time-limited suspension (never permanent)."""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.errors import UserError
from app.models import Strike, Student
from app.services import audit, notify
from app.services import settings as shop_settings

MIN_REASON = 15  # characters: "bad" is not a justification


def active(db: Session, student: Student, now: datetime | None = None) -> list[Strike]:
    now = now or clock.now()
    cfg = shop_settings.load(db)
    since = now - timedelta(days=cfg.noshow_window_days)
    if student.noshow_forgiven_at:
        since = max(since, student.noshow_forgiven_at)
    return list(db.scalars(select(Strike).where(Strike.student_id == student.id, Strike.voided_at.is_(None),
                                                Strike.issued_at > since).order_by(Strike.issued_at)))


def suspension_days(db: Session) -> int:
    cfg = shop_settings.load(db)
    return max(1, min(int(cfg.suspension_days), int(cfg.max_suspension_days)))


def give(db: Session, student: Student, actor: str, reason: str, *, kind: str = "staff", reservation_id: int | None = None,
         now: datetime | None = None, notify_student: bool = True) -> tuple[Strike, datetime | None]:
    """Record a justified strike. Returns (strike, suspended_until if this strike triggered a suspension)."""
    now = now or clock.now()
    reason = (reason or "").strip()
    if kind == "staff" and len(reason) < MIN_REASON:
        raise UserError(f"Explain why in at least {MIN_REASON} characters. The student will see this reason.", field="reason")
    cfg = shop_settings.load(db)
    s = Strike(student_id=student.id, kind=kind, reason=reason[:500], reservation_id=reservation_id,
               issued_by=actor.split(":", 1)[-1], issued_at=now)
    db.add(s)
    db.flush()
    rows = active(db, student, now)
    suspended_until = None
    if len(rows) >= cfg.noshow_threshold and not (student.suspended_until and student.suspended_until > now):
        suspended_until = now + timedelta(days=suspension_days(db))
        student.suspended_until = suspended_until
        student.suspension_reason = f"{len(rows)} strikes in {cfg.noshow_window_days} days: " + "; ".join(r.reason for r in rows)[:200]
        audit.record(db, "system", "student.auto_suspend", "student", student.id,
                     after={"until": suspended_until, "strikes": [r.id for r in rows]})
    audit.record(db, actor, "strike.give", "student", student.id, after={"strike": s.id, "kind": kind, "reason": s.reason,
                                                                         "active": len(rows)})
    if notify_student:
        notify.strike(db, student, s, len(rows), cfg.noshow_threshold, suspended_until)
    return s, suspended_until


def void(db: Session, strike: Strike, actor: str, reason: str) -> None:
    reason = (reason or "").strip()
    if len(reason) < 10:
        raise UserError("Say why this strike is being voided (at least 10 characters).", field="reason")
    if strike.voided_at:
        raise UserError("That strike is already voided.")
    now = clock.now()
    strike.voided_at, strike.voided_by, strike.void_reason = now, actor.split(":", 1)[-1], reason[:500]
    st = strike.student
    cfg = shop_settings.load(db)
    lifted = False
    if st.suspended_until and st.suspended_until > now and len(active(db, st, now)) < cfg.noshow_threshold:
        st.suspended_until, st.suspension_reason = None, None  # the suspension rested on this strike
        lifted = True
    audit.record(db, actor, "strike.void", "student", st.id, after={"strike": strike.id, "reason": reason, "suspension_lifted": lifted})
