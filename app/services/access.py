"""Who may use which machine. Two staff-managed lists (approved / super approved) + a per-machine level.
Students never see which list anyone is on, and the messages avoid words that rank people."""
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.errors import UserError
from app.models import ACCESS_APPROVED, ACCESS_SUPER, AccessRoster, Machine, MachineAccess, Student
from app.services import audit
from app.services import settings as shop_settings

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-']+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
LABELS = {ACCESS_APPROVED: "Approved", ACCESS_SUPER: "Super approved"}


def level_for(db: Session, email: str) -> str | None:
    row = db.scalar(select(AccessRoster).where(AccessRoster.email == email.strip().lower()))
    return row.level if row else None


def effective_level(db: Session, student: Student) -> str | None:
    """None = not on a list. If the shop turned the list requirement off, everyone counts as approved."""
    lvl = level_for(db, student.email)
    if lvl is None and not shop_settings.load(db).require_access_list:
        return ACCESS_APPROVED
    return lvl


def machine_levels(machine: Machine) -> set[str]:
    """The groups allowed to book this machine (explicit rows win; legacy access_level is the fallback)."""
    if machine.access_rows:
        return {r.level for r in machine.access_rows}
    return {"staff_only": set(), "advanced": {ACCESS_SUPER}}.get(machine.access_level, {ACCESS_APPROVED, ACCESS_SUPER})


def is_staff_run(machine: Machine) -> bool:
    return not machine_levels(machine)


def machine_allows(level: str | None, machine: Machine) -> bool:
    return level is not None and level in machine_levels(machine)


def set_machine_levels(db: Session, actor: str, machine: Machine, levels: set[str]) -> None:
    """Choose which groups may book a machine. Empty = staff-run (students submit parts only)."""
    levels = set(levels)
    if not levels <= {ACCESS_APPROVED, ACCESS_SUPER}:
        raise UserError("Unknown group.")
    before = sorted(machine_levels(machine))
    for row in list(machine.access_rows):
        machine.access_rows.remove(row)
    db.flush()
    for lv in sorted(levels):
        machine.access_rows.append(MachineAccess(level=lv))
    # keep the legacy summary column in step (used for labels)
    machine.access_level = ("staff_only" if not levels else "advanced" if levels == {ACCESS_SUPER} else "standard")
    db.flush()
    if before != sorted(levels):
        audit.record(db, actor, "machine.access", "machine", machine.id, {"groups": before}, {"groups": sorted(levels)})


CHOICES = {"standard": {ACCESS_APPROVED, ACCESS_SUPER}, "advanced": {ACCESS_SUPER}, "staff_only": set()}


def check_access(db: Session, student: Student, machine: Machine) -> str:
    """Raise a kind, specific UserError, or return the student's level."""
    if is_staff_run(machine):
        raise UserError(f"The {machine.name} is run by shop staff. Submit your part on the project form and the "
                        f"staff will make it for you.", code="staff_only", field="machine")
    lvl = effective_level(db, student)
    if lvl is None:
        raise UserError("You haven't been added to the shop's approved list yet. Ask a shop supervisor to add you.",
                        code="not_listed")
    if not machine_allows(lvl, machine):
        raise UserError(f"The {machine.name} needs additional shop sign-off. Ask a supervisor how to get set up on it.",
                        code="needs_more_access", field="machine")
    return lvl


def session_cap_hours(db: Session, level: str | None) -> float:
    cfg = shop_settings.load(db)
    return float(cfg.max_session_hours_advanced if level == ACCESS_SUPER else cfg.max_session_hours_standard)


def bookable_machines(db: Session, student: Student, machines: list[Machine]) -> list[Machine]:
    lvl = effective_level(db, student)
    return [m for m in machines if machine_allows(lvl, m)]


# ------------------------------------------------------------------ staff management
def parse_emails(raw: str) -> list[str]:
    seen, out = set(), []
    for m in EMAIL_RE.findall((raw or "").lower()):
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out


def set_levels(db: Session, actor: str, emails: list[str], level: str, note: str = "") -> dict:
    if level not in (ACCESS_APPROVED, ACCESS_SUPER):
        raise UserError("Choose a list.")
    if not emails:
        raise UserError("Enter at least one email address.")
    added = moved = 0
    for e in emails:
        row = db.scalar(select(AccessRoster).where(AccessRoster.email == e))
        if row is None:
            db.add(AccessRoster(email=e, level=level, added_by=actor.split(":", 1)[-1], note=note[:255] or None))
            added += 1
            audit.record(db, actor, "access.add", "access_roster", e, None, {"level": level})
        elif row.level != level:
            audit.record(db, actor, "access.move", "access_roster", e, {"level": row.level}, {"level": level})
            row.level = level
            moved += 1
    db.flush()
    return {"added": added, "moved": moved, "unchanged": len(emails) - added - moved}


def remove(db: Session, actor: str, email: str) -> None:
    row = db.scalar(select(AccessRoster).where(AccessRoster.email == email.strip().lower()))
    if row is None:
        raise UserError("That email isn't on a list.")
    audit.record(db, actor, "access.remove", "access_roster", row.email, {"level": row.level}, None)
    db.delete(row)


def roster(db: Session) -> dict[str, list]:
    rows = db.scalars(select(AccessRoster).order_by(func.lower(AccessRoster.email))).all()
    return {ACCESS_SUPER: [r for r in rows if r.level == ACCESS_SUPER],
            ACCESS_APPROVED: [r for r in rows if r.level == ACCESS_APPROVED]}
