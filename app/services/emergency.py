"""Emergency close: every machine reports Under Maintenance, the TV shows a red banner, active users are emailed."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import emailer
from app.errors import UserError
from app.models import ShopSession
from app.services import audit, notify
from app.services import settings as shop_settings


def close(db: Session, actor: str, message: str | None = None) -> int:
    cfg = shop_settings.load(db)
    if cfg.emergency_active:
        raise UserError("An emergency close is already active.")
    msg = (message or "").strip() or shop_settings.DEFAULTS["emergency_message"]
    shop_settings.set_value(db, "emergency_message", msg)
    shop_settings.set_value(db, "emergency_active", True)
    emails = {s.student.email for s in db.scalars(select(ShopSession).where(ShopSession.status == "active"))}
    for to in sorted(emails):
        notify.emergency(db, to, msg)
    audit.record(db, actor, "emergency.close", "shop", 1, {"active": False}, {"active": True, "message": msg,
                                                                              "emailed": len(emails)})
    return len(emails)


def reopen(db: Session, actor: str) -> None:
    if not shop_settings.load(db).emergency_active:
        raise UserError("There is no active emergency close.")
    shop_settings.set_value(db, "emergency_active", False)
    audit.record(db, actor, "emergency.reopen", "shop", 1, {"active": True}, {"active": False})
