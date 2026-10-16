"""Polite, optional requests to change a time slot."""
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock, emailer
from app.db import get_db
from app.errors import UserError
from app.models import Reservation, Student, TimeChangeRequest
from app.services import changes
from app.web import get_form, public_post_guard, render, student_required

router = APIRouter(dependencies=[Depends(public_post_guard)])


@router.get("/requests/new")
def new_form(request: Request, res: int, student: Student = Depends(student_required), db: Session = Depends(get_db)):
    t = db.get(Reservation, res)
    if t is None or t.status != "confirmed" or t.student_id == student.id:
        raise HTTPException(404, "That booking wasn't found.")
    return render(request, "request_new.html", {"t": t, "options": changes.candidate_windows(db, t, clock.now()),
                                                "errs": [], "note": ""})


@router.post("/requests/new")
def new_post(request: Request, bg: BackgroundTasks, form=Depends(get_form), student: Student = Depends(student_required),
             db: Session = Depends(get_db)):
    t = db.get(Reservation, int(form.get("res") or 0))
    if t is None:
        raise HTTPException(404, "That booking wasn't found.")
    try:
        start = datetime.fromisoformat(form.get("start", ""))
        if start.tzinfo is None:
            raise ValueError
        changes.create(db, student, t.id, start.astimezone(clock.UTC), form.get("note", ""))
        db.commit()
    except (UserError, ValueError) as e:
        db.rollback()
        msg = e.message if isinstance(e, UserError) else "Pick a new time for them."
        return render(request, "request_new.html", {"t": t, "options": changes.candidate_windows(db, t, clock.now()),
                                                    "errs": [msg], "note": form.get("note", "")}, status=400)
    bg.add_task(emailer.flush_in_new_session)
    return RedirectResponse("/my?msg=Request sent. They can say yes or no with one tap; we'll email you.", status_code=303)


def _req(db, student, rid) -> TimeChangeRequest:
    r = db.get(TimeChangeRequest, rid)
    if r is None or student.id not in (r.requester_id, r.target_student_id):
        raise HTTPException(404, "Request not found.")
    return r


@router.get("/requests/{rid}")
def view(rid: int, request: Request, student: Student = Depends(student_required), db: Session = Depends(get_db)):
    r = _req(db, student, rid)
    return render(request, "request_view.html", {"r": r, "as_target": r.target_student_id == student.id,
                                                 "open": r.status == "pending" and r.expires_at > clock.now(), "errs": []})


@router.post("/requests/{rid}/{action}")
def respond(rid: int, action: str, bg: BackgroundTasks, student: Student = Depends(student_required),
            db: Session = Depends(get_db)):
    r = _req(db, student, rid)
    try:
        if action == "accept":
            changes.accept(db, r, student)
        elif action == "decline":
            changes.decline(db, r, student)
        elif action == "cancel":
            changes.cancel(db, r, student)
        else:
            raise HTTPException(404, "Unknown action.")
        db.commit()
    except UserError as e:
        db.rollback()
        return RedirectResponse(f"/my?msg={e.message}", status_code=303)
    bg.add_task(emailer.flush_in_new_session)
    msg = {"accept": "Thank you! Your time was moved and the other student has been told.",
           "decline": "No problem. Nothing changes and nobody is told why.", "cancel": "Request cancelled."}[action]
    return RedirectResponse(f"/my?msg={msg}", status_code=303)
