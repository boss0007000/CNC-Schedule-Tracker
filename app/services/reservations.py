"""Reservation rules. Every rule lives here (the server is the single source of truth).

`validate()` raises a UserError with a specific, human-readable message for the first rule that fails,
in the order the spec lists them: approval -> machine coverage -> certification -> waiver -> slot rules
-> fairness rules. It never writes; `create()` validates then writes."""
import secrets
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.errors import UserError
from app.models import (
    RES_ACTIVE, RES_CANCELLED, RES_CHECKED_IN, RES_CONFIRMED, RES_COUNTED, Approval, Certification, Machine,
    MaintenanceRecord, Reservation, SafetyWaiver, ShopSession, Student, WaitlistEntry,
)
from app.services import access, audit, holds, notify, numbers, shop
from app.services import settings as shop_settings


def fmt(dt: datetime) -> str:
    return clock.to_local(dt).strftime("%a %b %d, %I:%M %p").replace(" 0", " ")


def fmt_time(dt: datetime) -> str:
    return clock.to_local(dt).strftime("%I:%M %p").lstrip("0")


# ----------------------------------------------------------------------------- helpers
def find_approval(db: Session, student: Student, number: str) -> Approval:
    a = db.scalar(select(Approval).where(Approval.number == numbers.normalize_approval(number)))
    if a is None or a.student_id != student.id:  # same message either way: don't reveal other students' numbers
        raise UserError("We couldn't find that approval number under your email. Check the number on your "
                        "approval email (it looks like APR-2026-0042).", code="approval_not_found", field="approval")
    return a


def approval_usage(db: Session, a: Approval, exclude_id: int | None = None) -> tuple[float, int]:
    q = select(Reservation).where(Reservation.approval_id == a.id, Reservation.status.in_(RES_COUNTED))
    if exclude_id:
        q = q.where(Reservation.id != exclude_id)
    rows = db.scalars(q).all()
    return sum(r.hours for r in rows), len(rows)


def machine_names(db: Session, ids: list[int]) -> str:
    return ", ".join(db.scalars(select(Machine.name).where(Machine.id.in_(ids)).order_by(Machine.name))) or "none"


def find_certification(db: Session, student_id: int, machine: Machine, at: datetime) -> tuple[Certification | None, Certification | None]:
    """(valid cert, most recent expired cert) for the machine or its group."""
    cond = (Certification.machine_id == machine.id)
    if machine.group_id:
        cond = cond | (Certification.group_id == machine.group_id)
    certs = db.scalars(select(Certification).where(Certification.student_id == student_id,
                                                   Certification.revoked_at.is_(None), cond)
                       .order_by(Certification.expires_at.desc().nulls_first())).all()
    valid = next((c for c in certs if c.expires_at is None or c.expires_at > at), None)
    expired = certs[0] if certs and valid is None else None
    return valid, expired


def has_waiver(db: Session, student_id: int, version: str) -> bool:
    return db.scalar(select(SafetyWaiver.id).where(SafetyWaiver.student_id == student_id,
                                                   SafetyWaiver.version == version)) is not None


def sign_waiver(db: Session, student: Student, ip: str | None) -> None:
    version = shop_settings.load(db).waiver_version
    if not has_waiver(db, student.id, version):
        db.add(SafetyWaiver(student_id=student.id, version=version, ip=ip))
        db.flush()
        audit.record(db, f"student:{student.email}", "waiver.sign", "student", student.id, after={"version": version}, ip=ip)


def _week_bounds(start: datetime) -> tuple[datetime, datetime]:
    d = clock.to_local(start).date()
    monday = d - timedelta(days=d.weekday())
    return clock.local_dt(monday, datetime.min.time()), clock.local_dt(monday + timedelta(days=7), datetime.min.time())


def _holds_blocking(db: Session, machine_id: int, start: datetime, end: datetime, now: datetime,
                    student_id: int | None) -> bool:
    """A waitlist offer holds its slot for the offered student until the claim window closes."""
    q = select(WaitlistEntry.id).where(WaitlistEntry.machine_id == machine_id, WaitlistEntry.status == "offered",
                                       WaitlistEntry.claim_expires_at > now, WaitlistEntry.start_at < end,
                                       WaitlistEntry.end_at > start)
    if student_id:
        q = q.where(WaitlistEntry.student_id != student_id)
    return db.scalar(q.limit(1)) is not None


