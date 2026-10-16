"""/reserve — machine reservations, cancellation, extension, waiting list."""
from datetime import date, datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock, emailer, security
from app.db import get_db
from app.errors import UserError
from app.models import Approval, Machine, Reservation, Student, WaitlistEntry
from app.services import access, contact, identity, projects, reservations
from app.services import settings as shop_settings
from app.web import get_form, public_post_guard, render, student_required

router = APIRouter(dependencies=[Depends(public_post_guard)])


def _parse_dt(raw: str) -> datetime:
    try:
        dt = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        raise UserError("Pick a time slot.", field="start") from None
    if dt.tzinfo is None:
        raise UserError("Pick a time slot.", field="start")
    return dt.astimezone(clock.UTC)


def _page_ctx(db, student, approval, machine_id, day, nslots=1):
    cfg = shop_settings.load(db)
    my_approvals = db.scalars(select(Approval).where(Approval.student_id == student.id, Approval.status == "active")
                              .order_by(Approval.id.desc())).all()
    today = clock.to_local(clock.now()).date()
    lvl = access.effective_level(db, student)
    max_slots = max(1, int(access.session_cap_hours(db, lvl) * 60 // cfg.slot_minutes))
    ctx = {"cfg": cfg, "approvals": my_approvals, "max_slots": max_slots,
           "machines": access.bookable_machines(db, student, projects.active_machines(db)), "approval": approval or "",
           "machine_id": machine_id, "day": day.isoformat() if day else "", "today": today.isoformat(),
           "last_day": (today + timedelta(days=cfg.max_advance_days)).isoformat(), "result": None,
           "nslots": nslots, "errs": [], "student": student}
    if approval and machine_id and day:
        ctx["result"] = reservations.slots_for(db, student, approval, machine_id, day)
    elif approval is not None and (machine_id or day):
        ctx["errs"] = []
    return ctx


@router.get("/reserve")
def reserve_page(request: Request, approval: str = "", machine: int = 0, date: str = "", n: int = 1,
                 student: Student = Depends(student_required), db: Session = Depends(get_db)):
    day = None
    if date:
        try:
            day = datetime.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            day = None
    return render(request, "reserve.html", _page_ctx(db, student, approval.strip(), machine or None, day, n))


@router.post("/reserve")
def reserve_post(request: Request, bg: BackgroundTasks, form=Depends(get_form),
                 student: Student = Depends(student_required), db: Session = Depends(get_db)):
    token = form.get("form_token")
    replay = security.idem_lookup(db, token)
    if replay:
        return RedirectResponse(replay, status_code=303)
    ip = security.client_ip(request)
    approval_no = (form.get("approval") or "").strip()
    action = form.get("action", "book")
    try:
        machine_id = int(form.get("machine_id") or 0)
        start = _parse_dt(form.get("start", ""))
        n = max(1, min(int(form.get("nslots") or 1), 48))
        slot = timedelta(minutes=shop_settings.load(db).slot_minutes)
        end = start + slot * n
        accepted = form.get("accept_waiver") == "on"
        contact.apply_from_form(db, student, form)  # phone is required to reserve; saved with the booking
        if action == "waitlist":
            w = reservations.join_waitlist(db, student, approval_no, machine_id, start, end,
                                           waiver_accepted=accepted, ip=ip)
            url = f"/my?joined={w.id}"
        else:
            r = reservations.create(db, student, approval_no, machine_id, start, end, waiver_accepted=accepted, ip=ip)
            url = f"/reservation/{r.ref}?new=1"
        security.idem_store(db, token, "reserve", url)
        db.commit()
    except UserError as e:
        db.rollback()
        try:
            day = clock.to_local(_parse_dt(form.get("start", ""))).date()
        except UserError:
            day = None
        ctx = _page_ctx(db, student, approval_no, int(form.get("machine_id") or 0) or None, day)
        ctx["errs"] = [e.message]
        ctx["needs_waiver_prompt"] = e.code == "waiver_required"
        if e.code == "waiver_required" and ctx["result"]:
            ctx["result"]["needs_waiver"] = True
        if e.code in ("phone_required",) or e.field in ("phone", "ack_phone"):
            if ctx["result"]:
                ctx["result"]["needs_contact"] = True
            ctx["form_values"] = dict(form)
        return render(request, "reserve.html", ctx, status=400)
    bg.add_task(emailer.flush_in_new_session)
    return RedirectResponse(url, status_code=303)


# --------------------------------------------------------------------- one reservation
def _reservation_access(db: Session, request: Request, ref: str, token: str | None) -> tuple[Reservation, bool]:
    ref = ref.upper()
    r = db.scalar(select(Reservation).where(Reservation.ref == ref))
    if r is None:
        raise HTTPException(404, "Reservation not found.")
    d = security.unsign("res-cancel", token or "", 60 * 86400)
    owner = identity.current_student(db, request)
    if (d and d.get("ref") == r.ref) or (owner and owner.id == r.student_id):
        return r, bool(owner and owner.id == r.student_id)
    raise HTTPException(404, "Reservation not found.")


@router.get("/reservation/{ref}")
def reservation_view(ref: str, request: Request, t: str | None = None, new: int = 0, db: Session = Depends(get_db)):
    r, owner = _reservation_access(db, request, ref, t)
    cfg = shop_settings.load(db)
    can_cancel = r.status == "confirmed" and clock.now() <= r.start_at - timedelta(minutes=cfg.cancel_cutoff_minutes)
    return render(request, "reservation.html", {"r": r, "t": t or "", "owner": owner, "new": bool(new),
                                                "can_cancel": can_cancel, "cfg": cfg, "errs": [], "msg": None})


@router.post("/reservation/{ref}/cancel")
def reservation_cancel(ref: str, request: Request, bg: BackgroundTasks, form=Depends(get_form),
                       db: Session = Depends(get_db)):
    r, _ = _reservation_access(db, request, ref, form.get("t"))
    try:
        reservations.cancel(db, r, actor=f"student:{r.student.email}")
        db.commit()
    except UserError as e:
        db.rollback()
        cfg = shop_settings.load(db)
        return render(request, "reservation.html", {"r": r, "t": form.get("t") or "", "owner": False, "new": False,
                                                    "can_cancel": False, "cfg": cfg, "errs": [e.message], "msg": None},
                      status=400)
    bg.add_task(emailer.flush_in_new_session)
    return RedirectResponse(f"/reservation/{r.ref}?t={form.get('t') or ''}", status_code=303)


@router.post("/reservation/{ref}/extend")
def reservation_extend(ref: str, request: Request, bg: BackgroundTasks, form=Depends(get_form),
                       student: Student = Depends(student_required), db: Session = Depends(get_db)):
    r, owner = _reservation_access(db, request, ref, None)
    if not owner:
        raise HTTPException(404, "Reservation not found.")
    try:
        reservations.extend(db, r, student, 1)
        db.commit()
    except UserError as e:
        db.rollback()
        cfg = shop_settings.load(db)
        return render(request, "reservation.html", {"r": r, "t": "", "owner": True, "new": False, "can_cancel": False,
                                                    "cfg": cfg, "errs": [e.message], "msg": None}, status=400)
    bg.add_task(emailer.flush_in_new_session)
    return RedirectResponse(f"/reservation/{r.ref}", status_code=303)


# --------------------------------------------------------------------- waiting list
@router.get("/waitlist/claim/{token}")
def claim_page(token: str, request: Request, student: Student = Depends(student_required),
               db: Session = Depends(get_db)):
    w = db.scalar(select(WaitlistEntry).where(WaitlistEntry.claim_token == token))
    if w is None or w.student_id != student.id:
        raise HTTPException(404, "That claim link isn't valid for your account.")
    ok = w.status == "offered" and w.claim_expires_at and w.claim_expires_at > clock.now()
    return render(request, "claim.html", {"w": w, "ok": ok, "token": token, "student": student, "v": None})


@router.post("/waitlist/claim/{token}")
def claim_post(token: str, request: Request, bg: BackgroundTasks, form=Depends(get_form),
               student: Student = Depends(student_required), db: Session = Depends(get_db)):
    try:
        contact.apply_from_form(db, student, form)
        r = reservations.claim(db, token, student, ip=security.client_ip(request))
        db.commit()
    except UserError as e:
        db.rollback()
        w = db.scalar(select(WaitlistEntry).where(WaitlistEntry.claim_token == token))
        return render(request, "claim.html", {"w": w, "ok": True, "token": token, "errs": [e.message], "student": student,
                                              "v": dict(form)}, status=400)
    bg.add_task(emailer.flush_in_new_session)
    return RedirectResponse(f"/reservation/{r.ref}?new=1", status_code=303)


@router.post("/waitlist/{wid}/leave")
def leave_waitlist(wid: int, bg: BackgroundTasks, student: Student = Depends(student_required),
                   db: Session = Depends(get_db)):
    w = db.get(WaitlistEntry, wid)
    if w is None:
        raise HTTPException(404, "Not found.")
    reservations.cancel_waitlist(db, w, student)
    db.commit()
    bg.add_task(emailer.flush_in_new_session)
    return RedirectResponse("/my", status_code=303)
