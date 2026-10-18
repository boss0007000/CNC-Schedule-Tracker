"""Staff accounts: argon2 passwords, lockout, expiring signed sessions, role hierarchy."""
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock, security
from app.errors import UserError
from app.models import StaffUser
from app.services import audit

MAX_FAILURES = 5
LOCKOUT = timedelta(minutes=15)
SESSION_MAX_AGE = 8 * 3600
COOKIE = "staff_session"


# --- permissions -------------------------------------------------------------------------------
# Every capability is a named permission; roles are presets of permissions; a person can hold several roles.
PERM_DESCRIPTIONS = {
    "view": "See every admin page (read-only)",
    "checkins.decide": "Approve or deny student check-ins",
    "sessions.force": "Force check-in/out, adjust or close sessions, review auto-closed sessions",
    "incidents.write": "Record incidents and update damage reports",
    "machines.status": "Override machine status, log maintenance, clear inspections, place/release holds",
    "emergency": "Emergency close and reopen the shop",
    "projects.review": "Approve, reject or send back projects/parts; revoke approvals",
    "certs.manage": "Grant and revoke certifications",
    "access.approved": "Add/remove students on the Approved list",
    "access.super": "Add/remove students on the Super approved list",
    "students.suspend": "Suspend and unsuspend booking",
    "reservations.manage": "Create, edit, cancel, release reservations and edit booked times",
    "admin": "Users, settings, machines, access grid, backups, audit log, sensitive exports",
}
_OPS = {"view", "checkins.decide", "sessions.force", "incidents.write", "machines.status", "emergency", "projects.review",
        "certs.manage", "access.approved", "students.suspend", "reservations.manage"}
ROLE_INFO: dict[str, tuple[str, str, set[str]]] = {
    "monitor": ("Monitor", "Read-only: can look at everything, change nothing.", {"view"}),
    "maintenance": ("Maintenance", "Keeps machines running: maintenance log, repairs, inspection clearance, damage reports.",
                    {"view", "machines.status", "incidents.write"}),
    "scheduler": ("Scheduler", "Runs the booking calendar: create, edit and cancel reservations.", {"view", "reservations.manage"}),
    "trainer": ("Trainer", "Signs people off: certifications and the Approved student list.",
                {"view", "certs.manage", "access.approved"}),
    "part_reviewer": ("Part reviewer", "Reviews projects and parts: approve, reject, request changes, revoke approvals.",
                      {"view", "projects.review"}),
    "floor_supervisor": ("Floor supervisor (safety)", "On the shop floor making sure people work safely: approves check-ins, "
                         "forces check-in/out, incidents, machine status and holds, emergency close, suspensions.",
                         {"view", "checkins.decide", "sessions.force", "incidents.write", "machines.status", "emergency",
                          "students.suspend"}),
    "supervisor": ("Lead supervisor", "Everything operational (all of the above), but not users, settings or the audit log.",
                   set(_OPS)),
    "admin": ("Admin", "Everything, including users, settings, machines, the access grid, backups and the audit log.",
              set(_OPS) | {"access.super", "admin"}),
}
ROLE_ORDER = ["monitor", "maintenance", "scheduler", "trainer", "part_reviewer", "floor_supervisor", "supervisor", "admin"]


def role_rank(role: str) -> int:
    return ROLE_ORDER.index(role) if role in ROLE_ORDER else 0


def roles_of(user: StaffUser) -> set[str]:
    return {r for r in (list(user.roles or []) + [user.role]) if r in ROLE_INFO}


def perms_of(user: StaffUser) -> frozenset[str]:
    out: set[str] = {"view"}
    for r in roles_of(user):
        out |= ROLE_INFO[r][2]
    return frozenset(out)


def can(user: StaffUser, perm: str) -> bool:
    return perm in perms_of(user)


def primary_role(roles: set[str] | list[str]) -> str:
    rs = [r for r in roles if r in ROLE_INFO]
    return max(rs, key=role_rank) if rs else "monitor"


def has_role(user: StaffUser, minimum: str) -> bool:  # legacy rank check
    return role_rank(user.role) >= role_rank(minimum)


def normalize_roles(roles) -> list[str]:
    clean = [r for r in ROLE_ORDER if r in set(roles or [])]
    if not clean:
        raise UserError("Choose at least one role.")
    return clean


def set_roles(db: Session, actor: str, user: StaffUser, roles) -> None:
    clean = normalize_roles(roles)
    before = sorted(roles_of(user))
    if "admin" in before and "admin" not in clean:
        other_admins = db.scalar(select(func.count()).select_from(StaffUser).where(
            StaffUser.active.is_(True), StaffUser.id != user.id, StaffUser.role == "admin")) or 0
        if not other_admins:
            raise UserError("There must always be at least one active admin. Make someone else an admin first.")
    user.roles, user.role = clean, primary_role(clean)
    user.session_epoch += 1  # sign them out so the new permissions apply immediately
    audit.record(db, actor, "staff.roles", "staff_user", user.id, {"roles": before}, {"roles": clean})


def create_user(db: Session, username: str, password: str, role: str, display_name: str = "",
                email: str | None = None, actor: str = "system", roles: list[str] | None = None) -> StaffUser:
    username = username.strip().lower()
    chosen = normalize_roles(roles if roles else [role])
    if len(password) < 10:
        raise UserError("Password must be at least 10 characters.", field="password")
    if db.scalar(select(StaffUser).where(func.lower(StaffUser.username) == username)):
        raise UserError("That username is already taken.", field="username")
    u = StaffUser(username=username, display_name=display_name or username,
                  password_hash=security.hash_password(password), role=primary_role(chosen), roles=chosen, email=email)
    db.add(u)
    db.flush()
    audit.record(db, actor, "staff.create", "staff_user", u.id, after={"username": username, "roles": chosen})
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