def overlapping(db: Session, machine_id: int, start: datetime, end: datetime, exclude_id: int | None = None) -> list[Reservation]:
    q = select(Reservation).where(Reservation.machine_id == machine_id, Reservation.status.in_(RES_ACTIVE),
                                  Reservation.start_at < end, Reservation.end_at > start)
    if exclude_id:
        q = q.where(Reservation.id != exclude_id)
    return list(db.scalars(q))


# ----------------------------------------------------------------------------- validation
@dataclass
class Ctx:
    approval: Approval | None
    machine: Machine
    needs_waiver: bool = False


def validate(db: Session, student: Student, approval_number: str | None, machine_id: int, start: datetime,
             end: datetime, *, now: datetime | None = None, waiver_accepted: bool = False,
             ignore_overlap: bool = False, exclude_id: int | None = None, allow_started: bool = False,
             override: bool = False, contact_accepted: bool = False) -> Ctx:
    now = now or clock.now()
    cfg = shop_settings.load(db)
    machine = db.get(Machine, machine_id)
    if machine is None or not machine.active:
        raise UserError("That machine isn't available for booking.", code="machine_unavailable", field="machine")
    if end <= start:
        raise UserError("The end time must be after the start time.", code="bad_window")
    slot = timedelta(minutes=cfg.slot_minutes)
    n = (end - start) / slot
    if n != int(n) or int(n) < 1:
        raise UserError("Reservations are booked in whole time slots.", code="bad_window")
    grid = {s for s, _ in shop.slot_grid(db, clock.to_local(start).date(), cfg.slot_minutes, machine.location_id)}
    cur, aligned = start, True
    while cur < end:
        aligned &= cur in grid
        cur += slot
    approval = None
    if override:  # staff bookings still can't exceed the longest session anyone is allowed
        cap_h = access.session_cap_hours(db, "super_approved")
        if (end - start).total_seconds() / 3600 > cap_h + 1e-9:
            raise UserError(f"A single session can be at most {cap_h:g} hours.", code="too_long")

    if not override:
        # 1. approval
        approval = find_approval(db, student, approval_number or "")
        if approval.status != "active":
            raise UserError(f"Approval {approval.number} has been revoked. Contact shop staff.", code="approval_revoked")
        if approval.expires_at and approval.expires_at < end:
            when = clock.to_local(approval.expires_at).strftime("%b %d, %Y")
            raise UserError(f"Approval {approval.number} {'expired' if approval.expires_at < now else 'expires'} on {when}, "
                            f"which is before this time slot.", code="approval_expired")
        used_h, used_n = approval_usage(db, approval, exclude_id)
        if approval.max_total_hours is not None and used_h + (end - start).total_seconds() / 3600 > approval.max_total_hours + 1e-9:
            raise UserError(f"Approval {approval.number} allows {approval.max_total_hours:g} total hours and "
                            f"{used_h:g} are already booked or used.", code="approval_exhausted")
        if approval.max_sessions is not None and used_n + (0 if exclude_id else 1) > approval.max_sessions:
            raise UserError(f"Approval {approval.number} allows {approval.max_sessions} sessions and all are used.",
                            code="approval_exhausted")
        # 2. machine coverage
        if machine.id not in approval.machine_ids:
            raise UserError(f"Your approval doesn't cover the {machine.name}. Covered machines: "
                            f"{machine_names(db, approval.machine_ids)}.", code="not_covered", field="machine")
        # 2b. access: on a list, and the list the machine needs (staff-only machines can't be booked)
        level = access.check_access(db, student, machine)
        cap_h = access.session_cap_hours(db, level)
        if (end - start).total_seconds() / 3600 > cap_h + 1e-9:
            raise UserError(f"A single session can be at most {cap_h:g} hours. Book a second session after it if you "
                            f"need more.", code="too_long")
        # 3. certification (hard block)
        # Being on the access list that allows the machine IS the authorization. Individual certification records are only
        # enforced when the shop turns on `require_certifications` (or when the access list requirement is switched off).
        if machine.requires_certification and (cfg.require_certifications or not cfg.require_access_list):
            valid, expired = find_certification(db, student.id, machine, end)
            if valid is None:
                grp = f" or {machine.group.name}" if machine.group else ""
                if expired and expired.expires_at:
                    raise UserError(f"Your certification for the {machine.name}{grp} expires/expired on "
                                    f"{clock.to_local(expired.expires_at).strftime('%b %d, %Y')}, before this slot. "
                                    f"Ask shop staff to renew it.", code="cert_expired", field="machine")
                raise UserError(f"You need a current certification for the {machine.name}{grp} before you can "
                                f"reserve it. Ask shop staff about training.", code="cert_missing", field="machine")
        # 4. safety waiver
        if not has_waiver(db, student.id, cfg.waiver_version) and not waiver_accepted:
            raise UserError("Please read and accept the safety waiver to continue.", code="waiver_required")

    # 4b. a phone number is required to reserve: other students can call about the machine
    if not override and not contact_accepted and not (student.phone and student.phone_ack_at):
        raise UserError("Please add your phone number. It is shown to other students on the schedule page and the shop TV "
                        "while you have a booking, so they can reach you about the machine.", code="phone_required",
                        field="phone")

    # 5. slot rules
    if (not allow_started and not override and start <= now) or end <= now:
        raise UserError("That time has already started or passed. Pick a later slot.", code="past")
    if cfg.emergency_active:
        raise UserError("The shop is closed for an emergency right now. Please check back later.", code="emergency")
    if not shop.within_hours(db, start, end, machine.location_id) or not aligned:
        hrs = shop.day_label(db, clock.to_local(start).date(), machine.location_id)
        raise UserError(f"The shop isn't open for that time slot on {clock.to_local(start).strftime('%a %b %d')} "
                        f"(hours: {hrs}).", code="closed", field="start")
    if machine.requires_supervision and not shop.supervisor_covers(db, start, end, machine.location_id):
        raise UserError(f"The {machine.name} needs a supervisor on duty and none is scheduled for that time.",
                        code="no_supervisor", field="start")
    if machine.maintenance_state == "under_maintenance":
        raise UserError(f"The {machine.name} is out of service for maintenance right now.", code="maintenance")
    blocked = db.scalar(select(MaintenanceRecord.id).where(
        MaintenanceRecord.machine_id == machine.id, MaintenanceRecord.closed_at.is_(None),
        MaintenanceRecord.window_start.is_not(None), MaintenanceRecord.window_start < end,
        MaintenanceRecord.window_end > start).limit(1))
    if blocked:
        raise UserError(f"The {machine.name} has scheduled maintenance during that time.", code="maintenance")
    hold = holds.active_hold(db, machine.id, now)
    if hold and not override and hold.student_id != student.id and start < hold.expires_at:
        raise UserError(f"The {machine.name} has {hold.student.public_name}'s "
                        f"{'setup' if hold.kind == 'setup' else 'tools'} in it (please don't touch it) until {fmt(hold.expires_at)}. "
                        f"Pick another machine or a time after that, or call them from the schedule page to ask.",
                        code="machine_on_hold")
    if not ignore_overlap and (overlapping(db, machine.id, start, end, exclude_id)
                               or _holds_blocking(db, machine.id, start, end, now, student.id)):
        raise UserError("That time slot is already taken.", code="overlap", field="start")

    # 6. fairness
    if not override:
        if student.suspended_until and student.suspended_until > now:
            raise UserError(f"Your booking privileges are paused until {fmt(student.suspended_until)}"
                            f"{(' (' + student.suspension_reason + ')') if student.suspension_reason else ''}. "
                            f"Contact shop staff if you think this is a mistake.", code="suspended")
        today = clock.to_local(now).date()
        last_day = today + timedelta(days=cfg.max_advance_days)
        if clock.to_local(start).date() > last_day:
            raise UserError(f"You can book at most {cfg.max_advance_days} days ahead (up to {last_day.strftime('%b %d')}).",
                            code="too_far", field="date")
        wk_s, wk_e = _week_bounds(start)
        week = db.scalars(select(Reservation).where(
            Reservation.student_id == student.id, Reservation.status.in_(RES_COUNTED),
            Reservation.start_at >= wk_s, Reservation.start_at < wk_e,
            *([Reservation.id != exclude_id] if exclude_id else []))).all()
        if cfg.max_reservations_per_week and len(week) + (0 if exclude_id else 1) > cfg.max_reservations_per_week:
            raise UserError(f"You can have at most {cfg.max_reservations_per_week} reservations per week, and you "
                            f"already have {len(week)} that week.", code="weekly_count")
        req_h = (end - start).total_seconds() / 3600
        if cfg.max_hours_per_week and sum(r.hours for r in week) + req_h > cfg.max_hours_per_week + 1e-9:
            raise UserError(f"This would put you over the weekly limit of {cfg.max_hours_per_week:g} hours "
                            f"({sum(r.hours for r in week):g} already booked that week).", code="weekly_hours")
        grp_cond = (Machine.group_id == machine.group_id) if machine.group_id else (Machine.id == machine.id)
        active = db.scalars(select(Reservation).join(Machine, Machine.id == Reservation.machine_id).where(
            Reservation.student_id == student.id, Reservation.status.in_(RES_ACTIVE), Reservation.end_at > now,
            grp_cond, *([Reservation.id != exclude_id] if exclude_id else []))).all()
        if len(active) >= cfg.max_active_per_group:
            r0 = active[0]
            gname = machine.group.name if machine.group else machine.name
            raise UserError(f"You already have an active reservation in {gname} ({r0.ref}, {fmt(r0.start_at)}). "
                            f"Use it or cancel it before booking another.", code="group_limit")
    return Ctx(approval, machine, needs_waiver=not override and not has_waiver(db, student.id, cfg.waiver_version))


