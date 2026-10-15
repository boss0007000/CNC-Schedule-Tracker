"""Staff accounts: argon2 passwords, lockout, expiring signed sessions, role hierarchy."""
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock, security
from app.errors import UserError
from app.models import ROLES, StaffUser
from app.services import audit

MAX_FAILURES = 5
LOCKOUT = timedelta(minutes=15)
SESSION_MAX_AGE = 8 * 3600
COOKIE = "staff_session"


def role_rank(role: str) -> int:
    return ROLES.index(role)


def has_role(user: StaffUser, minimum: str) -> bool:
    return role_rank(user.role) >= role_rank(minimum)


def create_user(db: Session, username: str, password: str, role: str, display_name: str = "",
                email: str | None = None, actor: str = "system") -> StaffUser:
    username = username.strip().lower()
    if role not in ROLES:
        raise UserError("Unknown role.")
    if len(password) < 10:
        raise UserError("Password must be at least 10 characters.", field="password")
    if db.scalar(select(StaffUser).where(func.lower(StaffUser.username) == username)):
        raise UserError("That username is already taken.", field="username")
    u = StaffUser(username=username, display_name=display_name or username,
                  password_hash=security.hash_password(password), role=role, email=email)
    db.add(u)
    db.flush()
    audit.record(db, actor, "staff.create", "staff_user", u.id, after={"username": username, "role": role})
    return u


def authenticate(db: Session, username: str, password: str, ip: str | None = None) -> StaffUser:
    now = clock.now()
    u = db.scalar(select(StaffUser).where(func.lower(StaffUser.username) == username.strip().lower()))
    generic = UserError("Incorrect username or password.", code="bad_login")
    if u is None or not u.active:
        security.hash_password("x")  # equalize timing a little
        raise generic
    if u.locked_until and u.locked_until > now:
        raise UserError("Too many failed attempts. This account is locked for a few minutes.", code="locked")
    if not security.verify_password(u.password_hash, password):
        u.failed_logins += 1
        if u.failed_logins >= MAX_FAILURES:
            u.locked_until = now + LOCKOUT
            u.failed_logins = 0
            audit.record(db, f"staff:{u.username}", "staff.locked", "staff_user", u.id, ip=ip)
        raise generic
    u.failed_logins = 0
    u.locked_until = None
    u.last_login_at = now
    audit.record(db, f"staff:{u.username}", "staff.login", "staff_user", u.id, ip=ip)
    return u


def make_session_token(u: StaffUser) -> str:
    return security.sign("staff-session", {"id": u.id, "epoch": u.session_epoch})


def user_from_token(db: Session, token: str | None) -> StaffUser | None:
    if not token:
        return None
    data = security.unsign("staff-session", token, SESSION_MAX_AGE)
    if not data:
        return None
    u = db.get(StaffUser, data["id"])
    if u is None or not u.active or u.session_epoch != data["epoch"]:
        return None
    return u


def set_password(db: Session, u: StaffUser, new_password: str, actor: str) -> None:
    if len(new_password) < 10:
        raise UserError("Password must be at least 10 characters.", field="password")
    u.password_hash = security.hash_password(new_password)
    u.session_epoch += 1  # logs out existing sessions
    audit.record(db, actor, "staff.password_change", "staff_user", u.id)
