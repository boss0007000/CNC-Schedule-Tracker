"""/submit — project submission, revision and student-visible status."""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import emailer, security
from app.db import get_db
from app.errors import UserError
from app.models import Approval, Project, Student
from app.services import contact, files, identity, notify, pdfs, projects
from app.web import get_form, public_post_guard, render, student_required

router = APIRouter(dependencies=[Depends(public_post_guard)])


def _ctx(db, v=None, errs=None, project=None):
    return {"machines": projects.active_machines(db), "v": v or {}, "errs": errs or [], "project": project,
            "max_files": files.MAX_FILES, "max_mb": files.max_bytes() // (1024 * 1024)}


def _values(form) -> dict:
    return {"title": form.get("title", ""), "description": form.get("description", ""),
            "materials": form.get("materials", ""), "estimated_hours": form.get("estimated_hours", ""),
            "course": form.get("course", ""), "instructor": form.get("instructor", ""),
            "machine_ids": form.getlist("machine_ids")}


@router.get("/submit")
def submit_form(request: Request, student: Student = Depends(student_required), db: Session = Depends(get_db)):
    return render(request, "submit.html", _ctx(db, {"course": student.course}), student=student)


@router.post("/submit")
def submit_post(request: Request, bg: BackgroundTasks, form=Depends(get_form),
                student: Student = Depends(student_required), db: Session = Depends(get_db)):
    token = form.get("form_token")
    replay = security.idem_lookup(db, token)
    if replay:
        return RedirectResponse(replay, status_code=303)
    v = _values(form)
    v["phone"], v["show_contact"] = form.get("phone", ""), form.get("show_contact", "none")
    try:
        contact.apply_from_form(db, student, form)
        p = projects.submit(db, student, v, form.getlist("files"))
        url = f"/submit/{p.id}?new=1"
        security.idem_store(db, token, "submit", url)
        db.commit()
    except UserError as e:
        db.rollback()
        return render(request, "submit.html", _ctx(db, v, [e.message]), student=student, status=400)
    bg.add_task(emailer.flush_in_new_session)
    return RedirectResponse(url, status_code=303)


def _own_project(db, student, pid) -> Project:
    p = db.get(Project, pid)
    if p is None or p.student_id != student.id:
        raise HTTPException(404, "That project wasn't found.")
    return p


@router.get("/submit/{pid}")
def project_status(pid: int, request: Request, new: int = 0, student: Student = Depends(student_required),
                   db: Session = Depends(get_db)):
    p = _own_project(db, student, pid)
    approval = db.scalar(select(Approval).where(Approval.project_id == p.id).order_by(Approval.id.desc()))
    return render(request, "project.html", {"p": p, "approval": approval, "new": bool(new),
                                            "machines": {m.id: m.name for m in projects.active_machines(db)}})


@router.get("/submit/{pid}/revise")
def revise_form(pid: int, request: Request, student: Student = Depends(student_required),
                db: Session = Depends(get_db)):
    p = _own_project(db, student, pid)
    if p.status != "modifications_requested":
        raise HTTPException(400, "This project isn't waiting for changes.")
    v = {"title": p.title, "description": p.description, "materials": p.materials,
         "estimated_hours": f"{p.estimated_hours:g}", "course": p.course, "instructor": p.instructor,
         "machine_ids": [str(i) for i in p.machine_ids]}
    return render(request, "submit.html", _ctx(db, v, project=p), student=student)


@router.post("/submit/{pid}/revise")
def revise_post(pid: int, request: Request, bg: BackgroundTasks, form=Depends(get_form),
                student: Student = Depends(student_required), db: Session = Depends(get_db)):
    p = _own_project(db, student, pid)
    token = form.get("form_token")
    replay = security.idem_lookup(db, token)
    if replay:
        return RedirectResponse(replay, status_code=303)
    v = _values(form)
    try:
        projects.resubmit(db, student, p, v, form.getlist("files"))
        url = f"/submit/{p.id}?new=1"
        security.idem_store(db, token, "revise", url)
        db.commit()
    except UserError as e:
        db.rollback()
        return render(request, "submit.html", _ctx(db, v, [e.message], p), student=student, status=400)
    bg.add_task(emailer.flush_in_new_session)
    return RedirectResponse(url, status_code=303)


# --- approval record: viewable via the signed link in the approval email, or when verified ---
def _approval_for(db: Session, number: str, token: str | None, request: Request) -> Approval:
    a = db.scalar(select(Approval).where(Approval.number == number))
    if a is None:
        raise HTTPException(404, "Approval not found.")
    ok = False
    d = security.unsign("approval-view", token or "", 90 * 86400)
    if d and d.get("n") == number:
        ok = True
    else:
        s = identity.current_student(db, request)
        ok = bool(s and s.id == a.student_id)
    if not ok:
        raise HTTPException(404, "Approval not found.")
    return a


@router.get("/approval/{number}")
def approval_record(number: str, request: Request, t: str | None = None, db: Session = Depends(get_db)):
    a = _approval_for(db, number, t, request)
    return render(request, "approval_record.html", {"a": a, "text": notify.approval_text(a), "pdf": f"/approval/{number}/pdf" + (f"?t={t}" if t else "")})


@router.get("/approval/{number}/pdf")
def approval_pdf(number: str, request: Request, t: str | None = None, db: Session = Depends(get_db)):
    a = _approval_for(db, number, t, request)
    return Response(pdfs.approval_pdf(a), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{a.number}.pdf"'})
