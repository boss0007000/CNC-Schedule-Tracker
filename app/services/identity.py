"""Student identity. The rest of the system only depends on `IdentityProvider`:
swap `EmailCodeProvider` for an SSO provider that returns the same `Identity` and nothing else changes."""
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Protocol

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock, emailer, security
from app.errors import UserError
from app.models import STANDINGS, EmailVerification, Student
from app.services import audit
from app.services import settings as shop_settings

COOKIE = "student_session"
COOKIE_MAX_AGE = 8 * 3600
CODE_TTL = timedelta(minutes=10)
MAX_ATTEMPTS = 5
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-']+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


@dataclass
class Identity:
    email: str  # verified, lower-cased: the internal key for a student


class IdentityProvider(Protocol):
    def current(self, request: Request) -> Identity | None: ...


class EmailCodeProvider:
    """Reads the signed verified-session cookie set after a successful code check."""

    def current(self, request: Request) -> Identity | None:
        token = request.cookies.get(COOKIE)
        if not token:
            return None
        data = security.unsign("student-session", token, COOKIE_MAX_AGE)
        return Identity(email=data["email"]) if data else None


provider: IdentityProvider = EmailCodeProvider()


def session_token(email: str) -> str:
    return security.sign("student-session", {"email": email})


def current_student(db: Session, request: Request) -> Student | None:
    ident = provider.current(request)
    if ident is None:
        return None
    return db.scalar(select(Student).where(Student.email == ident.email))


# --- code issuing / verification ---------------------------------------------------
def _hash(email: str, code: str) -> str:
    return hmac.new(security.get_config().secret_key.encode(), f"{email}:{code}".encode(),
                    hashlib.sha256).hexdigest()


def normalize_email(raw: str) -> str:
    return (raw or "").strip().lower()


def validate_profile(db: Session, email: str, full_name: str, standing: str, course: str | None,
                     student_id: str | None, existing: Student | None) -> dict:
    cfg = shop_settings.load(db)
    if not EMAIL_RE.match(email) or len(email) > 254:
        raise UserError("Enter a valid email address.", field="email")
    if not email.endswith("@" + cfg.email_domain):
        raise UserError(f"Please use your @{cfg.email_domain} campus email address.", field="email")
    full_name = " ".join((full_name or "").split())
    if existing and not full_name:
        full_name = existing.full_name
    if len(full_name) < 2 or len(full_name) > 120:
        raise UserError("Enter your full name.", field="full_name")
    standing = (standing or (existing.standing if existing else "")).lower()
    if standing not in STANDINGS:
        raise UserError("Choose your academic standing.", field="standing")
    profile = {"full_name": full_name, "standing": standing,
               "course": (course or "").strip()[:120] or None}
    if cfg.collect_student_id:
        sid = (student_id or "").strip()
        if sid and not re.fullmatch(r"[A-Za-z0-9\-]{4,30}", sid):
            raise UserError("Student ID format looks wrong.", field="student_id")
        profile["student_id"] = sid or None
    return profile


def issue_code(db: Session, email: str, profile: dict, ip: str) -> str:
    """Create + email a 6-digit code. Rate-limited per email and per IP."""
    security.rate_check(db, f"code:email:{email}", 3, 600,
                        "We already sent several codes to this address. Wait 10 minutes, then try again.")
    security.rate_check(db, f"code:ip:{ip}", 10, 600)
    code = f"{secrets.randbelow(10**6):06d}"
    now = clock.now()
    db.add(EmailVerification(email=email, code_hash=_hash(email, code), expires_at=now + CODE_TTL,
                             ip=ip, pending_profile=profile))
    db.flush()
    shop = shop_settings.load(db).shop_name
    emailer.queue(db, email, f"Your {shop} verification code: {code}",
                  f"Your verification code is {code}.\n\nIt expires in 10 minutes. "
                  f"If you didn't request it, ignore this email.")
    return code


def check_code(db: Session, email: str, code: str, ip: str) -> Student:
    """Verify the newest unconsumed code; create/update the student record on success."""
    security.rate_check(db, f"verify:ip:{ip}", 30, 600)
    now = clock.now()
    ver = db.scalar(select(EmailVerification)
                    .where(EmailVerification.email == email, EmailVerification.consumed_at.is_(None))
                    .order_by(EmailVerification.id.desc()).limit(1))
    code = (code or "").strip().replace(" ", "")
    if ver is None or ver.expires_at <= now:
        raise UserError("That code has expired. Request a new one.", code="expired")
    if ver.attempts >= MAX_ATTEMPTS:
        raise UserError("Too many wrong attempts for this code. Request a new one.", code="locked")
    ver.attempts += 1
    if not hmac.compare_digest(ver.code_hash, _hash(email, code)):
        left = MAX_ATTEMPTS - ver.attempts
        raise UserError(f"That code is not correct. {left} attempt{'s' if left != 1 else ''} left.",
                        code="wrong_code", field="code")
    ver.consumed_at = now
    p = ver.pending_profile or {}
    student = db.scalar(select(Student).where(func.lower(Student.email) == email))
    if student is None:
        student = Student(email=email, full_name=p.get("full_name", email.split("@")[0]),
                          standing=p.get("standing", "freshman"), course=p.get("course"),
                          student_id=p.get("student_id"))
        db.add(student)
        db.flush()
        audit.record(db, f"student:{email}", "student.create", "student", student.id)
    else:
        student.full_name = p.get("full_name") or student.full_name
        student.standing = p.get("standing") or student.standing
        student.course = p.get("course") or student.course
        if "student_id" in p and p["student_id"]:
            student.student_id = p["student_id"]
    student.last_verified_at = now
    return student
