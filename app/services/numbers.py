"""Human-friendly identifiers."""
import secrets

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.models import Approval, Reservation

# No 0/O/1/I/L: typeable from a phone screen or a printout.
ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"


def new_reservation_ref(db: Session) -> str:
    for _ in range(20):
        ref = "RSV-" + "".join(secrets.choice(ALPHABET) for _ in range(6))
        if db.scalar(select(Reservation.id).where(Reservation.ref == ref)) is None:
            return ref
    raise RuntimeError("could not allocate a reservation reference")


def next_approval_number(db: Session) -> str:
    year = clock.to_local(clock.now()).year
    prefix = f"APR-{year}-"
    last = db.scalar(select(Approval.number).where(Approval.number.like(prefix + "%"))
                     .order_by(Approval.number.desc()).limit(1))
    seq = int(last.rsplit("-", 1)[1]) + 1 if last else 1
    return f"{prefix}{seq:04d}"


def normalize_ref(raw: str, prefix: str) -> str:
    """'rsv 7k3q9f' / 'RSV7K3Q9F' / '7k3q9f' -> 'RSV-7K3Q9F'."""
    s = "".join(ch for ch in (raw or "").upper() if ch.isalnum())
    if s.startswith(prefix):
        s = s[len(prefix):]
    return f"{prefix}-{s}"


def normalize_approval(raw: str) -> str:
    """'apr-2026-42' / 'APR20260042' -> 'APR-2026-0042'."""
    s = "".join(ch for ch in (raw or "").upper() if ch.isalnum())
    if s.startswith("APR"):
        s = s[3:]
    if len(s) >= 5 and s[:4].isdigit() and s[4:].isdigit():
        return f"APR-{s[:4]}-{int(s[4:]):04d}"
    return (raw or "").strip().upper()
