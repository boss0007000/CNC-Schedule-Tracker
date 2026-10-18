"""Staff auth, user management, audit log viewer."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import security
from app.config import get_config
from app.db import get_db
from app.errors import UserError
from app.models import AuditLog, StaffUser
from app.services import audit, staffauth
from app.web import get_form, render, safe_next, staff_actor, staff_dep

router = APIRouter(prefix="/admin")


@router.get("/login")
def login_form(request: Request, next: str = "/admin"):
    return render(request, "admin/login.html", {"next": safe_next(next, "/admin"), "errs": []})


@router.post("/login")
def login(request: Request, form=Depends(get_form), db: Session = Depends(get_db)):
    ip = security.client_ip(request)
    nxt = safe_next(form.get("next"), "/admin")
    try:
        security.rate_check(db, f"login:ip:{ip}", 20, 600)
        user = staffauth.authenticate(db, form.get("username", ""), form.get("password", ""), ip)
    except UserError as e:
        db.commit()  # persist failure counters / lockout
        return render(request, "admin/login.html", {"next": nxt, "errs": [e.message]}, status=401)
    db.commit()
    resp = RedirectResponse(nxt, status_code=303)
    resp.set_cookie(staffauth.COOKIE, staffauth.make_session_token(user), max_age=staffauth.SESSION_MAX_AGE,
                    httponly=True, samesite="lax", secure=get_config().secure_cookies)
    return resp


def _own_user(request: Request, db: Session) -> StaffUser:
    """Signed-in staff, even if they still must change their password (this is the page that fixes that)."""
    from app.web import NeedsStaffLogin
    u = staffauth.user_from_token(db, request.cookies.get(staffauth.COOKIE))
    if u is None:
        raise NeedsStaffLogin("/admin/password")
    return u


@router.get("/password")
def password_form(request: Request, db: Session = Depends(get_db)):
    u = _own_user(request, db)
    return render(request, "admin/password.html", {"errs": [], "forced": u.must_change_password})


@router.post("/password")
def password_post(request: Request, form=Depends(get_form), db: Session = Depends(get_db)):
    u = _own_user(request, db)
    try:
        security.rate_check(db, f"pwchange:{u.id}", 10, 600)
        staffauth.change_own_password(db, u, form.get("current", ""), form.get("password", ""), form.get("confirm", ""))
    except UserError as e:
        db.commit()
        return render(request, "admin/password.html", {"errs": [e.message], "forced": u.must_change_password}, status=400)
    db.commit()
    resp = RedirectResponse("/admin", status_code=303)  # old sessions were invalidated; issue a fresh one
    resp.set_cookie(staffauth.COOKIE, staffauth.make_session_token(u), max_age=staffauth.SESSION_MAX_AGE,
                    httponly=True, samesite="lax", secure=get_config().secure_cookies)
    return resp


@router.post("/logout")
def logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie(staffauth.COOKIE)
    return resp


# --- user management (admin only) ----------------------------------------------------
def _users_ctx(db, errs=None, msg=None):
    rows = db.scalars(select(StaffUser).order_by(StaffUser.username)).all()
    return {"users": rows, "roles": staffauth.ROLE_ORDER, "info": staffauth.ROLE_INFO, "errs": errs or [], "msg": msg,
            "roles_of": staffauth.roles_of, "perm_desc": staffauth.PERM_DESCRIPTIONS}


@router.get("/users")
def users(request: Request, user: StaffUser = Depends(staff_dep("admin")), db: Session = Depends(get_db)):
    return render(request, "admin/users.html", _users_ctx(db, msg=request.query_params.get("msg")))


@router.post("/users")
def users_post(request: Request, form=Depends(get_form), user: StaffUser = Depends(staff_dep("admin")),
               db: Session = Depends(get_db)):
    action = form.get("action")
    try:
        if action == "create":
            chosen = form.getlist("roles") or ([form["role"]] if form.get("role") else [])
            staffauth.create_user(db, form.get("username", ""), form.get("password", ""), chosen[0] if chosen else "",
                                  form.get("display_name", ""), form.get("email") or None, staff_actor(user), roles=chosen,
                                  email_password=True)
        elif action in ("deactivate", "activate", "reset_password", "set_roles"):
            target = db.get(StaffUser, int(form.get("id", 0)))
            if target is None:
                raise UserError("Unknown user.")
            if action == "deactivate":
                if target.id == user.id:
                    raise UserError("You can't deactivate yourself.")
                if "admin" in staffauth.roles_of(target):
                    others = db.scalar(select(func.count()).select_from(StaffUser).where(
                        StaffUser.active.is_(True), StaffUser.id != target.id, StaffUser.role == "admin")) or 0
                    if not others:
                        raise UserError("There must always be at least one active admin.")
                target.active = False
                target.session_epoch += 1
                audit.record(db, staff_actor(user), "staff.deactivate", "staff_user", target.id)
            elif action == "activate":
                target.active = True
                audit.record(db, staff_actor(user), "staff.activate", "staff_user", target.id)
            elif action == "set_roles":
                staffauth.set_roles(db, staff_actor(user), target, form.getlist("roles"))
            else:
                staffauth.admin_reset_password(db, staff_actor(user), target, form.get("password", ""))
    except UserError as e:
        db.rollback()
        return render(request, "admin/users.html", _users_ctx(db, [e.message]), status=400)
    return RedirectResponse("/admin/users?msg=Saved", status_code=303)


@router.get("/audit")
def audit_log(request: Request, q: str = "", page: int = 1, user: StaffUser = Depends(staff_dep("admin")),
              db: Session = Depends(get_db)):
    stmt = select(AuditLog).order_by(AuditLog.id.desc())
    if q:
        like = f"%{q}%"
        stmt = stmt.where(AuditLog.actor.like(like) | AuditLog.action.like(like) | AuditLog.entity.like(like))
    page = max(page, 1)
    rows = db.scalars(stmt.limit(101).offset((page - 1) * 100)).all()
    return render(request, "admin/audit.html", {"rows": rows[:100], "q": q, "page": page, "more": len(rows) > 100})

