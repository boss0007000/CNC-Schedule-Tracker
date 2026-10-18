import logging
import secrets
import sys
import time

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select, text
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import db as dbm
from app import security, web
from app.config import get_config
from app.errors import UserError
from app.models import StaffUser

log = logging.getLogger("app")


def _configure_logging():
    import json

    class JsonFmt(logging.Formatter):
        def format(self, r):  # structured logs
            d = {"t": self.formatTime(r), "lvl": r.levelname, "logger": r.name, "msg": r.getMessage()}
            if r.exc_info:
                d["exc"] = self.formatException(r.exc_info)
            return json.dumps(d)

    h = logging.StreamHandler(sys.stdout)
    if get_config().env == "prod":
        h.setFormatter(JsonFmt())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(logging.INFO)


def _apply_branding(db):
    """Rename the old default shop name and repair 'Hass'-style misspellings of Haas in machine names."""
    import re

    from app.models import Machine, Setting
    row = db.get(Setting, "shop_name")
    if row is not None and str(row.value).strip().strip('"') in ("Student Machine Shop", "Machine Shop", ""):
        row.value = "GENE HAAS ADVANCED MANUFACTURING LAB"
    for m in db.scalars(select(Machine)):
        fixed = re.sub(r"\b(hass|haus|haaz|hasss|haas)\b", "Haas", m.name, flags=re.I)
        if fixed != m.name:
            m.name = fixed


def bootstrap(db):
    """Base data on every boot; first admin from env vars (and an env-driven password reset for recovery)."""
    from sqlalchemy import func

    from app.services import audit, shopconfig, staffauth
    shopconfig.ensure_base_data(db)
    _apply_branding(db)
    c = get_config()
    if not (c.bootstrap_admin_username and c.bootstrap_admin_password):
        return
    uname = c.bootstrap_admin_username.strip().lower()
    existing = db.scalar(select(StaffUser).where(func.lower(StaffUser.username) == uname))
    any_staff = db.scalar(select(StaffUser.id).limit(1)) is not None
    if existing is None and (not any_staff or c.bootstrap_reset):
        staffauth.create_user(db, uname, c.bootstrap_admin_password, "admin", display_name="Administrator")
        log.warning("bootstrap: created admin account %r", uname)
    elif existing is not None and c.bootstrap_reset:
        staffauth.set_password(db, existing, c.bootstrap_admin_password, "system:bootstrap")
        existing.active, existing.role, existing.failed_logins, existing.locked_until = True, "admin", 0, None
        audit.record(db, "system:bootstrap", "staff.bootstrap_reset", "staff_user", existing.id)
        log.warning("bootstrap: reset password for %r (remove BOOTSTRAP_RESET now)", uname)
    else:
        log.warning("bootstrap: account %r not changed (accounts already exist; set BOOTSTRAP_RESET=true to reset it)", uname)