# ----------------------------------------------------------------------------- slots for the UI
def slots_for(db: Session, student: Student, approval_number: str, machine_id: int, d: date,
              now: datetime | None = None) -> dict:
    """Only genuinely bookable slots are 'open'. 'taken' slots are listed (for the waitlist) only when the
    student would otherwise be allowed to book them."""
    now = now or clock.now()
    cfg = shop_settings.load(db)
    machine = db.get(Machine, machine_id)
    out = {"error": None, "needs_waiver": False, "slots": [], "code": None}
    if machine is None:
        out["error"] = "Choose a machine."
        return out
    grid = shop.slot_grid(db, d, cfg.slot_minutes, machine.location_id)
    if not grid:
        out["error"], out["code"] = f"The shop is closed on {d.strftime('%A %b %d')}.", "closed"
        return out
    first_error = None
    reasons: dict[str, UserError] = {}
    counts: dict[str, int] = {}
    for s, e in grid:
        try:
            ctx = validate(db, student, approval_number, machine_id, s, e, now=now, waiver_accepted=True,
                           contact_accepted=True)
            out["slots"].append({"start": s, "end": e, "state": "open"})
            out["needs_waiver"] = ctx.needs_waiver
        except UserError as ex:
            if ex.code == "overlap":
                try:
                    validate(db, student, approval_number, machine_id, s, e, now=now, waiver_accepted=True,
                             ignore_overlap=True, contact_accepted=True)
                    out["slots"].append({"start": s, "end": e, "state": "taken"})
                except UserError:
                    pass
            elif ex.code in ("approval_not_found", "approval_revoked", "approval_expired", "approval_exhausted",
                             "not_covered", "machine_on_hold", "not_listed", "needs_more_access", "staff_only", "too_long", "cert_missing", "cert_expired", "suspended", "too_far", "weekly_count",
                             "weekly_hours", "group_limit", "machine_unavailable", "maintenance", "emergency"):
                first_error = first_error or ex
            if ex.code not in ("overlap", "past"):
                reasons.setdefault(ex.code, ex)
                counts[ex.code] = counts.get(ex.code, 0) + 1
    if not any(s["state"] == "open" for s in out["slots"]) and first_error:
        out["error"], out["code"] = first_error.message, first_error.code
    elif not out["slots"] and counts:
        code_ = max(counts, key=counts.get)  # the most common reason every slot failed
        out["error"], out["code"] = reasons[code_].message, code_
        if code_ == "no_supervisor":
            out["error"] += " Ask a shop admin to add a supervisor shift (Settings > Supervisor shifts)."
    elif not out["slots"]:
        when = "today" if d == clock.to_local(now).date() else d.strftime("%A %b %d")
        out["error"], out["code"] = (f"There are no open times left {when}. Try another date.", "none_left")
    out["needs_contact"] = bool(out["slots"]) and not (student.phone and student.phone_ack_at)
    if out["slots"] and not out["needs_waiver"]:
        out["needs_waiver"] = not has_waiver(db, student.id, cfg.waiver_version)
    return out


