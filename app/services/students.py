"""Staff operations on students: suspensions and certifications (all audited)."""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.errors import UserError
from app.models import Certification, Machine, MachineGroup, Student
from app.services import audit


def clear_suspension(db: Session, student: Student, actor: str, reason: str) -> None:
    """Lift a suspension and forgive earlier strikes so the counter restarts."""
    if not reason.strip():
        raise UserError("A reason is required.")
    before = {"until": student.suspended_until, "reason": student.suspension_reason}
    student.suspended_until = None
    student.suspension_reason = None
    student.noshow_forgiven_at = clock.now()
    audit.record(db, actor, "student.clear_suspension", "student", student.id, before, {"override_reason": reason})


def grant_certification(db: Session, student: Student, actor: str, machine_id: int | None, group_id: int | None,
                        expires_at: datetime | None, notes: str = "") -> Certification:
    if bool(machine_id) == bool(group_id):
        raise UserError("Choose either a machine or a machine group.")
    if machine_id and db.get(Machine, machine_id) is None:
        raise UserError("Unknown machine.")
    if group_id and db.get(MachineGroup, group_id) is None:
        raise UserError("Unknown group.")
    if expires_at and expires_at <= clock.now():
        raise UserError("The expiry date must be in the future.")
    c = Certification(student_id=student.id, machine_id=machine_id or None, group_id=group_id or None,
                      granted_by=actor.split(":", 1)[-1], expires_at=expires_at, notes=notes[:255] or None)
    db.add(c)
    db.flush()
    audit.record(db, actor, "certification.grant", "certification", c.id, after={
        "student": student.email, "machine_id": machine_id, "group_id": group_id, "expires_at": expires_at})
    return c


def revoke_certification(db: Session, cert: Certification, actor: str) -> None:
    if cert.revoked_at:
        raise UserError("Already revoked.")
    cert.revoked_at = clock.now()
    audit.record(db, actor, "certification.revoke", "certification", cert.id, {"revoked_at": None},
                 {"revoked_at": cert.revoked_at})
