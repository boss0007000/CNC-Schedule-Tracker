from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import emailer, security
from app.db import get_db
from app.errors import UserError
from app.models import STANDINGS
from app.services import identity
from app.services import settings as shop_settings
from app.web import get_form, public_post_guard, render, safe_next

router = APIRouter(dependencies=[Depends(public_post_guard)])


def _page(request, db, v=None, errs=None, ferr=None, nxt="/", status=200):
    s = shop_settings.load(db)
    return render(request, "verify.html", {"v": v or {}, "errs": errs or [], "ferr": ferr or {},
                                           "next": nxt, "standings": STANDINGS, "domain": s.email_domain,
                                           "collect_student_id": s.collect_student_id,
                                           "shop_name": s.shop_name}, status=status)


@router.get("/verify")
def verify_form(request: Request, next: str = "/", db: Session = Depends(get_db)):
    existing = identity.current_student(db, request)
    if existing:
        return RedirectResponse(safe_next(next), status_code=303)
    return _page(request, db, nxt=safe_next(next))


def _send_code(request, db, form, bg):
    email = identity.normalize_email(form.get("email"))
    nxt = safe_next(form.get("next"))
    from app.models import Student
    from sqlalchemy import select
    existing = db.scalar(select(Student).where(Student.email == email))
    try:
        profile = identity.validate_profile(db, email, form.get("full_name", ""), form.get("standing", ""),
                                            form.get("course"), form.get("student_id"), existing)
        identity.issue_code(db, email, profile, security.client_ip(request))
    except UserError as e:
        db.commit()  # keep rate-limit hits
        return None, _page(request, db, dict(form), [e.message], {e.field: e.message} if e.field else {},
                           nxt, 429 if e.code == "rate_limited" else 400)
    db.commit()
    bg.add_task(emailer.flush_in_new_session)
    return email, nxt


@router.post("/verify")
def verify_submit(request: Request, bg: BackgroundTasks, form=Depends(get_form), db: Session = Depends(get_db)):
    email, nxt = _send_code(request, db, form, bg)
    if email is None:
        return nxt
    return render(request, "verify_code.html", {"email": email, "next": nxt, "errs": []})


@router.post("/verify/resend")
def resend(request: Request, bg: BackgroundTasks, form=Depends(get_form), db: Session = Depends(get_db)):
    email = identity.normalize_email(form.get("email"))
    nxt = safe_next(form.get("next"))
    from sqlalchemy import select
    from app.models import EmailVerification
    last = db.scalar(select(EmailVerification).where(EmailVerification.email == email)
                     .order_by(EmailVerification.id.desc()).limit(1))
    if last is None or not last.pending_profile:
        return _page(request, db, {"email": email}, ["Start again so we can set up your profile."], nxt=nxt)
    try:
        identity.issue_code(db, email, last.pending_profile, security.client_ip(request))
    except UserError as e:
        db.commit()
        return render(request, "verify_code.html", {"email": email, "next": nxt, "errs": [e.message]}, status=429)
    db.commit()
    bg.add_task(emailer.flush_in_new_session)
    return render(request, "verify_code.html", {"email": email, "next": nxt, "errs": []})


@router.post("/verify/code")
def verify_code(request: Request, form=Depends(get_form), db: Session = Depends(get_db)):
    email = identity.normalize_email(form.get("email"))
    nxt = safe_next(form.get("next"))
    try:
        identity.check_code(db, email, form.get("code", ""), security.client_ip(request))
    except UserError as e:
        db.commit()  # persist attempt counters
        return render(request, "verify_code.html", {"email": email, "next": nxt, "errs": [e.message]},
                      status=429 if e.code == "rate_limited" else 400)
    db.commit()
    resp = RedirectResponse(nxt, status_code=303)
    from app.config import get_config
    resp.set_cookie(identity.COOKIE, identity.session_token(email), max_age=identity.COOKIE_MAX_AGE,
                    httponly=True, samesite="lax", secure=get_config().secure_cookies)
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie(identity.COOKIE)
    return resp
