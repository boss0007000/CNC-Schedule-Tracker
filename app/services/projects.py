"""Project submission, revision history and the approval workflow."""
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.errors import UserError
from app.models import (
    PROJECT_APPROVED, PROJECT_CHANGES, PROJECT_REJECTED, PROJECT_SUBMITTED, Approval, ApprovalMachine,
    ApprovalRestriction, Machine, Project, ProjectFile, ProjectRevision, Student,
)
from app.services import audit, files, notify, numbers
from app.services import settings as shop_settings


def active_machines(db: Session) -> list[Machine]:
    return list(db.scalars(select(Machine).where(Machine.active.is_(True)).order_by(Machine.sort_order, Machine.name)))


def _clean(data: dict, db: Session) -> dict:
    title = " ".join(str(data.get("title", "")).split())
    desc = str(data.get("description", "")).strip()
    if len(title) < 3 or len(title) > 200:
        raise UserError("Give your project a title (3–200 characters).", field="title")
    if len(desc) < 10 or len(desc) > 5000:
        raise UserError("Describe your project in at least a sentence (10–5000 characters).", field="description")
    ids = []
    valid = {m.id for m in active_machines(db)}
    for raw in data.get("machine_ids", []):
        try:
            i = int(raw)
        except (TypeError, ValueError):
            continue
        if i in valid and i not in ids:
            ids.append(i)
    if not ids:
        raise UserError("Choose at least one machine you plan to use.", field="machine_ids")
    try:
        hours = float(data.get("estimated_hours") or 0)
    except ValueError:
        raise UserError("Estimated hours must be a number.", field="estimated_hours") from None
    if not 0.5 <= hours <= 500:
        raise UserError("Estimated hours must be between 0.5 and 500.", field="estimated_hours")
    return {"title": title, "description": desc, "machine_ids": ids, "estimated_hours": hours,
            "materials": str(data.get("materials", "")).strip()[:500],
            "course": (str(data.get("course", "")).strip()[:120] or None),
            "instructor": (str(data.get("instructor", "")).strip()[:120] or None)}


def _attach_files(db: Session, project: Project, uploads: list, revision_no: int) -> None:
    uploads = [u for u in uploads if getattr(u, "filename", "")]
    existing = db.scalar(select(func.count()).select_from(ProjectFile).where(ProjectFile.project_id == project.id)) or 0
    if len(uploads) > files.MAX_FILES:
        raise UserError(f"You can upload at most {files.MAX_FILES} files.", field="files")
    if existing + len(uploads) > files.MAX_FILES * 3:
        raise UserError("This project already has too many files.", field="files")
    # validate everything first, then write, so a bad file doesn't leave strays
    blobs = [(u, files.read_upload(u)) for u in uploads]
    for u, data in blobs:
        files.validate(u.filename, u.content_type, data)
    for u, data in blobs:
        st = files.store(db, u.filename, u.content_type, data)
        db.add(ProjectFile(project_id=project.id, revision_no=revision_no, original_name=st.original_name,
                           storage_key=st.key, mime=st.mime, size=st.size, sha256=st.sha256, scan_status=st.scan_status))
    db.flush()


def submit(db: Session, student: Student, data: dict, uploads: list) -> Project:
    c = _clean(data, db)
    now = clock.now()
    p = Project(student_id=student.id, status=PROJECT_SUBMITTED, revision_no=1, created_at=now, updated_at=now, **c)
    db.add(p)
    db.flush()
    db.add(ProjectRevision(project_id=p.id, revision_no=1, title=p.title, description=p.description,
                           materials=p.materials, estimated_hours=p.estimated_hours, machine_ids=p.machine_ids))
    _attach_files(db, p, uploads, 1)
    audit.record(db, f"student:{student.email}", "project.submit", "project", p.id, after={"title": p.title})
    notify.project_submitted(db, p)
    return p


def resubmit(db: Session, student: Student, project: Project, data: dict, uploads: list) -> Project:
    if project.student_id != student.id:
        raise UserError("That project belongs to another student.")
    if project.status != PROJECT_CHANGES:
        raise UserError("This project isn't waiting for changes, so it can't be resubmitted.")
    c = _clean(data, db)
    project.revision_no += 1
    for k, v in c.items():
        setattr(project, k, v)
    project.status = PROJECT_SUBMITTED
    project.updated_at = clock.now()
    db.add(ProjectRevision(project_id=project.id, revision_no=project.revision_no, title=project.title,
                           description=project.description, materials=project.materials,
                           estimated_hours=project.estimated_hours, machine_ids=project.machine_ids))
    _attach_files(db, project, uploads, project.revision_no)
    audit.record(db, f"student:{student.email}", "project.resubmit", "project", project.id,
                 after={"revision": project.revision_no})
    notify.project_submitted(db, project, revised=True)
    return project


