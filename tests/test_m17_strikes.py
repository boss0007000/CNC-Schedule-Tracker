"""Strikes (justified, expiring, voidable; 3 = a time-limited suspension) and the careful no-show check."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import clock, emailer
from app.errors import UserError
from app.models import AuditLog, Reservation, ShopSession, Strike
from app.services import emergency, lifecycle, strikes
from app.services import machines as machines_svc
from app.services import settings as shop_settings
from tests.factories import at, book, machine
from tests.test_m6_sessions import MM1, IP, booked, checkin, code, go, w  # noqa: F401  (fixture)

WHY = "Ran the spindle with the door open after being told twice."


# ---------------------------------------------------------------- strikes
def test_a_strike_needs_a_real_justification(db, w):
    with pytest.raises(UserError):
        strikes.give(db, w.alex, "staff:sam", "bad")
    s, sus = strikes.give(db, w.alex, "staff:sam", WHY)
    assert s.reason == WHY and sus is None and w.alex.suspended_until is None


def test_three_strikes_suspend_for_a_limited_time_and_never_permanently(db, w):
    shop_settings.set_value(db, "suspension_days", 365)  # even a silly setting is capped
    for _ in range(2):
        strikes.give(db, w.alex, "staff:sam", WHY)
        assert w.alex.suspended_until is None
    emailer.SENT.clear()
    _, sus = strikes.give(db, w.alex, "staff:sam", WHY)
    assert sus and w.alex.suspended_until == sus
    assert sus - clock.now() <= timedelta(days=30)
    emailer.flush_outbox(db)
    assert any("paused until" in m["text"] and WHY in m["text"] for m in emailer.SENT)


def test_strikes_expire_and_voiding_lifts_the_suspension(db, w):
    s1, _ = strikes.give(db, w.alex, "staff:sam", WHY)
    strikes.give(db, w.alex, "staff:sam", WHY)
    clock.advance(days=31)  # the first two have expired
    assert len(strikes.active(db, w.alex)) == 0
    for _ in range(3):
        last, _ = strikes.give(db, w.alex, "staff:sam", WHY)
    assert w.alex.suspended_until
    with pytest.raises(UserError):
        strikes.void(db, last, "staff:sam", "oops")  # needs a reason too
    strikes.void(db, last, "staff:sam", "Reviewed the camera; the guard was closed.")
    assert w.alex.suspended_until is None and last.voided_at


def test_no_direct_suspension_route_exists(db, w):
    import app.services.students as ss
    assert not hasattr(ss, "suspend")


# ---------------------------------------------------------------- the 15-minute check
def test_a_checkin_that_landed_but_was_not_reflected_is_never_a_no_show(db, w):
    r = booked(db, w)
    checkin(db, w, r, when=65)
    r.status = "confirmed"  # simulate a delayed status update
    db.flush()
    go(76)
    assert lifecycle.mark_no_show(db, r, clock.now()) == "attended"
    assert r.status == "checked_in" and not db.scalars(select(Strike)).all()


def test_machine_breakdown_cancels_without_a_strike(db, w):
    r = booked(db, w)
    machines_svc.open_maintenance(db, "staff:max", machine(db, MM1), "issue", "spindle failed", "under_maintenance")
    go(76)
    emailer.SENT.clear()
    out = lifecycle.sweep(db)
    assert out["no_shows"] == 0 and out.get("excused") == 1
    assert r.status == "cancelled" and "excused" in r.cancel_reason
    assert not db.scalars(select(Strike)).all()
    emailer.flush_outbox(db)
    assert any("no penalty" in m["subject"] for m in emailer.SENT)


def test_breakdown_fixed_before_the_check_still_excuses_the_slot(db, w):
    r = booked(db, w)
    go(65)
    machines_svc.open_maintenance(db, "staff:max", machine(db, MM1), "issue", "coolant leak", "under_maintenance")
    go(70)
    machines_svc.clear_machine(db, "staff:max", machine(db, MM1), "fixed")
    go(76)
    assert lifecycle.mark_no_show(db, r, clock.now()) == "excused"


def test_emergency_closure_is_not_a_no_show_even_after_reopening(db, w):
    r = booked(db, w)
    go(65)
    emergency.close(db, "staff:fran", "fire alarm")
    go(68)
    emergency.reopen(db, "staff:fran")
    go(76)
    assert lifecycle.mark_no_show(db, r, clock.now()) == "excused"
    assert w.alex.suspended_until is None and not db.scalars(select(Strike)).all()


def test_cancelled_reservations_never_count(db, w):
    r = booked(db, w)
    r.status = "cancelled"
    go(100)
    assert lifecycle.sweep(db)["no_shows"] == 0 and not db.scalars(select(Strike)).all()


def test_real_no_show_still_records_a_justified_strike(db, w):
    r = booked(db, w)
    go(76)
    assert lifecycle.sweep(db)["no_shows"] == 1 and r.status == "no_show"
    st = db.scalars(select(Strike)).one()
    assert st.kind == "no_show" and r.ref in st.reason and "Did not check in" in st.reason