# ----------------------------------------------------------------------------- writes
def create(db: Session, student: Student, approval_number: str | None, machine_id: int, start: datetime,
           end: datetime, *, waiver_accepted: bool = False, ip: str | None = None, now: datetime | None = None,
           actor: str | None = None, override: bool = False, allow_started: bool = False) -> Reservation:
    now = now or clock.now()
    # serialize bookings per machine (row lock on Postgres; SQLite serializes writers anyway)
    db.execute(select(Machine.id).where(Machine.id == machine_id).with_for_update())
    ctx = validate(db, student, approval_number, machine_id, start, end, now=now, waiver_accepted=waiver_accepted,
                   override=override, allow_started=allow_started)
    if waiver_accepted and not override:
        sign_waiver(db, student, ip)
    r = Reservation(ref=numbers.new_reservation_ref(db), student_id=student.id,
                    approval_id=ctx.approval.id if ctx.approval else None, machine_id=machine_id,
                    start_at=start, end_at=end, status=RES_CONFIRMED, manual=bool(actor), created_at=now)
    db.add(r)
    db.flush()
    r.student, r.machine = student, ctx.machine
    audit.record(db, actor or f"student:{student.email}", "reservation.create", "reservation", r.id,
                 after={"ref": r.ref, "machine": ctx.machine.name, "start": start, "end": end, "override": override},
                 ip=ip)
    notify.reservation_confirmed(db, r)
    return r


