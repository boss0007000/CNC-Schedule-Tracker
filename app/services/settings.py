"""Runtime shop settings stored in the DB, with typed defaults."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_config
from app.models import Setting

WAIVER_TEXT = (
    "I understand that machine shop equipment can cause serious injury. I will follow all posted "
    "safety rules, wear required protective equipment, only use machines I am certified on, and "
    "follow staff instructions. I accept responsibility for damage caused by misuse."
)

DEFAULTS: dict[str, object] = {
    "shop_name": "Student Machine Shop",
    "allowed_email_domain": None,  # None -> falls back to ALLOWED_EMAIL_DOMAIN env
    "collect_student_id": False,
    "slot_minutes": 60,
    "max_reservation_slots": 4,
    "max_reservations_per_week": 3,
    "max_hours_per_week": 8,
    "max_advance_days": 14,
    "max_active_per_group": 1,
    "cancel_cutoff_minutes": 120,
    "checkin_early_minutes": 15,
    "checkin_grace_minutes": 15,
    "autoclose_buffer_minutes": 30,
    "noshow_threshold": 3,
    "noshow_window_days": 30,
    "suspension_days": 14,
    "rotating_code_interval_min": 10,
    "rotating_code_overlap_min": 2,
    "waitlist_claim_minutes": 30,
    "reserved_lead_minutes": 30,
    "retention_days": 730,
    "approval_default_days": 120,
    "waiver_version": "1",
    "waiver_text": WAIVER_TEXT,
    "emergency_active": False,
    "emergency_message": "The shop is closed. Please stop all machines and follow staff instructions.",
}

SETTING_META: list[tuple[str, str, str]] = [
    # key, type, label
    ("shop_name", "str", "Shop name"),
    ("allowed_email_domain", "str", "Allowed student email domain"),
    ("collect_student_id", "bool", "Collect student ID (privacy: off by default)"),
    ("slot_minutes", "int", "Slot length (minutes)"),
    ("max_reservation_slots", "int", "Max consecutive slots per reservation"),
    ("max_reservations_per_week", "int", "Max reservations per student per week"),
    ("max_hours_per_week", "float", "Max hours per student per week"),
    ("max_advance_days", "int", "Max advance booking window (days)"),
    ("max_active_per_group", "int", "Max active reservations per machine group"),
    ("cancel_cutoff_minutes", "int", "Cancellation cutoff (minutes before start)"),
    ("checkin_early_minutes", "int", "Check-in allowed this many minutes early"),
    ("checkin_grace_minutes", "int", "Late grace before No-Show (minutes)"),
    ("autoclose_buffer_minutes", "int", "Auto-close buffer after reservation end (minutes)"),
    ("noshow_threshold", "int", "No-shows that trigger a suspension"),
    ("noshow_window_days", "int", "No-show counting window (days)"),
    ("suspension_days", "int", "Automatic suspension length (days)"),
    ("rotating_code_interval_min", "int", "Shop code rotation interval (minutes)"),
    ("rotating_code_overlap_min", "int", "Previous shop code stays valid (minutes)"),
    ("waitlist_claim_minutes", "int", "Waitlist claim window (minutes)"),
    ("reserved_lead_minutes", "int", "Show 'Reserved' this many minutes before a booking"),
    ("retention_days", "int", "Data retention (days)"),
    ("approval_default_days", "int", "Default approval validity (days)"),
    ("waiver_version", "str", "Safety waiver version (change to force re-signing)"),
    ("waiver_text", "text", "Safety waiver text"),
]


class SettingsView:
    def __init__(self, values: dict[str, object]):
        self._v = values

    def __getattr__(self, name: str):
        try:
            return self._v[name]
        except KeyError:
            raise AttributeError(name) from None

    def get(self, name: str, default=None):
        return self._v.get(name, default)

    @property
    def email_domain(self) -> str:
        d = self._v.get("allowed_email_domain") or get_config().allowed_email_domain
        return str(d).lower().lstrip("@")


def load(db: Session) -> SettingsView:
    values = dict(DEFAULTS)
    for row in db.scalars(select(Setting)):
        values[row.key] = row.value
    return SettingsView(values)


def set_value(db: Session, key: str, value: object) -> None:
    if key not in DEFAULTS:
        raise KeyError(key)
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=value))
    else:
        row.value = value
    db.flush()


def coerce(kind: str, raw: str):
    raw = raw.strip()
    if kind == "bool":
        return raw.lower() in ("1", "true", "on", "yes")
    if kind == "int":
        return int(raw)
    if kind == "float":
        return float(raw)
    return raw
