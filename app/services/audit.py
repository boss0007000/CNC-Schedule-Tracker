from sqlalchemy.orm import Session

from app.models import AuditLog


def _plain(v):
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, dict):
        return {str(k): _plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_plain(x) for x in v]
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return str(v)


def record(db: Session, actor: str, action: str, entity: str | None = None, entity_id=None,
           before=None, after=None, ip: str | None = None) -> AuditLog:
    row = AuditLog(actor=actor, action=action, entity=entity,
                   entity_id=None if entity_id is None else str(entity_id),
                   before=_plain(before), after=_plain(after), ip=ip)
    db.add(row)
    db.flush()
    return row


def snapshot(obj, fields: list[str]) -> dict:
    return {f: _plain(getattr(obj, f, None)) for f in fields}
