"""Peer time-change requests.

How it works (humane by design, nobody can order anybody):
  1. Student B opens student A's booking and asks for the time B needs (e.g. B wants 1-3 PM, A has 2-5 PM).
  2. A is emailed. A can say NO in one tap (never shown to anyone), or say YES and pick where to go instead
     (e.g. 3-6 PM, or just keep the part that doesn't clash: 3-5 PM).
  3. On YES everything is applied in one step: A's booking moves, B's booking is created, and B, A and the
     supervisor on duty are all told. If anything stopped being valid in the meantime nothing changes.
There is no priority/urgency field, nothing reveals who is on which access list, and limits stop pestering.
"""
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.errors import UserError
from app.models import Approval, Reservation, Student, TimeChangeRequest
from app.services import access, audit, notify, reservations, shop
from app.services import settings as shop_settings

MIN_LEAD = timedelta(hours=2)


def _requester_approval(db: Session, requester: Student, machine_id: int, start: datetime, end: datetime, now: datetime):
    """The requester's first approval that would legally let them book this window (ignoring that it is taken)."""
    last: UserError | None = None
    for a in db.scalars(select(Approval).where(Approval.student_id == requester.id, Approval.status == "active")
                        .order_by(Approval.id.desc())):
        try:
            reservations.validate(db, requester, a.number, machine_id, start, end, now=now, waiver_accepted=True,
                                  ignore_overlap=True, contact_accepted=True)
            return a
        except UserError as e:
            last = e
    raise last or UserError("You need an approved project for this machine before you can ask for its time.",
                            code="approval_not_found")


def _only_target_in_the_way(db: Session, t: Reservation, s: datetime, e: datetime, now: datetime, requester_id: int) -> bool:
    """Free apart from the target's own booking (no other booking, no waiting-list hold, no machine hold)."""
    if reservations.overlapping(db, t.machine_id, s, e, exclude_id=t.id):
        return False
    return not reservations._holds_blocking(db, t.machine_id, s, e, now, requester_id)


