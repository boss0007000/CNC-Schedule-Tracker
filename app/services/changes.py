"""Peer time-change requests.

Design rules (deliberately humane, no one gains 'power'):
  * a request is an ask, never an order: the other student accepts or declines in one tap, no reason needed;
  * a decline is never shown to anyone but the asker (as a neutral message) and never counts against anybody;
  * there is NO priority, urgency or seniority field, and nothing here reveals who is on which access list;
  * limits stop pestering: a few open requests each, one open ask per slot, a cooldown after a no, opt-out in My shop;
  * if the two cannot agree, a supervisor decides (existing staff tools), not students.
"""
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.errors import UserError
from app.models import Approval, Reservation, Student, TimeChangeRequest, WaitlistEntry
from app.services import audit, notify, reservations, shop
from app.services import settings as shop_settings

MIN_LEAD = timedelta(hours=2)
CLAIM_HOURS = 4.0


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


def candidate_windows(db: Session, target: Reservation, now: datetime, days: int = 3, limit: int = 12):
    """Where the other student could move to: same length, free, legal for THEM, not overlapping their old time."""
    cfg = shop_settings.load(db)
    dur = target.end_at - target.start_at
    step = timedelta(minutes=cfg.slot_minutes)
    out = []
    day0 = clock.to_local(now).date()
    for d in range(days + 1):
        for s, _ in shop.slot_grid(db, day0 + timedelta(days=d), cfg.slot_minutes, target.machine.location_id):
            e = s + dur
            if s < target.end_at and e > target.start_at:
                continue  # must not overlap the time being asked for
            try:
                reservations.validate(db, target.student, target.approval.number if target.approval else None,
                                      target.machine_id, s, e, now=now, waiver_accepted=True, exclude_id=target.id,
                                      override=target.approval_id is None, contact_accepted=True)
            except UserError:
                continue
            out.append((s, e))
    out.sort(key=lambda w: abs((w[0] - target.start_at).total_seconds()))
    return sorted(out[:limit])


def create(db: Session, requester: Student, target_res_id: int, proposed_start: datetime, note: str,
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
    proposed_end = proposed_start + (t.end_at - t.start_at)
    if (proposed_start, proposed_end) not in candidate_windows(db, t, now, days=5, limit=500):
        raise UserError("That new time isn't available for them any more. Pick another.", code="bad_window")
    approval = _requester_approval(db, requester, t.machine_id, t.start_at, t.end_at, now)
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
                            requester_approval_id=approval.id, proposed_start=proposed_start, proposed_end=proposed_end,
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


def accept(db: Session, req: TimeChangeRequest, student: Student, now: datetime | None = None) -> WaitlistEntry:
    now = now or clock.now()
    _mine(req, student, True)
    if req.status != "pending" or req.expires_at <= now:
        raise UserError("This request is no longer open.", code="closed")
    t = req.target_reservation
    if t.status != "confirmed" or t.start_at - now < timedelta(minutes=30):
        raise UserError("Your booking has changed, so this request can't be applied.", code="gone")
    old = (t.machine_id, t.start_at, t.end_at)
    requester = req.requester
    approval = db.get(Approval, req.requester_approval_id)
    # re-check both sides now (things may have changed since the request was sent)
    reservations.validate(db, student, t.approval.number if t.approval else None, t.machine_id, req.proposed_start,
                          req.proposed_end, now=now, waiver_accepted=True, exclude_id=t.id, override=t.approval_id is None,
                          contact_accepted=True)
    reservations.validate(db, requester, approval.number, t.machine_id, old[1], old[2], now=now, waiver_accepted=True,
                          ignore_overlap=True, contact_accepted=True)
    t.start_at, t.end_at = req.proposed_start, req.proposed_end
    db.flush()
    cfg = shop_settings.load(db)
    claim_for = min(timedelta(hours=CLAIM_HOURS), old[1] - now - timedelta(minutes=15))
    import secrets
    wl = WaitlistEntry(student_id=requester.id, approval_id=approval.id, machine_id=old[0], start_at=old[1], end_at=old[2],
                       status="offered", offered_at=now, claim_expires_at=now + max(claim_for, timedelta(minutes=30)),
                       claim_token=secrets.token_urlsafe(24))
    db.add(wl)
    req.status, req.responded_at = "accepted", now
    db.flush()
    audit.record(db, f"student:{student.email}", "change_request.accept", "change_request", req.id,
                 {"start": old[1]}, {"start": req.proposed_start})
    notify.change_accepted(db, req, wl.claim_token, max(claim_for, timedelta(minutes=30)).total_seconds() / 3600)
    return wl


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
