"""Signing, password hashing, CSRF, rate limiting, idempotency, client IP."""
import hmac
import secrets
from datetime import timedelta

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import clock
from app.config import get_config
from app.errors import RateLimited
from app.models import IdempotencyKey, RateEvent

_ph = PasswordHasher()


# --- passwords -----------------------------------------------------------------
def hash_password(pw: str) -> str:
    return _ph.hash(pw)


def verify_password(hashed: str, pw: str) -> bool:
    try:
        return _ph.verify(hashed, pw)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# --- signed tokens -------------------------------------------------------------
def _ser(salt: str) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_config().secret_key, salt=salt)


def sign(salt: str, data) -> str:
    return _ser(salt).dumps(data)


def unsign(salt: str, token: str, max_age: int):
    try:
        return _ser(salt).loads(token, max_age=max_age)
    except (BadSignature, SignatureExpired):
        return None


# --- client ip -----------------------------------------------------------------
def client_ip(request: Request) -> str:
    if get_config().trusted_proxy:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()[:64]
    return (request.client.host if request.client else "unknown")[:64]


# --- CSRF (double-submit cookie) -------------------------------------------------
CSRF_COOKIE = "csrf"


def csrf_token(request: Request) -> str:
    tok = request.cookies.get(CSRF_COOKIE) or getattr(request.state, "csrf_new", None)
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.state.csrf_new = tok
    return tok


def csrf_ok(request: Request, submitted: str | None) -> bool:
    cookie = request.cookies.get(CSRF_COOKIE)
    return bool(cookie and submitted and hmac.compare_digest(cookie, submitted))


# --- rate limiting ---------------------------------------------------------------
def rate_count(db: Session, key: str, window_s: int) -> int:
    since = clock.now() - timedelta(seconds=window_s)
    return db.scalar(select(func.count()).select_from(RateEvent)
                     .where(RateEvent.key == key, RateEvent.at > since)) or 0


def rate_hit(db: Session, key: str) -> None:
    db.add(RateEvent(key=key))
    db.flush()


def rate_check(db: Session, key: str, limit: int, window_s: int, message: str | None = None) -> None:
    """Count this attempt; raise RateLimited when over the limit."""
    if rate_count(db, key, window_s) >= limit:
        raise RateLimited(message) if message else RateLimited()
    rate_hit(db, key)


def rate_gc(db: Session, older_than_s: int = 86400) -> None:
    db.execute(delete(RateEvent).where(RateEvent.at < clock.now() - timedelta(seconds=older_than_s)))


# --- idempotent form submission -----------------------------------------------------
def new_form_token() -> str:
    return secrets.token_urlsafe(18)


def idem_lookup(db: Session, token: str | None) -> str | None:
    """Return the stored result URL if this form token was already processed."""
    if not token:
        return None
    row = db.get(IdempotencyKey, token)
    return row.result if row else None


def idem_store(db: Session, token: str | None, scope: str, result: str) -> None:
    if token and db.get(IdempotencyKey, token) is None:
        db.add(IdempotencyKey(token=token, scope=scope, result=result))
        db.flush()
