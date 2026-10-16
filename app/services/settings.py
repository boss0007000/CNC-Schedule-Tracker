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
    "max_session_hours_standard": 6,  # longest single session for students on the approved list
    "max_session_hours_advanced": 12,  # ... for students with extended access
    "require_access_list": True,  # students must be on a staff-managed list to book
    "max_reservations_per_week": 0,  # 0 = no limit (project approval already gates use)
    "max_hours_per_week": 0,  # 0 = no limit
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
    "setup_hold_max_business_days": 3,  # a setup may stay in a machine this many open days (the day you leave counts as day 1)
    "tools_hold_max_business_days": 1,  # only-my-tools-in-it
    "closing_prompt_minutes": 30,  # before closing, ask people still on a machine if anything is staying
    "tv_show_phone": True,  # numbers of the current user / next booking shown on the shop TV
    "early_checkin_max_minutes": 120,  # check in early when the machine is idle and nobody else is booked first
    "change_request_hours": 48,  # how long a time-change request stays open
    "max_open_change_requests": 3,
    "change_request_cooldown_hours": 24,  # before asking the same student again after a decline
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
    ("require_access_list", "bool", "Students must be on an access list to book"),
    ("max_session_hours_standard", "float", "Longest single session: approved list (hours)"),
    ("max_session_hours_advanced", "float", "Longest single session: extended access (hours)"),
    ("max_reservations_per_week", "int", "Max reservations per student per week (0 = no limit)"),
    ("max_hours_per_week", "float", "Max hours per student per week (0 = no limit)"),
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
    ("setup_hold_max_business_days", "int", "Setup hold: max open days a setup may stay in a machine"),
    ("tools_hold_max_business_days", "int", "Tools-only hold: max open days"),
    ("closing_prompt_minutes", "int", "Ask people still on a machine about leftovers this long before closing (min)"),
    ("tv_show_phone", "bool", "Show phone numbers on the shop TV (current user + next booking)"),
    ("early_checkin_max_minutes", "int", "Earliest an idle machine can be started early (minutes)"),
    ("change_request_hours", "int", "Time-change request stays open (hours)"),
    ("max_open_change_requests", "int", "Max open time-change requests per student"),
    ("change_request_cooldown_hours", "int", "Wait before asking the same student again after a no (hours)"),
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
