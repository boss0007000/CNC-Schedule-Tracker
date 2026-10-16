"""Setup / tools holds: 'my setup (or only my tools) is in this machine: do not touch'.

* a student declares it when leaving (check-out form, a one-tap email near closing, or My shop);
* it blocks other students from booking/using the machine until it expires or is cleared;
* it is capped in OPEN days (weekends/holidays configured as closed don't count);
* the owner gets one friendly reminder per open day, and everyone else sees it on the schedule page and the TV."""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.errors import UserError
from app.models import AuditLog, Machine, MachineHold, Reservation, ShopSession, Student
from app.services import audit, notify, shop
from app.services import settings as shop_settings

KINDS = ("setup", "tools")
RECENT_HOURS = 36


def active_hold(db: Session, machine_id: int, now: datetime | None = None) -> MachineHold | None:
    now = now or clock.now()
    return db.scalar(select(MachineHold).where(MachineHold.machine_id == machine_id, MachineHold.released_at.is_(None),
                                               MachineHold.expires_at > now))


def cap_days(cfg, kind: str) -> int:
    return int(cfg.setup_hold_max_business_days if kind == "setup" else cfg.tools_hold_max_business_days)


def recent_session(db: Session, student: Student, machine_id: int, now: datetime) -> ShopSession | None:
    since = now - timedelta(hours=RECENT_HOURS)
    return db.scalar(select(ShopSession).where(
        ShopSession.student_id == student.id, ShopSession.machine_id == machine_id,
        (ShopSession.status == "active") | (ShopSession.checked_out_at >= since)).order_by(ShopSession.id.desc()).limit(1))


def declare(db: Session, student: Student, machine: Machine, kind: str, *, via: str, now: datetime | None = None,
            note: str = "", actor: str | None = None) -> MachineHold:
    now = now or clock.now()
    cfg = shop_settings.load(db)
    if kind not in KINDS:
        raise UserError("Choose what is staying in the machine.", code="bad_kind")
    if actor is None and recent_session(db, student, machine.id, now) is None:
        raise UserError(f"You haven't used the {machine.name} recently, so you can't put a hold on it. "
                        f"Ask a supervisor if something of yours is in it.", code="no_recent_use")
    existing = db.scalar(select(MachineHold).where(MachineHold.machine_id == machine.id, MachineHold.released_at.is_(None)))
    if existing is not None and existing.expires_at > now and existing.student_id != student.id:
        raise UserError(f"The {machine.name} already has {existing.student.public_name}'s "
                        f"{'setup' if existing.kind == 'setup' else 'tools'} in it. A supervisor can sort that out.",
                        code="already_held")
    start_day = clock.to_local(now).date()
    if existing is not None and existing.student_id == student.id:  # same owner re-declaring: keep the original start
        start_day = clock.to_local(existing.started_at).date()
        existing.kind = kind
        existing.expires_at = shop.nth_open_day_close(db, start_day, cap_days(cfg, kind), machine.location_id)
        if existing.expires_at <= now:
            existing.expires_at = now + timedelta(hours=1)
        audit.record(db, actor or f"student:{student.email}", "hold.update", "machine_hold", existing.id, after={"kind": kind})
        return existing
    if existing is not None:  # a stale (expired but unreleased) hold from someone else
        existing.released_at, existing.release_reason = now, "expired"
        db.flush()
    expires = shop.nth_open_day_close(db, start_day, cap_days(cfg, kind), machine.location_id)
    if expires <= now:  # left after the last open-day close: give at least the next open day
        expires = shop.nth_open_day_close(db, start_day + timedelta(days=1), cap_days(cfg, kind), machine.location_id)
    h = MachineHold(machine_id=machine.id, student_id=student.id, kind=kind, started_at=now, expires_at=expires,
                    declared_via=via, note=note[:200] or None)
    db.add(h)
    db.flush()
    h.machine, h.student = machine, student
    audit.record(db, actor or f"student:{student.email}", "hold.declare", "machine_hold", h.id,
                 after={"machine": machine.name, "kind": kind, "until": expires, "via": via})
    for r in db.scalars(select(Reservation).where(Reservation.machine_id == machine.id, Reservation.status == "confirmed",
                                                  Reservation.student_id != student.id, Reservation.end_at > now,
                                                  Reservation.start_at < expires)):
        notify.hold_affects_booking(db, h, r)
    return h


def release(db: Session, h: MachineHold, actor: str, reason: str = "owner", now: datetime | None = None) -> None:
    now = now or clock.now()
    if h.released_at:
        raise UserError("That hold is already cleared.", code="closed")
    h.released_at, h.release_reason = now, reason
    audit.record(db, actor, "hold.release", "machine_hold", h.id, {"until": h.expires_at}, {"reason": reason})
    db.flush()
    from app.services import sessions
    sessions.notify_next_up(db, h.machine_id, now)


def extend(db: Session, h: MachineHold, actor: str, extra_days: int, now: datetime | None = None) -> None:
    now = now or clock.now()
    if h.released_at:
        raise UserError("That hold is already cleared.")
    if not 1 <= extra_days <= 10:
        raise UserError("Extend by 1–10 open days.")
    before = h.expires_at
    base = max(clock.to_local(h.expires_at).date(), clock.to_local(now).date()) + timedelta(days=1)
    h.expires_at = shop.nth_open_day_close(db, base, extra_days, h.machine.location_id)
    audit.record(db, actor, "hold.extend", "machine_hold", h.id, {"until": before}, {"until": h.expires_at})


def expire_due(db: Session, now: datetime | None = None) -> int:
    now = now or clock.now()
    n = 0
    for h in db.scalars(select(MachineHold).where(MachineHold.released_at.is_(None), MachineHold.expires_at <= now)):
        h.released_at, h.release_reason = now, "expired"
        audit.record(db, "system", "hold.expire", "machine_hold", h.id)
        notify.hold_expired(db, h)
        n += 1
    return n


def daily_reminders(db: Session, now: datetime | None = None) -> int:
    """One friendly email per OPEN day (after the first day), sent once the shop is open."""
    now = now or clock.now()
    today = clock.to_local(now).date()
    if not shop.is_open(db, now):
        return 0
    cfg = shop_settings.load(db)
    n = 0
    for h in db.scalars(select(MachineHold).where(MachineHold.released_at.is_(None), MachineHold.expires_at > now)):
        start_day = clock.to_local(h.started_at).date()
        if h.last_reminder_on == today or start_day == today:
            continue
        day = shop.business_day_number(db, start_day, today, h.machine.location_id)
        final = clock.to_local(h.expires_at).date() == today
        notify.hold_reminder(db, h, day, cap_days(cfg, h.kind), final)
        h.last_reminder_on = today
        n += 1
    return n


def closing_prompts(db: Session, now: datetime | None = None) -> int:
    """Shortly before closing, one email to everyone still on a machine: anything staying overnight?"""
    now = now or clock.now()
    cfg = shop_settings.load(db)
    w = shop.hours_for_date(db, clock.to_local(now).date())
    if not w or not (timedelta(0) < w[1] - now <= timedelta(minutes=cfg.closing_prompt_minutes)):
        return 0
    n = 0
    for s in db.scalars(select(ShopSession).where(ShopSession.status == "active")):
        if db.scalar(select(AuditLog.id).where(AuditLog.action == "closing.prompt", AuditLog.entity_id == str(s.id))):
            continue
        notify.closing_prompt(db, s)
        audit.record(db, "system", "closing.prompt", "session", s.id)
        n += 1
    return n
