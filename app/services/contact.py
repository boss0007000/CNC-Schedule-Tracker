"""Phone number: required to reserve, and shown to other students on the schedule page and on the shop TV."""
import re

from sqlalchemy.orm import Session

from app import clock
from app.errors import UserError
from app.models import Student
from app.services import audit

ACK = ("What other students may see on the schedule page and the shop TV. Staff can always see how to reach you.")
CHOICES = [("phone", "My phone number (if I haven't added one, my campus email is shown)"),
           ("email", "My campus email"), ("none", "Nothing: keep my contact details private")]


def normalize_phone(raw: str) -> str | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if plus:
        ok, out = 8 <= len(digits) <= 15, "+" + digits
    elif len(digits) == 10:
        ok, out = True, "+1" + digits
    elif len(digits) == 11 and digits.startswith("1"):
        ok, out = True, "+" + digits
    else:
        ok, out = False, ""
    if not ok or re.search(r"[A-Za-z]", raw):
        raise UserError("Enter a valid phone number, for example (530) 555-0123.", field="phone")
    return out


def pretty(phone: str | None) -> str:
    if not phone:
        return ""
    if phone.startswith("+1") and len(phone) == 12:
        return f"({phone[2:5]}) {phone[5:8]}-{phone[8:]}"
    return phone


def has_contact(student: Student) -> bool:
    """A contact the student agreed to show."""
    return student.shown_contact is not None


def display(c: tuple[str, str] | None) -> dict | None:
    """Public-safe, ready to render: {'kind': 'phone'|'email', 'text': '(530) 555-0123'|'a@x.edu', 'href': ...}."""
    if not c:
        return None
    kind, value = c
    return {"kind": kind, "text": pretty(value) if kind == "phone" else value}


def update_contact(db: Session, student: Student, phone_raw: str | None, show: str,
                   allow_requests: bool | None = None) -> None:
    """Phone is optional. `show` is the student's choice of what to display to other students and on the TV:
    'phone', 'email' or 'none'. Choosing phone without a saved number shows their email instead."""
    if show not in ("phone", "email", "none"):
        show = "none"
    phone = normalize_phone(phone_raw or "")
    before = {"has_phone": bool(student.phone), "show": student.contact_display, "allow_requests": student.allow_requests}
    student.phone = phone
    student.contact_display = show
    if show == "none":
        student.phone_ack_at = None  # consent withdrawn (or never given): nothing is displayed
    elif not student.phone_ack_at:
        student.phone_ack_at = clock.now()
    if allow_requests is not None:
        student.allow_requests = allow_requests
    after = {"has_phone": bool(student.phone), "show": student.contact_display, "allow_requests": student.allow_requests}
    if before != after:  # never write the number or email itself to the audit log
        audit.record(db, f"student:{student.email}", "student.contact_update", "student", student.id, before, after)


def apply_from_form(db: Session, student: Student, form) -> None:
    """Used by forms that include the phone fields (reserve, submit, check-in, claim)."""
    if "phone" in form:
        update_contact(db, student, form.get("phone"), form.get("show_contact", "none"))
