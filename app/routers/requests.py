"""Polite, optional requests to change a time slot (see services/changes.py)."""
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import clock, emailer
from app.db import get_db
from app.errors import UserError
from app.models import Reservation, Student, TimeChangeRequest
from app.services import changes
from app.web import get_form, public_post_guard, render, student_required

router = APIRouter(dependencies=[Depends(public_post_guard)])


def _parse_window(raw: str) -> tuple[datetime, datetime]:
    try:
        a, b = (datetime.fromisoformat(x) for x in (raw or "").split("|"))
        if a.tzinfo is None or b.tzinfo is None:
            raise ValueError
        return a.astimezone(clock.UTC), b.astimezone(clock.UTC)
    except ValueError:
        raise UserError("Please pick one of the times in the list.") from None


def _enc(s: datetime, e: datetime) -> str:
    return f"{s.isoformat()}|{e.isoformat()}"


@router.get("/requests/new")
def new_form(request: Request, res: int, student: Student = Depends(student_required), db: Session = Depends(get_db)):
    t = db.get(Reservation, res)
    if t is None or t.status != "confirmed" or t.student_id == student.id:
        raise HTTPException(404, "That booking wasn't found.")
    return render(request, "request_new.html", {"t": t, "options": changes.wanted_options(db, student, t, clock.now()),
                                                "errs": [], "note": "", "enc": _enc})


@router.post("/requests/new")
def new_post(request: Request, bg: BackgroundTasks, form=Depends(get_form), student: Student = Depends(student_required),
             db: Session = Depends(get_db)):
    t = db.get(Reservation, int(form.get("res") or 0))
    if t is None:
        raise HTTPException(404, "That booking wasn't found.")
    try:
        ws, we = _parse_window(form.get("window", ""))
        changes.create(db, student, t.id, ws, we, form.get("note", ""))
        db.commit()
    except UserError as e:
        db.rollback()
        return render(request, "request_new.html", {"t": t, "options": changes.wanted_options(db, student, t, clock.now()),
                                                    "errs": [e.message], "note": form.get("note", ""), "enc": _enc}, status=400)
    bg.add_task(emailer.flush_in_new_session)
    return RedirectResponse("/my?msg=Request sent. They can say yes (and pick a time that suits them) or no; we'll email you.",
                            status_code=303)


def _req(db, student, rid) -> TimeChangeRequest:
    r = db.get(TimeChangeRequest, rid)
    if r is None or student.id not in (r.requester_id, r.target_student_id):
        raise HTTPException(404, "Request not found.")
    return r


@router.get("/requests/{rid}")
def view(rid: int, request: Request, student: Student = Depends(student_required), db: Session = Depends(get_db)):
    r = _req(db, student, rid)
    is_open = r.status == "pending" and r.expires_at > clock.now()
    as_target = r.target_student_id == student.id
    options = changes.target_options(db, r.target_reservation, (r.wanted_start, r.wanted_end), clock.now()) \
        if (is_open and as_target) else []
    return render(request, "request_view.html", {"r": r, "as_target": as_target, "open": is_open, "errs": [],
                                                 "options": options, "enc": _enc})


@router.post("/requests/{rid}/{action}")
def respond(rid: int, action: str, request: Request, bg: BackgroundTasks, form=Depends(get_form),
            student: Student = Depends(student_required), db: Session = Depends(get_db)):
    r = _req(db, student, rid)
    try:
        if action == "accept":
            ns, ne = _parse_window(form.get("choice", ""))
            changes.accept(db, r, student, ns, ne)
        elif action == "decline":
            changes.decline(db, r, student)
        elif action == "cancel":
            changes.cancel(db, r, student)
        else:
            raise HTTPException(404, "Unknown action.")
        db.commit()
    except UserError as e:
        db.rollback()
        r = _req(db, student, rid)
        is_open = r.status == "pending" and r.expires_at > clock.now()
        opts = changes.target_options(db, r.target_reservation, (r.wanted_start, r.wanted_end), clock.now()) if is_open else []
        return render(request, "request_view.html", {"r": r, "as_target": r.target_student_id == student.id, "open": is_open,
                                                     "errs": [e.message], "options": opts, "enc": _enc}, status=400)
    bg.add_task(emailer.flush_in_new_session)
    msg = {"accept": "Thank you! Your time was moved and everyone has been told.",
           "decline": "No problem. Nothing changes and nobody is told why.", "cancel": "Request cancelled."}[action]
    return RedirectResponse(f"/my?msg={msg}", status_code=303)
