"""/checkin and /checkout — presence-verified with the rotating shop code."""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock, emailer, security
from app.db import get_db
from app.errors import UserError
from app.models import Machine, Reservation, ShopSession, Student
from app.services import contact, lifecycle, sessions
from app.services import settings as shop_settings
from app.web import get_form, public_post_guard, render, student_required

router = APIRouter(dependencies=[Depends(public_post_guard)])


def _todays(db, student):
    now = clock.now()
    today = clock.to_local(now).date()
    rows = db.scalars(select(Reservation).where(Reservation.student_id == student.id, Reservation.status == "confirmed",
                                                Reservation.end_at > now).order_by(Reservation.start_at)).all()
    return [r for r in rows if clock.to_local(r.start_at).date() == today]


@router.get("/checkin")
def checkin_form(request: Request, ref: str = "", student: Student = Depends(student_required),
                 db: Session = Depends(get_db)):
    lifecycle.lazy_sweep(db)
    active = sessions.active_session_for(db, student.id)
    return render(request, "checkin.html", {"ref": ref, "todays": _todays(db, student), "active": active, "errs": [],
                                            "student": student, "v": None})


@router.post("/checkin")
def checkin_post(request: Request, bg: BackgroundTasks, form=Depends(get_form),
                 student: Student = Depends(student_required), db: Session = Depends(get_db)):
    token = form.get("form_token")
    replay = security.idem_lookup(db, token)
    if replay:  # double-tap / double-scan: show the original result
        return RedirectResponse(replay, status_code=303)
    try:
        contact.apply_from_form(db, student, form)
        s = sessions.check_in(db, student, form.get("ref", ""), form.get("shop_code", ""), security.client_ip(request))
        url = f"/checkin/done/{s.id}"
        security.idem_store(db, token, "checkin", url)
        db.commit()
    except UserError as e:
        (db.commit if e.persist else db.rollback)()
        return render(request, "checkin.html", {"ref": form.get("ref", ""), "todays": _todays(db, student),
                                                "active": sessions.active_session_for(db, student.id),
                                                "errs": [e.message], "student": student, "v": dict(form)},
                      status=429 if e.code == "rate_limited" else 400)
    bg.add_task(emailer.flush_in_new_session)
    return RedirectResponse(url, status_code=303)


@router.get("/checkin/done/{sid}")
def checkin_done(sid: int, request: Request, student: Student = Depends(student_required), db: Session = Depends(get_db)):
    s = db.get(ShopSession, sid)
    if s is None or s.student_id != student.id:
        raise HTTPException(404, "Not found.")
    return render(request, "checkin_done.html", {"s": s})


@router.post("/checkin/done/{sid}/cancel")
def checkin_cancel(sid: int, student: Student = Depends(student_required), db: Session = Depends(get_db)):
    s = db.get(ShopSession, sid)
    if s is None or s.student_id != student.id:
        raise HTTPException(404, "Not found.")
    try:
        sessions.cancel_request(db, s, student)
        db.commit()
    except UserError:
        db.rollback()
    return RedirectResponse(f"/checkin/done/{sid}", status_code=303)


@router.get("/checkout")
def checkout_form(request: Request, student: Student = Depends(student_required), db: Session = Depends(get_db)):
    lifecycle.lazy_sweep(db)
    return render(request, "checkout.html", {"s": sessions.active_session_for(db, student.id), "errs": [],
                                             "items": sessions.CHECKLIST_ITEMS, "v": {}})


@router.post("/checkout")
def checkout_post(request: Request, bg: BackgroundTasks, form=Depends(get_form),
                  student: Student = Depends(student_required), db: Session = Depends(get_db)):
    token = form.get("form_token")
    replay = security.idem_lookup(db, token)
    if replay:
        return RedirectResponse(replay, status_code=303)
    checklist = {k: form.get(k) == "on" for k, _ in sessions.CHECKLIST_ITEMS}
    try:
        s = sessions.check_out(db, student, form.get("ref") or None, form.get("shop_code", ""), checklist,
                               security.client_ip(request), report_kind=form.get("report_kind", ""),
                               report_text=form.get("report_text", ""), photo=form.get("photo"),
                               leave_state=form.get("leave_state", ""))
        url = f"/checkout/done/{s.id}"
        security.idem_store(db, token, "checkout", url)
        db.commit()
    except UserError as e:
        (db.commit if e.persist else db.rollback)()
        return render(request, "checkout.html", {"s": sessions.active_session_for(db, student.id), "errs": [e.message],
                                                 "items": sessions.CHECKLIST_ITEMS, "v": dict(form)},
                      status=429 if e.code == "rate_limited" else 400)
    bg.add_task(emailer.flush_in_new_session)
    return RedirectResponse(url, status_code=303)


@router.get("/checkout/done/{sid}")
def checkout_done(sid: int, request: Request, student: Student = Depends(student_required), db: Session = Depends(get_db)):
    s = db.get(ShopSession, sid)
    if s is None or s.student_id != student.id:
        raise HTTPException(404, "Not found.")
    return render(request, "checkout_done.html", {"s": s})


# ---------------------------------------------------------------- per-machine QR check-in
def _machine_by_token(db, token):
    m = db.scalar(select(Machine).where(Machine.qr_token == token, Machine.active.is_(True)))
    if m is None:
        raise HTTPException(404, "This QR code isn't valid any more. Ask shop staff.")
    return m


def _qr_ctx(db, m, student, errs=None, v=None):
    now = clock.now()
    mine = [r for r in _todays(db, student) if r.machine_id == m.id]
    from app.services import qr
    return {"m": m, "token": m.qr_token, "mine": mine, "active": sessions.active_session_for(db, student.id),
            "waiting": sessions.pending_session_for(db, student.id), "enabled": shop_settings.load(db).machine_qr_checkin,
            "errs": errs or [], "student": student, "v": v, "now": now, "qr": qr}


@router.get("/m/{token}")
def machine_qr(token: str, request: Request, student: Student = Depends(student_required), db: Session = Depends(get_db)):
    m = _machine_by_token(db, token)
    lifecycle.lazy_sweep(db)
    return render(request, "machine_qr.html", _qr_ctx(db, m, student))


@router.post("/m/{token}/checkin")
def machine_qr_post(token: str, request: Request, bg: BackgroundTasks, form=Depends(get_form),
                    student: Student = Depends(student_required), db: Session = Depends(get_db)):
    m = _machine_by_token(db, token)
    ftoken = form.get("form_token")
    replay = security.idem_lookup(db, ftoken)
    if replay:
        return RedirectResponse(replay, status_code=303)
    try:
        if not shop_settings.load(db).machine_qr_checkin:
            raise UserError("Check-in by machine QR code is switched off. Use the Check In code and the TV code instead.")
        security.rate_check(db, f"mqr:{student.id}", 30, 600)
        contact.apply_from_form(db, student, form)
        s = sessions.check_in(db, student, form.get("ref", ""), form.get("shop_code", ""), security.client_ip(request),
                              via_machine=m.id)
        url = f"/checkin/done/{s.id}"
        security.idem_store(db, ftoken, "checkin", url)
        db.commit()
    except UserError as e:
        (db.commit if e.persist else db.rollback)()
        return render(request, "machine_qr.html", _qr_ctx(db, m, student, [e.message], dict(form)),
                      status=429 if e.code == "rate_limited" else 400)
    bg.add_task(emailer.flush_in_new_session)
    return RedirectResponse(url, status_code=303)
