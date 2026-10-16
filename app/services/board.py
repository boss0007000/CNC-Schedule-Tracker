"""Public-safe live data for the TV and the admin board (one source: status.derive)."""
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.models import Reservation, ShopSession
from app.services import contact, qr, rotcode, shop, status
from app.services import settings as shop_settings


def _t(dt: datetime | None) -> str | None:
    return None if dt is None else clock.to_local(dt).strftime("%I:%M %p").lstrip("0")


def public_name(full_name: str) -> str:
    parts = full_name.split()
    return parts[0] if len(parts) < 2 else f"{parts[0]} {parts[-1][0]}."


def _timeline(db: Session, rows, now: datetime) -> tuple[dict, dict]:
    """Today's per-machine timeline (public-safe: no names). Returns (window, {machine_id: [segments]})."""
    from datetime import time as dtime
    today = clock.to_local(now).date()
    w = shop.hours_for_date(db, today) or (clock.local_dt(today, dtime(8)), clock.local_dt(today, dtime(22)))
    w0, w1 = w
    grace = timedelta(minutes=shop_settings.load(db).checkin_grace_minutes)
    ids = [m.id for m, _ in rows]
    segs: dict[int, list] = {i: [] for i in ids}
    ep = lambda d: int(d.timestamp())  # noqa: E731
    for r in db.scalars(select(Reservation).where(
            Reservation.machine_id.in_(ids), Reservation.status.in_(("confirmed", "checked_in", "completed")),
            Reservation.start_at < w1, Reservation.end_at > w0)):
        if r.status == "confirmed" and now > r.grace_anchor + grace:
            continue  # lapsed (no-show pending): not holding the machine
        kind = {"confirmed": "booked", "checked_in": "inuse", "completed": "done"}[r.status]
        segs[r.machine_id].append({"s": ep(max(r.start_at, w0)), "e": ep(min(r.end_at, w1)), "k": kind})
    for sess in db.scalars(select(ShopSession).where(ShopSession.machine_id.in_(ids), ShopSession.status == "active")):
        if now > sess.expected_end_at:
            segs[sess.machine_id].append({"s": ep(max(sess.expected_end_at, w0)), "e": ep(min(now, w1)), "k": "overdue"})
    for m, st in rows:
        if st.status in ("under_maintenance", "awaiting_inspection"):
            segs[m.id] = [{"s": ep(w0), "e": ep(w1), "k": "down"}]
        elif st.status == "on_hold":
            segs[m.id] = [{"s": ep(w0), "e": ep(w1), "k": "hold"}]
        elif st.open_ended and now < w1:  # keep going: nobody is booked next
            segs[m.id].append({"s": ep(max(now, w0)), "e": ep(min(st.estimated_available_at or w1, w1)), "k": "inuse"})
    span = (w1 - w0).total_seconds() / 3600
    step = 1 if span <= 6 else 2
    ticks, t = [], w0
    while t <= w1:
        ticks.append({"t": ep(t), "label": clock.to_local(t).strftime("%I %p").lstrip("0")})
        t += timedelta(hours=step)
    return {"start": ep(w0), "end": ep(w1), "now": ep(now), "ticks": ticks}, segs


