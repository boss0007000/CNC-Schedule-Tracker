"""Setup / tools holds: one-tap pages (emailed links) and My shop actions."""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock, security
from app.db import get_db
from app.errors import UserError
from app.models import Machine, MachineHold, ShopSession, Student
from app.services import holds, identity
from app.web import get_form, public_post_guard, render, student_required

router = APIRouter(dependencies=[Depends(public_post_guard)])
LINK_TTL = 60 * 60 * 40


def _session_for(db: Session, request: Request, sid: int, token: str | None) -> ShopSession:
    s = db.get(ShopSession, sid)
    if s is None:
        raise HTTPException(404, "Not found.")
    d = security.unsign("hold-session", token or "", LINK_TTL)
    me = identity.current_student(db, request)
    if (d and d.get("sid") == sid) or (me and me.id == s.student_id):
        return s
    raise HTTPException(404, "Not found.")


@router.get("/hold/session/{sid}")
def session_page(sid: int, request: Request, t: str | None = None, db: Session = Depends(get_db)):
    s = _session_for(db, request, sid, t)
    cur = holds.active_hold(db, s.machine_id)
    return render(request, "hold_session.html", {"s": s, "t": t or "", "cur": cur, "errs": []})


@router.post("/hold/session/{sid}")
def session_post(sid: int, request: Request, form=Depends(get_form), db: Session = Depends(get_db)):
    s = _session_for(db, request, sid, form.get("t"))
    kind = form.get("kind", "")
    try:
        if kind == "clear":
            cur = holds.active_hold(db, s.machine_id)
            if cur is not None and cur.student_id == s.student_id:
                holds.release(db, cur, f"student:{s.student.email}", "owner")
            msg = "Thanks! The machine is marked clear."
        else:
            h = holds.declare(db, s.student, s.machine, kind, via="closing")
            msg = (f"Done. Everyone can see it's not to be touched until {h.expires_at.astimezone(clock.tz()):%a %b %d}. "
                   f"We'll remind you each open day.")
        db.commit()
    except UserError as e:
        db.rollback()
        return render(request, "hold_session.html", {"s": s, "t": form.get("t") or "", "cur": holds.active_hold(db, s.machine_id),
                                                     "errs": [e.message]}, status=400)
    return render(request, "hold_done.html", {"msg": msg})


def _hold_for(db: Session, request: Request, hid: int, token: str | None) -> MachineHold:
    h = db.get(MachineHold, hid)
    if h is None:
        raise HTTPException(404, "Not found.")
    d = security.unsign("hold-manage", token or "", 60 * 86400)
    me = identity.current_student(db, request)
    if (d and d.get("hid") == hid) or (me and me.id == h.student_id):
        return h
    raise HTTPException(404, "Not found.")


@router.get("/hold/{hid}")
def hold_page(hid: int, request: Request, t: str | None = None, db: Session = Depends(get_db)):
    h = _hold_for(db, request, hid, t)
    return render(request, "hold_manage.html", {"h": h, "t": t or "", "open": h.released_at is None})


@router.post("/hold/{hid}/release")
def hold_release(hid: int, request: Request, form=Depends(get_form), db: Session = Depends(get_db)):
    h = _hold_for(db, request, hid, form.get("t"))
    try:
        holds.release(db, h, f"student:{h.student.email}", "owner")
        db.commit()
    except UserError as e:
        db.rollback()
        return render(request, "hold_done.html", {"msg": e.message}, status=400)
    return render(request, "hold_done.html", {"msg": "Thank you! The machine is marked clear and open to others."})


@router.post("/my/hold")
def declare_from_my(request: Request, form=Depends(get_form), student: Student = Depends(student_required),
                    db: Session = Depends(get_db)):
    m = db.get(Machine, int(form.get("machine_id") or 0))
    if m is None:
        raise HTTPException(404, "Machine not found.")
    try:
        holds.declare(db, student, m, form.get("kind", ""), via="my")
        db.commit()
    except UserError as e:
        db.rollback()
        return RedirectResponse(f"/my?msg={e.message}", status_code=303)
    return RedirectResponse("/my?msg=Marked. Everyone can see it's not to be touched.", status_code=303)
