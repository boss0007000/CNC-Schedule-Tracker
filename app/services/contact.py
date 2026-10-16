"""Phone number: required to reserve, and shown to other students on the schedule page and on the shop TV."""
import re

from sqlalchemy.orm import Session

from app import clock
from app.errors import UserError
from app.models import Student
from app.services import audit

ACK = ("I understand my phone number is shown on the shop schedule page and TV while I have a booking, "
       "so other students can reach me about the machine.")


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
    return bool(student.phone and student.phone_ack_at)


def update_contact(db: Session, student: Student, phone_raw: str | None, ack: bool,
                   allow_requests: bool | None = None) -> None:
    phone = normalize_phone(phone_raw or "")
    before = {"has_phone": bool(student.phone), "acked": bool(student.phone_ack_at), "allow_requests": student.allow_requests}
    if phone is None:
        student.phone, student.phone_ack_at = None, None
    else:
        already = student.phone == phone and student.phone_ack_at is not None
        if not ack and not already:
            raise UserError("Tick the box to confirm your number may be shown to other students and on the shop TV.",
                            field="ack_phone")
        if not already:
            student.phone_ack_at = clock.now()
        student.phone = phone
    if allow_requests is not None:
        student.allow_requests = allow_requests
    after = {"has_phone": bool(student.phone), "acked": bool(student.phone_ack_at), "allow_requests": student.allow_requests}
    if before != after:  # never write the number itself to the audit log
        audit.record(db, f"student:{student.email}", "student.contact_update", "student", student.id, before, after)


def apply_from_form(db: Session, student: Student, form) -> None:
    """Used by forms that include the phone fields (reserve, submit, check-in, claim)."""
    if "phone" in form:
        update_contact(db, student, form.get("phone"), form.get("ack_phone") == "on")
