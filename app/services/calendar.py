"""Calendar grids for the public schedule pages: days across, time slots down. Built from the same rules as booking."""
from datetime import date, datetime, time as dtime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.models import Machine, MachineHold, MaintenanceRecord, Reservation
from app.services import shop
from app.services import settings as shop_settings


def machine_calendar(db: Session, m: Machine, start: date, days: int, now: datetime, viewer_id: int | None = None) -> dict:
    """One machine, `days` days from `start`. Each cell: state (free/booked/mine/inuse/hold/down/past/closed) and who."""
    cfg = shop_settings.load(db)
    today = clock.to_local(now).date()
    day_list = [start + timedelta(days=i) for i in range(days)]
    w0, w1 = clock.local_dt(start, dtime(0)), clock.local_dt(start + timedelta(days=days), dtime(0))
    res = db.scalars(select(Reservation).where(Reservation.machine_id == m.id, Reservation.start_at < w1, Reservation.end_at > w0,
                                               Reservation.status.in_(("confirmed", "checked_in", "completed")))).all()
    holds_ = db.scalars(select(MachineHold).where(MachineHold.machine_id == m.id, MachineHold.released_at.is_(None),
                                                  MachineHold.expires_at > now)).all()
    windows = db.scalars(select(MaintenanceRecord).where(MaintenanceRecord.machine_id == m.id, MaintenanceRecord.closed_at.is_(None),
                                                         MaintenanceRecord.window_start.is_not(None),
                                                         MaintenanceRecord.window_start < w1, MaintenanceRecord.window_end > w0)).all()
    grids = {d: shop.slot_grid(db, d, cfg.slot_minutes, m.location_id) for d in day_list}
    times = sorted({clock.to_local(s).time() for g in grids.values() for s, _ in g})
    by_day = {d: {clock.to_local(s).time(): (s, e) for s, e in g} for d, g in grids.items()}
    down_now = cfg.emergency_active or m.maintenance_state == "under_maintenance" or m.override_status in (
        "under_maintenance", "awaiting_inspection")
    rows = []
    for t in times:
        cells = []
        for d in day_list:
            se = by_day[d].get(t)
            if se is None:
                cells.append({"state": "closed", "label": ""})
                continue
            s, e = se
            hit = next((r for r in res if r.start_at < e and r.end_at > s), None)
            if e <= now:
                st, who = "past", (hit.student.public_name if hit else "")
            elif (d == today and down_now) or any(x.window_start < e and x.window_end > s for x in windows):
                st, who = "down", ""
            elif any(h.expires_at > s for h in holds_):
                st, who = "hold", ""
            elif hit is None:
                st, who = "free", ""
            elif viewer_id and hit.student_id == viewer_id:
                st, who = "mine", "You"
            else:
                st, who = ("inuse" if hit.status == "checked_in" else "booked"), hit.student.public_name
            cells.append({"state": st, "label": who, "start": s})
        rows.append({"time": datetime.combine(date(2000, 1, 1), t).strftime("%I:%M %p").lstrip("0"), "cells": cells})
    return {"days": [{"iso": d.isoformat(), "dow": d.strftime("%a"), "dom": d.strftime("%b %d").replace(" 0", " "),
                      "today": d == today, "closed": not grids[d]} for d in day_list], "rows": rows}
