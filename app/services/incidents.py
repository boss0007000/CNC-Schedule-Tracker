from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.errors import UserError
from app.models import DamageReport, IncidentReport, Machine, Student
from app.services import audit


def report_incident(db: Session, actor: str, kind: str, occurred_at: datetime | None, machine_id: int | None,
                    student_email: str, description: str, action_taken: str, severity: str) -> IncidentReport:
    if kind not in ("injury", "near_miss", "other"):
        raise UserError("Choose the incident type.", field="kind")
    if severity not in ("minor", "moderate", "serious"):
        raise UserError("Choose a severity.", field="severity")
    if len(description.strip()) < 10:
        raise UserError("Describe what happened (at least a sentence).", field="description")
    if occurred_at and occurred_at > clock.now():
        raise UserError("The incident time can't be in the future.", field="occurred_at")
    stu = None
    if student_email.strip():
        stu = db.scalar(select(Student).where(Student.email == student_email.strip().lower()))
        if stu is None:
            raise UserError("No student with that email. Leave it blank if unknown.", field="student_email")
    if machine_id and db.get(Machine, machine_id) is None:
        raise UserError("Unknown machine.")
    inc = IncidentReport(kind=kind, occurred_at=occurred_at or clock.now(), machine_id=machine_id or None,
                         student_id=stu.id if stu else None, description=description.strip(),
                         action_taken=action_taken.strip() or None, severity=severity,
                         reported_by=actor.split(":", 1)[-1])
    db.add(inc)
    db.flush()
    audit.record(db, actor, "incident.report", "incident", inc.id, after={"kind": kind, "severity": severity})
    return inc


def resolve_incident(db: Session, actor: str, inc: IncidentReport, resolved: bool) -> None:
    before = inc.resolved
    inc.resolved = resolved
    audit.record(db, actor, "incident.resolve", "incident", inc.id, {"resolved": before}, {"resolved": resolved})


def resolve_damage(db: Session, actor: str, rep: DamageReport, status: str) -> None:
    if status not in ("open", "reviewed", "resolved"):
        raise UserError("Unknown status.")
    before = rep.status
    rep.status = status
    audit.record(db, actor, "damage.status", "damage_report", rep.id, {"status": before}, {"status": status})