def _close_revision_feedback(project: Project, comments: str) -> None:
    for r in project.revisions:
        if r.revision_no == project.revision_no:
            r.staff_comments = comments


def approve(db: Session, project: Project, actor: str, machine_ids: list[int], restrictions: list[str],
            max_total_hours: float | None, max_sessions: int | None, expires_on: datetime | None,
            comments: str = "", conditions: str = "") -> Approval:
    if project.status not in (PROJECT_SUBMITTED, PROJECT_CHANGES):
        raise UserError("This project has already been decided.")
    if not machine_ids:
        raise UserError("Select at least one machine the approval covers.")
    valid = {m.id for m in active_machines(db)}
    machine_ids = [i for i in dict.fromkeys(machine_ids) if i in valid]
    if not machine_ids:
        raise UserError("Selected machines are not available.")
    if max_total_hours is not None and max_total_hours <= 0:
        raise UserError("Max total hours must be positive.")
    if max_sessions is not None and max_sessions <= 0:
        raise UserError("Max sessions must be positive.")
    now = clock.now()
    if expires_on is None:
        expires_on = now + timedelta(days=shop_settings.load(db).approval_default_days)
    if expires_on <= now:
        raise UserError("The expiry date must be in the future.")
    for _ in range(5):
        number = numbers.next_approval_number(db)
        a = Approval(number=number, project_id=project.id, student_id=project.student_id, comments=comments or None,
                     conditions=conditions or None, max_total_hours=max_total_hours, max_sessions=max_sessions,
                     expires_at=expires_on, approved_by=actor.split(":", 1)[-1], approved_at=now)
        try:
            with db.begin_nested():
                db.add(a)
                db.flush()
            break
        except Exception:  # unique collision on number; retry
            continue
    else:
        raise UserError("Could not allocate an approval number; please try again.")
    for mid in machine_ids:
        db.add(ApprovalMachine(approval_id=a.id, machine_id=mid))
    for r in restrictions:
        if r.strip():
            db.add(ApprovalRestriction(approval_id=a.id, kind="material", value=r.strip()[:255]))
    project.status = PROJECT_APPROVED
    project.staff_comments = comments or None
    project.reviewed_by, project.reviewed_at = actor.split(":", 1)[-1], now
    _close_revision_feedback(project, comments)
    db.flush()
    db.refresh(a)
    audit.record(db, actor, "project.approve", "project", project.id, {"status": PROJECT_SUBMITTED},
                 {"status": PROJECT_APPROVED, "approval": a.number, "machines": machine_ids,
                  "max_total_hours": max_total_hours, "max_sessions": max_sessions})
    notify.project_approved(db, a)
    return a


def reject(db: Session, project: Project, actor: str, reason: str) -> None:
    if project.status not in (PROJECT_SUBMITTED, PROJECT_CHANGES):
        raise UserError("This project has already been decided.")
    if len((reason or "").strip()) < 3:
        raise UserError("Please give the student a reason for the rejection.", field="reason")
    before = project.status
    project.status, project.staff_comments = PROJECT_REJECTED, reason.strip()
    project.reviewed_by, project.reviewed_at = actor.split(":", 1)[-1], clock.now()
    _close_revision_feedback(project, reason.strip())
    audit.record(db, actor, "project.reject", "project", project.id, {"status": before},
                 {"status": PROJECT_REJECTED, "reason": reason.strip()})
    notify.project_rejected(db, project, reason.strip())


def request_changes(db: Session, project: Project, actor: str, comments: str) -> None:
    if project.status != PROJECT_SUBMITTED:
        raise UserError("Only a submitted project can be sent back for changes.")
    if len((comments or "").strip()) < 3:
        raise UserError("Tell the student what to change.", field="comments")
    project.status, project.staff_comments = PROJECT_CHANGES, comments.strip()
    project.reviewed_by, project.reviewed_at = actor.split(":", 1)[-1], clock.now()
    _close_revision_feedback(project, comments.strip())
    audit.record(db, actor, "project.request_changes", "project", project.id, {"status": PROJECT_SUBMITTED},
                 {"status": PROJECT_CHANGES, "comments": comments.strip()})
    notify.project_changes(db, project, comments.strip())


def revoke_approval(db: Session, a: Approval, actor: str, reason: str) -> None:
    if a.status == "revoked":
        raise UserError("Already revoked.")
    a.status = "revoked"
    audit.record(db, actor, "approval.revoke", "approval", a.id, {"status": "active"},
                 {"status": "revoked", "reason": reason})
