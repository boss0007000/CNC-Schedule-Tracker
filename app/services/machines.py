"""Machine administration: CRUD, groups, status override, maintenance log, inspection clearance."""
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.errors import UserError
from app.models import MACHINE_STATUSES, Machine, MachineGroup, MaintenanceRecord
from app.services import audit

FIELDS = ["name", "group_id", "description", "requires_certification", "requires_supervision", "active", "sort_order"]


def save_group(db: Session, actor: str, name: str, group: MachineGroup | None = None) -> MachineGroup:
    name = " ".join(name.split())
    if not 2 <= len(name) <= 80:
        raise UserError("Group name must be 2–80 characters.")
    clash = db.scalar(select(MachineGroup).where(func.lower(MachineGroup.name) == name.lower()))
    if clash and (group is None or clash.id != group.id):
        raise UserError("A group with that name already exists.")
    before = {"name": group.name} if group else None
    if group is None:
        group = MachineGroup(name=name)
        db.add(group)
    else:
        group.name = name
    db.flush()
    audit.record(db, actor, "group.save", "machine_group", group.id, before, {"name": name})
    return group


def save_machine(db: Session, actor: str, machine: Machine | None, data: dict) -> Machine:
    name = " ".join(str(data.get("name", "")).split())
    if not 2 <= len(name) <= 120:
        raise UserError("Machine name must be 2–120 characters.", field="name")
    clash = db.scalar(select(Machine).where(func.lower(Machine.name) == name.lower()))
    if clash and (machine is None or clash.id != machine.id):
        raise UserError("A machine with that name already exists.", field="name")
    gid = int(data["group_id"]) if str(data.get("group_id") or "").isdigit() else None
    if gid and db.get(MachineGroup, gid) is None:
        raise UserError("Unknown group.")
    try:
        order = int(data.get("sort_order") or 0)
    except ValueError:
        raise UserError("Sort order must be a whole number.") from None
    vals = {"name": name, "group_id": gid, "description": (data.get("description") or "").strip()[:255] or None,
            "requires_certification": bool(data.get("requires_certification")),
            "requires_supervision": bool(data.get("requires_supervision")),
            "active": bool(data.get("active", True)), "sort_order": order}
    before = audit.snapshot(machine, FIELDS) if machine else None
    if machine is None:
        machine = Machine(location_id=1, **vals)
        db.add(machine)
    else:
        for k, v in vals.items():
            setattr(machine, k, v)
    db.flush()
    audit.record(db, actor, "machine.create" if before is None else "machine.update", "machine", machine.id,
                 before, audit.snapshot(machine, FIELDS))
    return machine


def set_override(db: Session, actor: str, machine: Machine, status: str | None, reason: str) -> None:
    status = status or None
    if status and status not in MACHINE_STATUSES:
        raise UserError("Unknown status.")
    if status and len(reason.strip()) < 3:
        raise UserError("A reason is required for a manual status override.", field="reason")
    before = {"status": machine.override_status, "reason": machine.override_reason}
    machine.override_status = status
    machine.override_reason = reason.strip()[:255] if status else None
    machine.override_by = actor.split(":", 1)[-1] if status else None
    audit.record(db, actor, "machine.override" if status else "machine.override_clear", "machine", machine.id, before,
                 {"status": status, "reason": machine.override_reason})


def open_maintenance(db: Session, actor: str, machine: Machine, kind: str, description: str, state: str,
                     window_start: datetime | None = None, window_end: datetime | None = None) -> MaintenanceRecord:
    if kind not in ("issue", "scheduled", "inspection", "repair"):
        raise UserError("Unknown maintenance type.")
    if len(description.strip()) < 3:
        raise UserError("Describe the work or problem.", field="description")
    if state not in ("none", "awaiting_inspection", "under_maintenance"):
        raise UserError("Unknown machine state.")
    if kind == "scheduled" and not (window_start and window_end and window_end > window_start):
        raise UserError("Scheduled maintenance needs a start and a later end time.")
    rec = MaintenanceRecord(machine_id=machine.id, kind=kind, description=description.strip(),
                            state_set=state if state != "none" else None, opened_by=actor.split(":", 1)[-1],
                            window_start=window_start if kind == "scheduled" else None,
                            window_end=window_end if kind == "scheduled" else None)
    db.add(rec)
    before = machine.maintenance_state
    if kind != "scheduled" and state != "none":
        machine.maintenance_state = state
    db.flush()
    audit.record(db, actor, "maintenance.open", "machine", machine.id, {"state": before},
                 {"state": machine.maintenance_state, "record": rec.id, "kind": kind})
    return rec


def clear_machine(db: Session, actor: str, machine: Machine, note: str = "") -> int:
    """Inspection clearance / return to service: closes open issue records and resets the flag."""
    before = machine.maintenance_state
    n = 0
    for rec in db.scalars(select(MaintenanceRecord).where(MaintenanceRecord.machine_id == machine.id,
                                                          MaintenanceRecord.closed_at.is_(None),
                                                          MaintenanceRecord.kind != "scheduled")):
        rec.closed_at, rec.closed_by = clock.now(), actor.split(":", 1)[-1]
        n += 1
    machine.maintenance_state = "none"
    audit.record(db, actor, "machine.clear", "machine", machine.id, {"state": before},
                 {"state": "none", "closed_records": n, "note": note})
    return n


def close_record(db: Session, actor: str, rec: MaintenanceRecord) -> None:
    if rec.closed_at:
        raise UserError("That record is already closed.")
    rec.closed_at, rec.closed_by = clock.now(), actor.split(":", 1)[-1]
    audit.record(db, actor, "maintenance.close", "maintenance", rec.id)
