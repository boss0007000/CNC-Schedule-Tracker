"""The ONE machine status function. Every endpoint, the TV and the admin board use `derive`.

Status is a pure function of timestamps + flags, so it is correct even if every background job
is asleep. Transition table (first matching rule wins):

  1. emergency close active ................................ Under Maintenance
  2. manual staff override ................................. the overridden status
  3. maintenance flag: under_maintenance / awaiting_inspection
  4. active session:  now > expected end ................... Overdue ; else In Use
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
    MS_AVAILABLE, MS_INSPECTION, MS_IN_USE, MS_MAINTENANCE, MS_OVERDUE, MS_RESERVED, RES_CONFIRMED,
    Machine, Reservation, ShopSession,
)
from app.services import settings as shop_settings

LABELS = {
    MS_AVAILABLE: "Available", MS_RESERVED: "Reserved", MS_IN_USE: "In Use", MS_OVERDUE: "Overdue",
    MS_MAINTENANCE: "Under Maintenance", MS_INSPECTION: "Awaiting Inspection",
}


@dataclass
class SessionInfo:
    student_name: str  # public form: "Alex R."
    started_at: datetime
    expected_end: datetime


@dataclass
class ResInfo:
    start: datetime
    end: datetime
    anchor: datetime  # late-arrival grace counts from here
    student_name: str


@dataclass
class StatusInputs:
    maintenance_state: str = "none"
    override_status: str | None = None
    override_reason: str | None = None
    emergency: bool = False
    session: SessionInfo | None = None
    reservations: list[ResInfo] = field(default_factory=list)  # confirmed, not yet checked in
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

    def done(status, **kw) -> MachineState:
        return MachineState(status=status, label=LABELS[status], next_reservation_start=nxt, **kw)

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
            return done(MS_OVERDUE, current_user=s.student_name, since=s.started_at, remaining_minutes=0,
                        overdue_minutes=int((now - s.expected_end).total_seconds() // 60),
                        estimated_available_at=now)
        return done(MS_IN_USE, current_user=s.student_name, since=s.started_at,
                    remaining_minutes=max(0, -(-int((s.expected_end - now).total_seconds()) // 60)),  # ceil
                    estimated_available_at=_chain_end(s.expected_end, inp.reservations))
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
        by_machine[r.machine_id].append(ResInfo(r.start_at, r.end_at, r.grace_anchor, r.student.public_name))
    out = {}
    for m in machines:
        s = sessions.get(m.id)
        out[m.id] = StatusInputs(
            maintenance_state=m.maintenance_state, override_status=m.override_status,
            override_reason=m.override_reason, emergency=bool(cfg.emergency_active),
            session=SessionInfo(s.student.public_name, s.checked_in_at, s.expected_end_at) if s else None,
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
