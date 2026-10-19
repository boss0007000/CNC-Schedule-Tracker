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
    "machines.status": "Place machine holds and maintenance, release holds you placed yourself",
    "machines.safety_clear": "Lift safety holds placed by someone else: maintenance/inspection holds and status overrides",
    "training.any": "Certify students on ANY machine (otherwise trainers only certify on machines they are authorized for)",
    "emergency": "Emergency close and reopen the shop",
    "projects.review": "Approve, reject or send back projects/parts; revoke approvals",
    "certs.manage": "Grant and revoke certifications",
    "access.approved": "Add/remove students on the Approved list",
    "access.super": "Add/remove students on the Super approved list",
    "students.suspend": "Give justified strikes (3 active = a time-limited suspension), void strikes, lift suspensions",
    "reservations.manage": "Create, edit, cancel, release reservations and edit booked times",
    "admin": "Users, settings, machines, access grid, backups, audit log, sensitive exports",
}
_OPS = {"view", "checkins.decide", "sessions.force", "incidents.write", "machines.status", "machines.safety_clear", "training.any", "emergency",
        "projects.review", "certs.manage", "access.approved", "students.suspend", "reservations.manage"}
ROLE_INFO: dict[str, tuple[str, str, set[str]]] = {
    "monitor": ("Monitor", "Read-only: can look at everything, change nothing.", {"view"}),
    "maintenance": ("Maintenance", "Keeps machines running: maintenance log, repairs, inspection clearance, damage reports.",
                    {"view", "machines.status", "machines.safety_clear", "incidents.write"}),
    "scheduler": ("Scheduler", "Runs the booking calendar: create, edit and cancel reservations.", {"view", "reservations.manage"}),
    "trainer": ("Trainer", "Signs people off, only on the machines an admin has authorized them for, plus the Approved student list.",
                {"view", "certs.manage", "access.approved"}),
    "part_reviewer": ("Part reviewer", "Reviews projects and parts: approve, reject, request changes, revoke approvals.",
                      {"view", "projects.review"}),
    "floor_supervisor": ("Floor supervisor (safety)", "On the shop floor making sure people work safely: approves check-ins, "
                         "forces check-in/out, incidents, places holds (cannot lift maintenance holds), emergency close, suspensions.",
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


def generate_password() -> str:
    """Readable temporary password (no look-alike characters), ~70 bits."""
    import secrets
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return "-".join("".join(secrets.choice(alphabet) for _ in range(5)) for _ in range(3))


def email_credentials(db: Session, u: StaffUser, password: str, reason: str) -> None:
    from app.config import get_config
    from app.emailer import SECRET_SUBJECT_PREFIX, queue
    from app.services import settings as shop_settings
    shop = shop_settings.load(db).shop_name
    base = get_config().base_url.rstrip("/")
    text = (f"Hi {u.display_name},\n\n{reason}\n\nSign in: {base}/admin/login\nUsername: {u.username}\n"
            f"Temporary password: {password}\n\nYou will be asked to choose your own password the first time you sign in. "
            f"Do not share this email or your password.\n\n{shop}")
    queue(db, u.email, f"{SECRET_SUBJECT_PREFIX} for {shop}", text)


def create_user(db: Session, username: str, password: str, role: str, display_name: str = "",
                email: str | None = None, actor: str = "system", roles: list[str] | None = None,
                email_password: bool = False) -> StaffUser:
    """email_password=True: generate a temporary password when none is given, email it and force a change at first login."""
    username = username.strip().lower()
    chosen = normalize_roles(roles if roles else [role])
    auto = email_password
    if auto:
        if not email:
            raise UserError("An email address is required so the temporary password can be sent.", field="email")
        password = password or generate_password()
    if len(password) < 10:
        raise UserError("Password must be at least 10 characters.", field="password")
    if db.scalar(select(StaffUser).where(func.lower(StaffUser.username) == username)):
        raise UserError("That username is already taken.", field="username")
    if username in deleted_usernames(db):
        raise UserError("That username belonged to a deleted account. Choose a different one so the audit history stays unambiguous.",
                        field="username")
    u = StaffUser(username=username, display_name=display_name or username,
                  password_hash=security.hash_password(password), role=primary_role(chosen), roles=chosen, email=email,
                  must_change_password=auto)
    db.add(u)
    db.flush()
    if auto:
        email_credentials(db, u, password, "An account has been created for you.")
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
    u.must_change_password = False
    u.session_epoch += 1  # logs out existing sessions
    audit.record(db, actor, "staff.password_change", "staff_user", u.id)


def admin_reset_password(db: Session, actor: str, u: StaffUser, new_password: str = "") -> None:
    """Admin reset: blank = generate one and email it. Either way the person must choose their own at next login."""
    if not new_password and not u.email:
        raise UserError("This person has no email address on file. Type a temporary password instead.", field="password")
    pw = new_password or generate_password()
    set_password(db, u, pw, actor)
    u.must_change_password = True
    if u.email:
        email_credentials(db, u, pw, "Your password was reset by an administrator.")


def change_own_password(db: Session, u: StaffUser, current: str, new: str, confirm: str) -> None:
    if not security.verify_password(u.password_hash, current):
        raise UserError("Your current password is incorrect.", field="current")
    if new != confirm:
        raise UserError("The new passwords don't match.", field="password")
    if security.verify_password(u.password_hash, new):
        raise UserError("Choose a new password that is different from the temporary one.", field="password")
    set_password(db, u, new, f"staff:{u.username}")


# --- machine-specific training authorization ----------------------------------------------------
def training_scope(db: Session, user: StaffUser) -> tuple[bool, set[int], set[int]]:
    """(unrestricted, machine_ids, group_ids) this person may certify students on."""
    from app.models import TrainerAuthorization
    if can(user, "training.any"):
        return True, set(), set()
    rows = db.scalars(select(TrainerAuthorization).where(TrainerAuthorization.staff_id == user.id)).all()
    return False, {r.machine_id for r in rows if r.machine_id}, {r.group_id for r in rows if r.group_id}


def can_train(db: Session, user: StaffUser, machine_id: int | None = None, group_id: int | None = None) -> bool:
    from app.models import Machine
    if not can(user, "certs.manage"):
        return False
    free, ms, gs = training_scope(db, user)
    if free:
        return True
    if group_id:
        return group_id in gs
    if machine_id:
        m = db.get(Machine, machine_id)
        return machine_id in ms or bool(m and m.group_id and m.group_id in gs)
    return False


def set_training(db: Session, actor: str, target: StaffUser, machine_ids: list[int], group_ids: list[int]) -> None:
    from app.models import Machine, MachineGroup, TrainerAuthorization
    machine_ids = {i for i in machine_ids if db.get(Machine, i)}
    group_ids = {i for i in group_ids if db.get(MachineGroup, i)}
    before = training_scope(db, target)
    for row in db.scalars(select(TrainerAuthorization).where(TrainerAuthorization.staff_id == target.id)):
        db.delete(row)
    db.flush()
    who = actor.split(":", 1)[-1]
    for i in sorted(machine_ids):
        db.add(TrainerAuthorization(staff_id=target.id, machine_id=i, granted_by=who))
    for i in sorted(group_ids):
        db.add(TrainerAuthorization(staff_id=target.id, group_id=i, granted_by=who))
    audit.record(db, actor, "staff.training_authorization", "staff_user", target.id,
                 before={"machines": sorted(before[1]), "groups": sorted(before[2])},
                 after={"machines": sorted(machine_ids), "groups": sorted(group_ids)})


def deleted_usernames(db: Session) -> set[str]:
    from app.models import AuditLog
    return {(r or {}).get("username", "") for r in db.scalars(select(AuditLog.before).where(AuditLog.action == "staff.delete"))}


def delete_user(db: Session, actor: str, target: StaffUser, confirm_username: str) -> None:
    """Remove the account but keep every audit entry they ever made (actions are stored under their username).
    The deletion itself is logged with a snapshot of who they were. Their username can't be reused."""
    from app.models import ShiftSchedule, TrainerAuthorization
    if (confirm_username or "").strip().lower() != target.username:
        raise UserError(f"Type the username ({target.username}) to confirm the deletion.")
    if actor == f"staff:{target.username}":
        raise UserError("You can't delete yourself.")
    if "admin" in roles_of(target) and target.active and not db.scalar(
            select(func.count()).select_from(StaffUser).where(StaffUser.active.is_(True), StaffUser.id != target.id,
                                                              StaffUser.role == "admin")):
        raise UserError("There must always be at least one active admin.")
    snap = {"username": target.username, "display_name": target.display_name, "email": target.email,
            "roles": sorted(roles_of(target)), "created_at": target.created_at, "last_login_at": target.last_login_at,
            "was_active": target.active}
    n_shifts = db.query(ShiftSchedule).filter(ShiftSchedule.staff_id == target.id).delete()
    db.query(TrainerAuthorization).filter(TrainerAuthorization.staff_id == target.id).delete()
    audit.record(db, actor, "staff.delete", "staff_user", target.id, before=snap, after={"shifts_removed": n_shifts})
    db.delete(target)
    db.flush()
