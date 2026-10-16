"""Machine schedules for signed-in students: see who is booked when, and reach them politely."""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock, security
from app.db import get_db
from app.models import Machine, Reservation, ShopSession, Student
from app.services import access, audit, contact, holds, projects, shop, status
from app.services import settings as shop_settings
from app.web import public_post_guard, render, student_required

router = APIRouter(dependencies=[Depends(public_post_guard)])


@router.get("/schedule")
def schedule_index(request: Request, student: Student = Depends(student_required), db: Session = Depends(get_db)):
    rows = status.board(db)
    return render(request, "schedule_index.html", {"rows": rows})


@router.get("/machine/{mid}")
def machine_schedule(mid: int, request: Request, date: str = "", student: Student = Depends(student_required),
                     db: Session = Depends(get_db)):
    m = db.get(Machine, mid)
    if m is None or not m.active:
        raise HTTPException(404, "That machine wasn't found.")
    security.rate_check(db, f"sched:{student.id}", 200, 3600, "You're looking things up very quickly. Please wait a bit.")
    cfg = shop_settings.load(db)
    today = clock.to_local(clock.now()).date()
    try:
        day = datetime.strptime(date, "%Y-%m-%d").date() if date else today
    except ValueError:
        day = today
    day = max(today, min(day, today + timedelta(days=cfg.max_advance_days)))
    start = clock.local_dt(day, datetime.min.time())
    end = start + timedelta(days=1)
    rows = db.scalars(select(Reservation).where(
        Reservation.machine_id == mid, Reservation.start_at < end, Reservation.end_at > start,
        Reservation.status.in_(("confirmed", "checked_in", "completed"))).order_by(Reservation.start_at)).all()
    now = clock.now()
    items, shown = [], 0
    for r in rows:
        mine = r.student_id == student.id
        can_contact = (not mine and bool(r.student.phone) and r.status in ("confirmed", "checked_in"))
        can_ask = (not mine and r.status == "confirmed" and r.start_at > now and r.student.allow_requests)
        if can_contact:
            shown += 1
        items.append({"r": r, "mine": mine, "who": r.student.public_name, "contact": can_contact, "ask": can_ask,
                      "live": r.status == "checked_in", "phone": contact.pretty(r.student.phone),
                      "no_number": not mine and not r.student.phone and r.status in ("confirmed", "checked_in")})
    hold = holds.active_hold(db, mid, now)
    if hold and hold.student.phone and hold.student_id != student.id:
        shown += 1
    if shown:
        audit.record(db, f"student:{student.email}", "contact.view", "machine", mid, after={"numbers_shown": shown})
    grid = shop.slot_grid(db, day, cfg.slot_minutes, m.location_id)
    open_slots = [(s, e) for s, e in grid if e > now and not any(x["r"].start_at < e and x["r"].end_at > s for x in items
                                                              if x["r"].status != "completed")]
    state = status.machine_state(db, m)
    sess = db.scalar(select(ShopSession).where(ShopSession.machine_id == mid, ShopSession.status == "active"))
    return render(request, "machine_schedule.html", {
        "m": m, "day": day, "prev": day - timedelta(days=1) if day > today else None,
        "next": day + timedelta(days=1) if day < today + timedelta(days=cfg.max_advance_days) else None,
        "items": items, "open_slots": open_slots, "state": state, "on_now": sess, "today": today,
        "hold": hold, "hold_phone": contact.pretty(hold.student.phone) if hold else "", "student": student, "bookable": access.machine_allows(access.effective_level(db, student), m)})
