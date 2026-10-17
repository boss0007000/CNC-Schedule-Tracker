"""Templating, request dependencies and shared page helpers."""
from datetime import datetime
from types import SimpleNamespace
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import clock, security
from app.db import get_db
from app.models import StaffUser, Student
from app.services import identity, staffauth
from app.services import settings as shop_settings

import logging

_log = logging.getLogger("csrf")
BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


# --- template filters -----------------------------------------------------------
def _local(dt: datetime | None, fmt: str = "%a %b %d, %I:%M %p") -> str:
    if dt is None:
        return "—"
    s = clock.to_local(dt).strftime(fmt)
    return s.replace(" 0", " ") if fmt.startswith("%a") else s


def _localtime(dt: datetime | None) -> str:
    return "—" if dt is None else clock.to_local(dt).strftime("%I:%M %p").lstrip("0")


def _localdate(dt: datetime | None) -> str:
    return "—" if dt is None else clock.to_local(dt).strftime("%a %b %d, %Y")


def _localinput(dt: datetime | None) -> str:
    return "" if dt is None else clock.to_local(dt).strftime("%Y-%m-%dT%H:%M")


def _hours(h: float | None) -> str:
    return "—" if h is None else f"{h:g}"


from app.services import contact as _contact  # noqa: E402

def _asset(name: str) -> str:
    """Static URL with a version stamp so browsers (and the TV!) pick up updates immediately after a deploy."""
    try:
        v = int((BASE / "static" / name).stat().st_mtime)
    except OSError:
        v = 0
    return f"/static/{name}?v={v}"


templates.env.globals["asset"] = _asset
templates.env.globals["consent_text"] = _contact.ACK
templates.env.globals["contact_choices"] = _contact.CHOICES
templates.env.filters.update(pretty_phone=_contact.pretty, local=_local, localtime=_localtime, localdate=_localdate,
                             localinput=_localinput, hours=_hours)


def render(request: Request, name: str, ctx: dict | None = None, status: int = 200, **extra):
    data = {"request": request, "csrf_token": security.csrf_token(request),
            "form_token": security.new_form_token(), "now": clock.now(),
            "staff": getattr(request.state, "staff", None), "tz": str(clock.tz())}
    data.update(ctx or {})
    data.update(extra)
    resp = templates.TemplateResponse(request, name, data, status_code=status)
    return resp


def safe_next(nxt: str | None, default: str = "/") -> str:
    if nxt and nxt.startswith("/") and not nxt.startswith("//") and "\\" not in nxt:
        return nxt
    return default


# --- exceptions turned into redirects -------------------------------------------------
class NeedsVerification(Exception):
    def __init__(self, next_url: str):
        self.next_url = next_url


class NeedsStaffLogin(Exception):
    def __init__(self, next_url: str):
        self.next_url = next_url


def needs_verification_response(exc: NeedsVerification):
    return RedirectResponse(f"/verify?next={quote(exc.next_url)}", status_code=303)


def needs_login_response(exc: NeedsStaffLogin):
    return RedirectResponse(f"/admin/login?next={quote(exc.next_url)}", status_code=303)


# --- dependencies -------------------------------------------------------------------
async def csrf_protect(request: Request):
    """Global: parse the form once (cached) and verify the CSRF token on every POST."""
    if request.method == "POST":
        form = await request.form()
        if not security.csrf_ok(request, form.get("csrf_token")):
            _log.warning("csrf rejected path=%s cookie_present=%s token_present=%s ua=%s", request.url.path,
                         bool(request.cookies.get(security.CSRF_COOKIE)), bool(form.get("csrf_token")),
                         request.headers.get("user-agent", "")[:120])
            raise HTTPException(403, "Your form expired. Go back, reload the page and try again.")


async def get_form(request: Request):
    return await request.form()


def student_required(request: Request, db: Session = Depends(get_db)) -> Student:
    s = identity.current_student(db, request)
    if s is None:
        path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
        raise NeedsVerification(path)
    request.state.student = s
    return s


def public_post_guard(request: Request, form=Depends(get_form), db: Session = Depends(get_db)):
    """Bot protection for public POSTs: honeypot field + per-IP limit."""
    if request.method != "POST":
        return
    if form.get("website"):  # honeypot, hidden from humans
        raise HTTPException(400, "Submission rejected.")
    security.rate_check(db, f"post:ip:{security.client_ip(request)}", 120, 60,
                        "You're sending requests too quickly. Please wait a minute.")
    db.commit()


def staff_dep(minimum: str = "monitor"):
    def dep(request: Request, db: Session = Depends(get_db)) -> StaffUser:
        user = staffauth.user_from_token(db, request.cookies.get(staffauth.COOKIE))
        if user is None:
            path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
            raise NeedsStaffLogin(path)
        # Monitors are read-only: any state-changing request needs supervisor+.
        need = minimum if request.method in ("GET", "HEAD") else (
            minimum if staffauth.role_rank(minimum) >= staffauth.role_rank("supervisor") else "supervisor")
        if not staffauth.has_role(user, need):
            raise HTTPException(403, "Your role does not allow this action.")
        # plain snapshot: error pages render after the DB session is closed
        request.state.staff = SimpleNamespace(id=user.id, username=user.username,
                                              display_name=user.display_name, role=user.role)
        return user
    return dep


def staff_actor(user: StaffUser) -> str:
    return f"staff:{user.username}"


def settings_for(db: Session):
    return shop_settings.load(db)