def tv_payload(db: Session, now: datetime | None = None) -> dict:
    """No emails, student IDs or last names: first name + last initial only."""
    now = now or clock.now()
    cfg = shop_settings.load(db)
    code = rotcode.current(db, now)
    sup = shop.supervisor_on_duty(db, now)
    w = shop.hours_for_date(db, clock.to_local(now).date())
    is_open = shop.is_open(db, now) and not cfg.emergency_active
    machines = []
    rows = status.board(db, now)
    window, segs = _timeline(db, rows, now)
    opens_label = None
    if not is_open:
        d0 = clock.to_local(now).date()
        for i in range(0, 9):
            w2 = shop.hours_for_date(db, d0 + timedelta(days=i))
            if w2 and w2[0] > now:
                opens_label = clock.to_local(w2[0]).strftime("%a %I:%M %p").replace(" 0", " ")
                break
    for m, st in rows:
        free_label = None
        if st.status == "available" and not is_open:
            free_label = f"Opens {opens_label}" if opens_label else "Closed"
        elif st.status == "available":
            nxt = st.next_reservation_start
            free_label = f"Free until {_t(nxt)}" if nxt else "Free for the rest of the day"
        machines.append({"timeline": sorted(segs[m.id], key=lambda x: x["s"]), "free_label": free_label,
                         "since": _t(st.since), "open_ended": st.open_ended,
                         "user_phone": contact.pretty(st.current_phone) if (cfg.tv_show_phone and st.current_user) else None,
                         "next_user": st.next_user,
                         "next_phone": contact.pretty(st.next_phone) if (cfg.tv_show_phone and st.next_user) else None,
                         "hold_kind": st.hold_kind,
                         "hold_until": clock.to_local(st.hold_until).strftime("%a %I:%M %p").replace(" 0", " ") if st.hold_until else None,
                         "staff_run": m.access_level == "staff_only",
                         "qr": f"/qr/machine/{m.id}.png",
            "id": m.id, "name": m.name, "group": m.group.name if m.group else None, "status": st.status,
            "label": st.label, "user": st.current_user, "remaining_minutes": st.remaining_minutes,
            "overdue_minutes": st.overdue_minutes, "next_reservation": _t(st.next_reservation_start),
            "available_at": _t(st.estimated_available_at) if st.status in ("in_use", "overdue", "reserved") else None,
            "reason": st.reason if st.status in ("under_maintenance", "awaiting_inspection", "on_hold", "reserved") or st.open_ended else None})
    return {
        "server_time": now.isoformat(), "updated_label": _t(now), "shop_open": is_open,
        "hours_label": shop.day_label(db, clock.to_local(now).date()) if w else "Closed today",
        "supervisor": public_name(sup.display_name or sup.username) if sup else None,
        "emergency": {"active": bool(cfg.emergency_active), "message": cfg.emergency_message},
        "code": {"value": code["code"], "seconds_left": code["seconds_left"]},
        "machines": machines, "shop_name": cfg.shop_name, "window": window,
        "qr": [{"label": lbl, "hint": hint, "img": f"/qr/{key}.png", "url": qr.url_for(path)}
               for key, lbl, hint, path in qr.FLOWS],
    }


def admin_overview(db: Session, now: datetime | None = None) -> dict:
    from app.models import PROJECT_SUBMITTED, Project
    from sqlalchemy import func
    now = now or clock.now()
    today = clock.to_local(now).date()
    day_start = clock.local_dt(today, datetime.min.time())
    day_end = clock.local_dt(today + timedelta(days=1), datetime.min.time())
    active = db.scalars(select(ShopSession).where(ShopSession.status == "active").order_by(ShopSession.checked_in_at)).all()
    flagged = db.scalars(select(ShopSession).where(ShopSession.needs_review.is_(True))
                         .order_by(ShopSession.id.desc()).limit(20)).all()
    todays = db.scalars(select(Reservation).where(Reservation.start_at >= day_start, Reservation.start_at < day_end,
                                                  Reservation.status != "cancelled")
                        .order_by(Reservation.start_at)).all()
    pending = db.scalar(select(func.count()).select_from(Project).where(Project.status == PROJECT_SUBMITTED)) or 0
    from app.models import MachineHold
    waiting_checkins = db.scalar(select(func.count()).select_from(ShopSession).where(ShopSession.status == "pending")) or 0
    active_holds = db.scalars(select(MachineHold).where(MachineHold.released_at.is_(None), MachineHold.expires_at > now)
                              .order_by(MachineHold.expires_at)).all()
    expired_holds = db.scalars(select(MachineHold).where(MachineHold.release_reason == "expired",
                                                         MachineHold.released_at > now - timedelta(days=3))
                               .order_by(MachineHold.id.desc())).all()
    overdue = [s for s in active if now > s.expected_end_at]
    return {"board": status.board(db, now), "active": active, "flagged": flagged, "todays": todays,
            "pending": pending, "overdue": overdue,
            "waiting_checkins": waiting_checkins, "holds": active_holds, "expired_holds": expired_holds}