def cancel(db: Session, r: Reservation, *, actor: str, by_staff: bool = False, reason: str = "",
           now: datetime | None = None, quiet: bool = False) -> None:
    now = now or clock.now()
    if r.status != RES_CONFIRMED:
        raise UserError(f"Reservation {r.ref} is {r.status.replace('_', ' ')} and can't be cancelled.", code="not_cancellable")
    cfg = shop_settings.load(db)
    if not by_staff:
        cutoff = r.start_at - timedelta(minutes=cfg.cancel_cutoff_minutes)
        if now > cutoff:
            raise UserError(f"Reservations can only be cancelled until {cfg.cancel_cutoff_minutes} minutes before they "
                            f"start. Please contact shop staff.", code="cutoff")
    r.status, r.cancelled_at, r.cancel_reason = RES_CANCELLED, now, reason or None
    audit.record(db, actor, "reservation.cancel", "reservation", r.id, {"status": RES_CONFIRMED},
                 {"status": RES_CANCELLED, "reason": reason})
    if not quiet:
        notify.reservation_cancelled(db, r, by_staff, reason)
    promote_waitlist(db, r.machine_id, r.start_at, r.end_at, now)
    from app.services import sessions
    sessions.notify_next_up(db, r.machine_id, now)


def extend(db: Session, r: Reservation, student: Student, slots: int = 1, *, now: datetime | None = None,
           actor: str | None = None) -> Reservation:
    now = now or clock.now()
    if r.student_id != student.id and not actor:
        raise UserError("That reservation belongs to someone else.")
    if r.status not in RES_ACTIVE or r.end_at <= now:
        raise UserError("Only a current or upcoming reservation can be extended.", code="not_extendable")
    cfg = shop_settings.load(db)
    new_end = r.end_at + timedelta(minutes=cfg.slot_minutes * max(1, slots))
    db.execute(select(Machine.id).where(Machine.id == r.machine_id).with_for_update())
    try:
        validate(db, student, r.approval.number if r.approval else None, r.machine_id, r.start_at, new_end, now=now,
                 exclude_id=r.id, allow_started=True, waiver_accepted=True, override=r.approval_id is None,
                 contact_accepted=True)
    except UserError as e:
        if e.code == "overlap":
            raise UserError("The next time slot is already booked, so this reservation can't be extended.",
                            code="overlap") from None
        raise
    before = r.end_at
    r.end_at = new_end
    sess = db.scalar(select(ShopSession).where(ShopSession.reservation_id == r.id, ShopSession.status == "active"))
    if sess:
        sess.expected_end_at = new_end
        sess.overdue_notified_at = None
    audit.record(db, actor or f"student:{student.email}", "reservation.extend", "reservation", r.id,
                 {"end": before}, {"end": new_end})
    notify.reservation_confirmed(db, r, extended=True)
    return r


