"""The ONE machine status function. Every endpoint, the TV and the admin board use `derive`.

Status is a pure function of timestamps + flags, so it is correct even if every background job
is asleep. Transition table (first matching rule wins):

  1. emergency close active ................................ Under Maintenance
  2. manual staff override ................................. the overridden status
  3. maintenance flag: under_maintenance / awaiting_inspection
  4. active session:  now > expected end ................... Overdue ; else In Use.
     Exception ("open-ended"): past the booked end but nobody is booked next and the shop is still open, the
     student may keep going: still In Use, never Overdue, until the next booking starts or the shop closes.
  4b. a setup / tools hold by a student (no active session) . On Hold (do not touch)
  5. confirmed reservation that starts within `reserved_lead` minutes (or already started and not
     past its late-arrival grace) ........................... Reserved
  6. otherwise ............................................. Available
"""
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.models import (
    MS_AVAILABLE, MS_HOLD, MS_INSPECTION, MS_IN_USE, MS_MAINTENANCE, MS_OVERDUE, MS_RESERVED, RES_CONFIRMED,
    Machine, MachineHold, Reservation, ShopSession,
)
from app.services import settings as shop_settings

LABELS = {
    MS_AVAILABLE: "Available", MS_RESERVED: "Reserved", MS_IN_USE: "In Use", MS_OVERDUE: "Overdue",
    MS_MAINTENANCE: "Under Maintenance", MS_INSPECTION: "Awaiting Inspection", MS_HOLD: "Do Not Touch",
}


@dataclass
class SessionInfo:
    student_name: str  # public form: "Alex R."
    started_at: datetime
    expected_end: datetime
    phone: str | None = None


@dataclass
class ResInfo:
    start: datetime
    end: datetime
    anchor: datetime  # late-arrival grace counts from here
    student_name: str
    phone: str | None = None


@dataclass
class HoldInfo:
    student_name: str
    kind: str  # setup | tools
    until: datetime
    phone: str | None = None


@dataclass
class StatusInputs:
    maintenance_state: str = "none"
    override_status: str | None = None
    override_reason: str | None = None
    emergency: bool = False
    session: SessionInfo | None = None
    reservations: list[ResInfo] = field(default_factory=list)  # confirmed, not yet checked in
    hold: HoldInfo | None = None
    close_at: datetime | None = None  # today's closing time (limits open-ended use)
    reserved_lead: timedelta = timedelta(minutes=30)
    grace: timedelta = timedelta(minutes=15)


@dataclass
class MachineState:
    status: str
    label: str
    current_user: str | None = None
    remaining_minutes: int | None = None
    overdue_minutes: int | None = None
    next_reservation_start: datetime | None = None
    estimated_available_at: datetime | None = None
    reason: str | None = None
    since: datetime | None = None  # session start (who's on the machine, and since when)
    current_phone: str | None = None
    next_user: str | None = None
    next_phone: str | None = None
    open_ended: bool = False  # past the booked end, but nobody is waiting
    hold_kind: str | None = None
    hold_until: datetime | None = None
    session_deadline: datetime | None = None  # when the session truly becomes overdue (booked end, or later if open-ended)


def _chain_end(end: datetime, res: list[ResInfo]) -> datetime:
    """Follow back-to-back reservations so 'available at' reflects the true free time."""
    for r in sorted(res, key=lambda r: r.start):
        if r.start <= end < r.end:
            end = r.end
    return end