def wanted_options(db: Session, requester: Student, t: Reservation, now: datetime, limit: int = 40) -> list[tuple[datetime, datetime]]:
    """Windows the requester may ask for: they overlap the target's booking, are otherwise free, and are legal for them."""
    cfg = shop_settings.load(db)
    slot = timedelta(minutes=cfg.slot_minutes)
    lvl = access.effective_level(db, requester)
    cap = max(1, min(8, int(access.session_cap_hours(db, lvl) * 60 // cfg.slot_minutes)))
    day = clock.to_local(t.start_at).date()
    grid = shop.slot_grid(db, day, cfg.slot_minutes, t.machine.location_id)
    out = []
    for s, _ in grid:
        if s >= t.end_at or s <= now or s < t.start_at - slot * (cap - 1):
            continue
        for n in range(1, cap + 1):
            e = s + slot * n
            if not (s < t.end_at and e > t.start_at):
                continue
            if not shop.within_hours(db, s, e, t.machine.location_id):
                break
            if not _only_target_in_the_way(db, t, s, e, now, requester.id):
                continue
            try:
                _requester_approval(db, requester, t.machine_id, s, e, now)
            except UserError:
                continue
            out.append((s, e))
            if len(out) >= limit:
                return out
    return out


def target_options(db: Session, t: Reservation, wanted: tuple[datetime, datetime], now: datetime, days: int = 3,
                   limit: int = 12) -> list[dict]:
    """Where the person asked could go instead. Trimmed options keep the part of their time that doesn't clash;
    moved options keep their full length. All are legal for THEM and free."""
    cfg = shop_settings.load(db)
    ws, we = wanted
    dur = t.end_at - t.start_at
    out: list[dict] = []

    def legal(s: datetime, e: datetime) -> bool:
        if s <= now or (s < we and e > ws):
            return False
        try:
            reservations.validate(db, t.student, t.approval.number if t.approval else None, t.machine_id, s, e, now=now,
                                  waiver_accepted=True, exclude_id=t.id, override=t.approval_id is None, contact_accepted=True)
            return True
        except UserError:
            return False
    left = (t.start_at, min(ws, t.end_at)) if ws > t.start_at else None
    right = (max(we, t.start_at), t.end_at) if we < t.end_at else None
    for part in (left, right):
        if part and part[1] > part[0] and legal(*part):
            out.append({"start": part[0], "end": part[1], "kind": "trim"})
    day0 = clock.to_local(now).date()
    moves = []
    for d in range(days + 1):
        for s, _ in shop.slot_grid(db, day0 + timedelta(days=d), cfg.slot_minutes, t.machine.location_id):
            e = s + dur
            if (s, e) in [(o["start"], o["end"]) for o in out] or (s == t.start_at and e == t.end_at):
                continue
            if legal(s, e):
                moves.append((s, e))
    moves.sort(key=lambda w: abs((w[0] - t.start_at).total_seconds()))
    out += [{"start": s, "end": e, "kind": "move"} for s, e in sorted(moves[:limit])]
    return out


def create(db: Session, requester: Student, target_res_id: int, wanted_start: datetime, wanted_end: datetime, note: str,
           now: datetime | None = None) -> TimeChangeRequest:
    now = now or clock.now()
    cfg = shop_settings.load(db)
    t = db.get(Reservation, target_res_id)
    if t is None or t.status != "confirmed":
        raise UserError("That booking can't be changed any more.", code="gone")
    if t.student_id == requester.id:
        raise UserError("That's your own booking.", code="own")
    if not t.student.allow_requests:
        raise UserError("That student prefers not to receive time-change requests. A shop supervisor can help.", code="muted")
    if t.start_at - now < MIN_LEAD:
        raise UserError("That time is too close to ask about now. A shop supervisor can help.", code="too_late")
    if (wanted_start, wanted_end) not in wanted_options(db, requester, t, now, limit=500):
        raise UserError("That time isn't available to ask for any more. Pick another.", code="bad_window")
    approval = _requester_approval(db, requester, t.machine_id, wanted_start, wanted_end, now)
    open_n = db.scalar(select(func.count()).select_from(TimeChangeRequest).where(
        TimeChangeRequest.requester_id == requester.id, TimeChangeRequest.status == "pending")) or 0
    if open_n >= cfg.max_open_change_requests:
        raise UserError(f"You have {open_n} requests waiting for answers. Give them time before asking more.", code="limit")
    if db.scalar(select(TimeChangeRequest.id).where(TimeChangeRequest.target_reservation_id == t.id,
                                                    TimeChangeRequest.status == "pending")):
        raise UserError("Someone has already asked about that slot. Please give them time to hear back.", code="dup")
    cool = now - timedelta(hours=cfg.change_request_cooldown_hours)
    if db.scalar(select(TimeChangeRequest.id).where(
            TimeChangeRequest.requester_id == requester.id, TimeChangeRequest.target_student_id == t.student_id,
            TimeChangeRequest.status == "declined", TimeChangeRequest.responded_at > cool)):
        raise UserError("You asked this student recently. Please give it a day, or ask a supervisor.", code="cooldown")
    req = TimeChangeRequest(requester_id=requester.id, target_reservation_id=t.id, target_student_id=t.student_id,
                            requester_approval_id=approval.id, wanted_start=wanted_start, wanted_end=wanted_end,
                            note=(note or "").strip()[:200] or None, created_at=now,
                            expires_at=min(now + timedelta(hours=cfg.change_request_hours), t.start_at - timedelta(hours=1)))
    db.add(req)
    db.flush()
    audit.record(db, f"student:{requester.email}", "change_request.create", "change_request", req.id,
                 after={"reservation": t.ref})
    req.requester, req.target_student, req.target_reservation = requester, t.student, t
    notify.change_requested(db, req)
    return req


def _mine(req: TimeChangeRequest, student: Student, as_target: bool) -> None:
    if (req.target_student_id if as_target else req.requester_id) != student.id:
        raise UserError("That request isn't yours.", code="not_yours")


def accept(db: Session, req: TimeChangeRequest, student: Student, new_start: datetime, new_end: datetime,
           now: datetime | None = None) -> Reservation:
    """The asked student agrees and picks where to go. One atomic step: they move, the asker's booking is created."""
    now = now or clock.now()
    _mine(req, student, True)
    if req.status != "pending" or req.expires_at <= now:
        raise UserError("This request is no longer open.", code="closed")
    t = req.target_reservation
    if t.status != "confirmed" or t.start_at - now < timedelta(minutes=30):
        raise UserError("Your booking has changed, so this request can't be applied.", code="gone")
    wanted = (req.wanted_start, req.wanted_end)
    if not any((o["start"], o["end"]) == (new_start, new_end) for o in target_options(db, t, wanted, now)):
        raise UserError("That new time isn't available any more. Please pick another from the list.", code="bad_choice")
    requester, approval = req.requester, db.get(Approval, req.requester_approval_id)
    old = (t.start_at, t.end_at)
    with db.begin_nested():  # all or nothing
        t.start_at, t.end_at = new_start, new_end
        db.flush()
        new_res = reservations.create(db, requester, approval.number, t.machine_id, wanted[0], wanted[1],
                                      waiver_accepted=True, contact_accepted=True, now=now)
        req.status, req.responded_at, req.new_start, req.new_end = "accepted", now, new_start, new_end
        db.flush()
    audit.record(db, f"student:{student.email}", "change_request.accept", "change_request", req.id,
                 {"start": old[0], "end": old[1]}, {"start": new_start, "end": new_end, "requester_ref": new_res.ref})
    notify.change_accepted(db, req, new_res, old)
    return new_res


def decline(db: Session, req: TimeChangeRequest, student: Student, now: datetime | None = None) -> None:
    now = now or clock.now()
    _mine(req, student, True)
    if req.status != "pending":
        raise UserError("This request is no longer open.", code="closed")
    req.status, req.responded_at = "declined", now
    audit.record(db, f"student:{student.email}", "change_request.decline", "change_request", req.id)
    notify.change_declined(db, req)


def cancel(db: Session, req: TimeChangeRequest, student: Student) -> None:
    _mine(req, student, False)
    if req.status != "pending":
        raise UserError("This request is no longer open.", code="closed")
    req.status, req.responded_at = "cancelled", clock.now()
    audit.record(db, f"student:{student.email}", "change_request.cancel", "change_request", req.id)


def expire_old(db: Session, now: datetime | None = None) -> int:
    now = now or clock.now()
    n = 0
    for req in db.scalars(select(TimeChangeRequest).where(TimeChangeRequest.status == "pending",
                                                          TimeChangeRequest.expires_at <= now)):
        req.status, req.responded_at = "expired", now
        n += 1
    return n