# ----------------------------------------------------------------------------- waitlist
def join_waitlist(db: Session, student: Student, approval_number: str, machine_id: int, start: datetime,
                  end: datetime, *, now: datetime | None = None, waiver_accepted: bool = False,
                  ip: str | None = None) -> WaitlistEntry:
    now = now or clock.now()
    ctx = validate(db, student, approval_number, machine_id, start, end, now=now, waiver_accepted=waiver_accepted,
                   ignore_overlap=True)
    if waiver_accepted:
        sign_waiver(db, student, ip)
    if not (overlapping(db, machine_id, start, end) or _holds_blocking(db, machine_id, start, end, now, student.id)):
        raise UserError("That slot is open now, so you can book it directly instead of joining the waiting list.",
                        code="not_full")
    dup = db.scalar(select(WaitlistEntry.id).where(WaitlistEntry.student_id == student.id,
                                                   WaitlistEntry.machine_id == machine_id,
                                                   WaitlistEntry.start_at == start,
                                                   WaitlistEntry.status.in_(("waiting", "offered"))))
    if dup:
        raise UserError("You're already on the waiting list for that slot.", code="dup")
    open_n = db.scalar(select(func.count()).select_from(WaitlistEntry).where(
        WaitlistEntry.student_id == student.id, WaitlistEntry.status.in_(("waiting", "offered")))) or 0
    if open_n >= 3:
        raise UserError("You can be on at most 3 waiting lists at once.", code="waitlist_limit")
    w = WaitlistEntry(student_id=student.id, approval_id=ctx.approval.id, machine_id=machine_id, start_at=start,
                      end_at=end, created_at=now)
    db.add(w)
    db.flush()
    audit.record(db, f"student:{student.email}", "waitlist.join", "waitlist", w.id,
                 after={"machine": ctx.machine.name, "start": start})
    return w


def promote_waitlist(db: Session, machine_id: int, start: datetime, end: datetime, now: datetime | None = None) -> WaitlistEntry | None:
    """Offer a freed window to the earliest waiting student who can still legally book it."""
    now = now or clock.now()
    cfg = shop_settings.load(db)
    cands = db.scalars(select(WaitlistEntry).where(
        WaitlistEntry.machine_id == machine_id, WaitlistEntry.status == "waiting", WaitlistEntry.start_at < end,
        WaitlistEntry.end_at > start, WaitlistEntry.end_at > now).order_by(WaitlistEntry.created_at, WaitlistEntry.id)).all()
    for w in cands:
        approval = db.get(Approval, w.approval_id)
        try:
            validate(db, w.student, approval.number, machine_id, w.start_at, w.end_at, now=now, waiver_accepted=True,
                     allow_started=w.start_at <= now, contact_accepted=True)
        except UserError:
            continue  # no longer eligible or still blocked: try the next student
        w.status, w.offered_at = "offered", now
        w.claim_expires_at = now + timedelta(minutes=cfg.waitlist_claim_minutes)
        w.claim_token = secrets.token_urlsafe(24)
        audit.record(db, "system", "waitlist.offer", "waitlist", w.id, after={"student": w.student.email})
        notify.waitlist_offer(db, w.student, w.machine.name, w.start_at, w.end_at, w.claim_token,
                              cfg.waitlist_claim_minutes)
        return w
    return None


def expire_offers(db: Session, now: datetime | None = None) -> int:
    """Claim windows that lapsed pass the slot to the next student in line."""
    now = now or clock.now()
    n = 0
    for w in db.scalars(select(WaitlistEntry).where(WaitlistEntry.status == "offered",
                                                    WaitlistEntry.claim_expires_at <= now)).all():
        w.status = "expired"
        audit.record(db, "system", "waitlist.expire", "waitlist", w.id)
        n += 1
        promote_waitlist(db, w.machine_id, w.start_at, w.end_at, now)
    return n


def claim(db: Session, token: str, student: Student, *, ip: str | None = None, now: datetime | None = None) -> Reservation:
    now = now or clock.now()
    w = db.scalar(select(WaitlistEntry).where(WaitlistEntry.claim_token == token))
    if w is None or w.student_id != student.id:
        raise UserError("That claim link isn't valid for your account.", code="bad_claim")
    if w.status == "claimed":
        raise UserError("You've already claimed this slot.", code="claimed")
    if w.status != "offered" or (w.claim_expires_at and w.claim_expires_at <= now):
        raise UserError("Sorry, the claim window for that slot has closed.", code="claim_expired")
    approval = db.get(Approval, w.approval_id)
    r = create(db, student, approval.number, w.machine_id, w.start_at, w.end_at, waiver_accepted=True, ip=ip,
               now=now, allow_started=w.start_at <= now)
    w.status = "claimed"
    audit.record(db, f"student:{student.email}", "waitlist.claim", "waitlist", w.id, after={"ref": r.ref})
    return r


def cancel_waitlist(db: Session, w: WaitlistEntry, student: Student) -> None:
    if w.student_id != student.id:
        raise UserError("That entry belongs to someone else.")
    was_offered = w.status == "offered"
    w.status = "cancelled"
    if was_offered:
        promote_waitlist(db, w.machine_id, w.start_at, w.end_at)
