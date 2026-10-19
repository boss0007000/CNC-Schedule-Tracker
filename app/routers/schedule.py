"""Machine schedules for signed-in students: see who is booked when, and reach them politely."""
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock, security
from app.db import get_db
from app.models import Machine, Reservation, ShopSession, Student
from app.services import access, audit, contact, holds, identity, projects, shop, status
from app.services import settings as shop_settings
from app.web import public_post_guard, render, student_required

router = APIRouter(dependencies=[Depends(public_post_guard)])


def _viewer(request: Request, db: Session):
    """Schedules need no login. If the visitor happens to be signed in we personalise a little (their own bookings)."""
    return identity.current_student(db, request)


def _parse_day(value: str, default, lo, hi):
    try:
        d = datetime.strptime(value, "%Y-%m-%d").date() if value else default
    except ValueError:
        d = default
    return max(lo, min(d, hi))


@router.get("/schedule")
def schedule_index(request: Request, start: str = "", db: Session = Depends(get_db)):
    from app.services import board
    cfg = shop_settings.load(db)
    now = clock.now()
    today = clock.to_local(now).date()
    last = today + timedelta(days=cfg.max_advance_days)
    first = _parse_day(start, today, today, last)
    rows = status.board(db)
    week = board.week_payload(db, rows, now, days=7, start=first)
    return render(request, "schedule_index.html", {
        "rows": rows, "week": week, "student": _viewer(request, db), "first": first,
        "prev": max(today, first - timedelta(days=7)) if first > today else None,
        "next": first + timedelta(days=7) if first + timedelta(days=7) <= last else None,
        "status_of": {m.id: st for m, st in rows}})


@router.get("/machine/{mid}")
def machine_schedule(mid: int, request: Request, date: str = "", start: str = "", db: Session = Depends(get_db)):
    student = _viewer(request, db)
    m = db.get(Machine, mid)
    if m is None or not m.active:
        raise HTTPException(404, "That machine wasn't found.")
    # no login required, so limit lookups per person (or per network address) to blunt scraping of the page
    who_key = f"sched:{student.id}" if student else f"sched:ip:{security.client_ip(request)}"
    security.rate_check(db, who_key, 300, 3600, "You're looking things up very quickly. Please wait a bit.")
    cfg = shop_settings.load(db)
    today = clock.to_local(clock.now()).date()
    last = today + timedelta(days=cfg.max_advance_days)
    first = _parse_day(start, today, today, last)
    day = _parse_day(date, first, today, last)
    from app.services import calendar as cal
    CAL_DAYS = 14
    calendar = cal.machine_calendar(db, m, first, CAL_DAYS, clock.now(), student.id if student else None)
    start = clock.local_dt(day, datetime.min.time())
    end = start + timedelta(days=1)
    rows = db.scalars(select(Reservation).where(
        Reservation.machine_id == mid, Reservation.start_at < end, Reservation.end_at > start,
        Reservation.status.in_(("confirmed", "checked_in", "completed"))).order_by(Reservation.start_at)).all()
    now = clock.now()
    show_phone = bool(cfg.public_schedule_show_phone or student)  # signed-in students always see numbers
    items, shown = [], 0
    for r in rows:
        mine = bool(student and r.student_id == student.id)
        c = r.student.shown_contact if show_phone else None
        can_contact = (not mine and c is not None and r.status in ("confirmed", "checked_in"))
        can_ask = (not mine and r.status == "confirmed" and r.start_at > now and r.student.allow_requests)
        if can_contact:
            shown += 1
        items.append({"r": r, "mine": mine, "who": r.student.public_name, "contact": can_contact, "ask": can_ask,
                      "live": r.status == "checked_in", "contact": contact.display(c) if not mine else None, "c": c,
                      "no_number": not mine and c is None and r.status in ("confirmed", "checked_in")})
    hold = holds.active_hold(db, mid, now)
    if hold and show_phone and hold.student.shown_contact and not (student and hold.student_id == student.id):
        shown += 1
    if shown:
        audit.record(db, f"student:{student.email}" if student else "public", "contact.view", "machine", mid,
                     after={"numbers_shown": shown})
    grid = shop.slot_grid(db, day, cfg.slot_minutes, m.location_id)
    open_slots = [(s, e) for s, e in grid if e > now and not any(x["r"].start_at < e and x["r"].end_at > s for x in items
                                                              if x["r"].status != "completed")]
    state = status.machine_state(db, m)
    sess = db.scalar(select(ShopSession).where(ShopSession.machine_id == mid, ShopSession.status == "active"))
    return render(request, "machine_schedule.html", {
        "m": m, "day": day, "prev": day - timedelta(days=1) if day > today else None,
        "next": day + timedelta(days=1) if day < today + timedelta(days=cfg.max_advance_days) else None,
        "calendar": calendar, "first": first,
        "cal_prev": max(today, first - timedelta(days=CAL_DAYS)) if first > today else None,
        "cal_next": first + timedelta(days=CAL_DAYS) if first + timedelta(days=CAL_DAYS) <= last else None,
        "items": items, "open_slots": open_slots, "state": state, "on_now": sess, "today": today,
        "hold": hold, "hold_contact": (contact.display(hold.student.shown_contact) if (hold and show_phone) else None), "hold_c": (hold.student.shown_contact if (hold and show_phone) else None), "student": student,
        "show_phone": show_phone, "bookable": bool(student) and access.machine_allows(access.effective_level(db, student), m)})