def create_app() -> FastAPI:
    from contextlib import asynccontextmanager

    from app import jobs

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _configure_logging()
        get_config().validate_for_prod()
        dbm.configure() if dbm._engine is None else None
        with dbm.session_scope() as s:
            try:
                bootstrap(s)
            except UserError as e:
                log.error("BOOTSTRAP ADMIN NOT CREATED: %s", e.message)
            except Exception:  # noqa: BLE001 - tables may not exist yet (pre-migration)
                log.exception("bootstrap skipped")
        sched = jobs.start() if get_config().scheduler_enabled and get_config().env != "test" else None
        yield
        if sched:
            sched.shutdown(wait=False)

    app = FastAPI(title="Gene Haas Advanced Manufacturing Lab", lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None, dependencies=[Depends(web.csrf_protect)])
    app.mount("/static", StaticFiles(directory=str(web.BASE / "static")), name="static")

    @app.middleware("http")
    async def mw(request: Request, call_next):
        cfg = get_config()
        t0 = time.monotonic()
        if cfg.force_https and request.headers.get("x-forwarded-proto", request.url.scheme) != "https" \
                and request.url.path != "/healthz":
            from fastapi.responses import RedirectResponse
            return RedirectResponse(str(request.url.replace(scheme="https")), status_code=308)
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > 60 * 1024 * 1024:
            return PlainTextResponse("Request too large", status_code=413)
        resp = await call_next(request)
        h = resp.headers
        h["X-Content-Type-Options"] = "nosniff"
        h["X-Frame-Options"] = "DENY"
        h["Referrer-Policy"] = "same-origin"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        h["Content-Security-Policy"] = ("default-src 'self'; img-src 'self' data:; style-src 'self'; "
                                        "script-src 'self'; form-action 'self'; frame-ancestors 'none'; "
                                        "base-uri 'self'")
        if cfg.secure_cookies or cfg.force_https:
            h["Strict-Transport-Security"] = "max-age=31536000"
        if getattr(request.state, "csrf_new", None):
            resp.set_cookie(security.CSRF_COOKIE, request.state.csrf_new, httponly=True, samesite="lax",
                            secure=cfg.secure_cookies, max_age=86400)
        if not request.url.path.startswith(("/static", "/qr")):
            h.setdefault("Cache-Control", "no-store")  # pages carry personal data: never cache
        log.info("%s %s %s %.0fms", request.method, request.url.path, resp.status_code,
                 (time.monotonic() - t0) * 1000)
        return resp

    @app.exception_handler(web.NeedsVerification)
    async def _nv(request, exc):
        return web.needs_verification_response(exc)

    @app.exception_handler(web.NeedsPasswordChange)
    async def _npc(request, exc):
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/admin/password", status_code=303)

    @app.exception_handler(web.NeedsStaffLogin)
    async def _nl(request, exc):
        return web.needs_login_response(exc)

    @app.exception_handler(UserError)
    async def _ue(request, exc: UserError):
        return web.render(request, "error.html", {"title": "Can't do that", "message": exc.message,
                                                  "back_url": web.back_url(request)},
                          status=429 if exc.code == "rate_limited" else 400)

    @app.exception_handler(StarletteHTTPException)
    async def _http(request, exc: StarletteHTTPException):
        if request.url.path == "/tv/data" or request.headers.get("accept", "").startswith("application/json"):
            return JSONResponse({"error": exc.detail}, status_code=exc.status_code)
        msg = {404: "That page doesn't exist.", 403: str(exc.detail)}.get(exc.status_code, str(exc.detail))
        retry = None
        if exc.status_code == 403 and "form expired" in str(exc.detail):
            # Going 'back' would resubmit the stale form (same bad token). Offer a fresh copy of the same page instead.
            from urllib.parse import urlparse
            ref = urlparse(request.headers.get("referer", ""))
            retry = web.safe_next(ref.path + (f"?{ref.query}" if ref.query else ""), "/") if ref.path else "/"
        return web.render(request, "error.html", {"title": f"Error {exc.status_code}", "message": msg, "retry_url": retry,
                                                  "back_url": web.back_url(request)},
                          status=exc.status_code)

    @app.exception_handler(Exception)
    async def _500(request, exc):
        log.exception("unhandled error")
        return web.render(request, "error.html", {"title": "Something went wrong",
                          "message": "An unexpected error occurred. Please try again; if it keeps happening, ask shop staff."},
                          status=500)

    @app.get("/healthz")
    def healthz():
        try:
            with dbm.engine().connect() as c:
                c.execute(text("select 1"))
        except Exception:  # noqa: BLE001
            return JSONResponse({"status": "db_down"}, status_code=503)
        return {"status": "ok"}

    from app.routers import public, verify
    app.include_router(public.router)
    app.include_router(verify.router)
    for mod in ("submit", "reserve", "checkin", "my", "schedule", "requests", "holds", "tv", "admin_core", "admin_ops", "admin_reports"):
        try:
            m = __import__(f"app.routers.{mod}", fromlist=["router"])
        except ModuleNotFoundError as e:
            if e.name != f"app.routers.{mod}":
                raise
            continue
        app.include_router(m.router)
    return app


app = create_app()