def derive(inp: StatusInputs, now: datetime) -> MachineState:
    live = [r for r in inp.reservations if r.end > now and now <= r.anchor + inp.grace]
    live.sort(key=lambda r: r.start)
    future = [r for r in live if not (r.start <= now)]
    nxt = future[0].start if future else None
    nxt_user = future[0] if future else None

    def done(status, **kw) -> MachineState:
        return MachineState(status=status, label=LABELS[status], next_reservation_start=nxt,
                            next_user=nxt_user.student_name if nxt_user else None,
                            next_phone=nxt_user.phone if nxt_user else None, **kw)

    if inp.emergency:
        return done(MS_MAINTENANCE, reason="Emergency close")
    if inp.override_status:
        return done(inp.override_status, reason=inp.override_reason)
    if inp.maintenance_state == "under_maintenance":
        return done(MS_MAINTENANCE, reason="Under maintenance")
    if inp.maintenance_state == "awaiting_inspection":
        return done(MS_INSPECTION, reason="Awaiting inspection")
    if inp.session:
        s = inp.session
        if now > s.expected_end:
            # who is waiting? the earliest live booking (started or about to), else the shop closing time
            waiting = [r.start for r in live]
            limit = min(waiting + ([inp.close_at] if inp.close_at else []), default=None)
            deadline = max(s.expected_end, limit) if limit is not None else s.expected_end
            if limit is not None and now <= limit and inp.close_at and now <= inp.close_at:
                return done(MS_IN_USE, session_deadline=deadline, current_user=s.student_name, since=s.started_at, current_phone=s.phone,
                            open_ended=True, remaining_minutes=max(0, -(-int((limit - now).total_seconds()) // 60)),
                            estimated_available_at=limit, reason="Nobody is booked next: keep going until then")
            return done(MS_OVERDUE, session_deadline=deadline, current_user=s.student_name, since=s.started_at, current_phone=s.phone, remaining_minutes=0,
                        overdue_minutes=int((now - s.expected_end).total_seconds() // 60),
                        estimated_available_at=now)
        return done(MS_IN_USE, session_deadline=s.expected_end, current_user=s.student_name, since=s.started_at,
                    current_phone=s.phone, remaining_minutes=max(0, -(-int((s.expected_end - now).total_seconds()) // 60)),  # ceil
                    estimated_available_at=_chain_end(s.expected_end, inp.reservations))
    if inp.hold:
        h = inp.hold
        return done(MS_HOLD, current_user=h.student_name, current_phone=h.phone, hold_kind=h.kind, hold_until=h.until,
                    reason="Setup in the machine" if h.kind == "setup" else "Tools in the machine")
    imminent = [r for r in live if r.start - inp.reserved_lead <= now]
    if imminent:
        r = imminent[0]
        return done(MS_RESERVED, estimated_available_at=_chain_end(r.end, inp.reservations))
    return done(MS_AVAILABLE)


# --- DB loading -------------------------------------------------------------------------
def load_inputs(db: Session, machines: list[Machine], now: datetime | None = None) -> dict[int, StatusInputs]:
    """Build StatusInputs for many machines with 3 queries (used by TV/admin boards)."""
    now = now or clock.now()
    cfg = shop_settings.load(db)
    ids = [m.id for m in machines]
    sessions = {s.machine_id: s for s in db.scalars(
        select(ShopSession).where(ShopSession.machine_id.in_(ids), ShopSession.status == "active"))}
    by_machine: dict[int, list[ResInfo]] = {i: [] for i in ids}
    horizon = now + timedelta(days=2)
    for r in db.scalars(select(Reservation).where(Reservation.machine_id.in_(ids),
                                                  Reservation.status == RES_CONFIRMED, Reservation.end_at > now,
                                                  Reservation.start_at < horizon).order_by(Reservation.start_at)):
        by_machine[r.machine_id].append(ResInfo(r.start_at, r.end_at, r.grace_anchor, r.student.public_name, r.student.phone))
    holds = {h.machine_id: h for h in db.scalars(select(MachineHold).where(
        MachineHold.machine_id.in_(ids), MachineHold.released_at.is_(None), MachineHold.expires_at > now))}
    from app.services import shop
    today_hours = shop.hours_for_date(db, clock.to_local(now).date())
    close_at = today_hours[1] if today_hours else None
    out = {}
    for m in machines:
        s = sessions.get(m.id)
        out[m.id] = StatusInputs(
            maintenance_state=m.maintenance_state, override_status=m.override_status,
            override_reason=m.override_reason, emergency=bool(cfg.emergency_active),
            session=SessionInfo(s.student.public_name, s.checked_in_at, s.expected_end_at, s.student.phone) if s else None,
            hold=(HoldInfo(holds[m.id].student.public_name, holds[m.id].kind, holds[m.id].expires_at, holds[m.id].student.phone)
                  if m.id in holds else None), close_at=close_at,
            reservations=by_machine[m.id], reserved_lead=timedelta(minutes=cfg.reserved_lead_minutes),
            grace=timedelta(minutes=cfg.checkin_grace_minutes))
    return out


def machine_state(db: Session, machine: Machine, now: datetime | None = None) -> MachineState:
    now = now or clock.now()
    return derive(load_inputs(db, [machine], now)[machine.id], now)


def board(db: Session, now: datetime | None = None, include_inactive: bool = False) -> list[tuple[Machine, MachineState]]:
    now = now or clock.now()
    stmt = select(Machine).order_by(Machine.sort_order, Machine.name)
    if not include_inactive:
        stmt = stmt.where(Machine.active.is_(True))
    machines = list(db.scalars(stmt))
    inputs = load_inputs(db, machines, now)
    return [(m, derive(inputs[m.id], now)) for m in machines]


def session_deadline(db: Session, session: ShopSession, now: datetime) -> datetime:
    """When a session really becomes overdue: its booked end, or later if the student may keep going."""
    st = machine_state(db, session.machine, now)
    return st.session_deadline or session.expected_end_at
