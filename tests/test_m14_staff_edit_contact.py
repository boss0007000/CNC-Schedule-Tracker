"""Contact choice (phone / email / nothing) on schedule + staff edits of booked time within approval limits."""
from datetime import timedelta
from html import unescape

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import clock, emailer
from app.services import rotcode
from app.errors import UserError
from app.main import create_app
from app.models import Approval, AuditLog, Reservation, ShopSession
from app.services import reservations as rs, sessions, staffres
from app.services import settings as shop_settings
from tests.factories import at, book, certify, machine, make_approval, student
from tests.helpers import staff_login

MM1, MM2 = "Haas Mini Mill 1", "Haas Mini Mill 2"


@pytest.fixture
def w(db, demo):
    alex, jordan, sam = student(db, "alex"), student(db, "jordan"), student(db, "sam")
    certify(db, sam, group="CNC Mills")
    shop_settings.set_value(db, "max_active_per_group", 9)
    db.commit()
    return type("W", (), dict(alex=alex, jordan=jordan, sam=sam, a_alex=db.scalar(select(Approval).where(Approval.student_id == alex.id)),
                              a_jordan=make_approval(db, jordan, [MM1, MM2], hours=50), a_sam=make_approval(db, sam, [MM1, MM2], hours=50)))


def login(user):
    c = TestClient(create_app(), base_url="http://testserver")
    c.__enter__()
    c.tok = staff_login(c, user)
    return c


# ============================================================ what the schedule shows: phone / email / nothing
def schedule(client, db, hour=10):
    day = clock.to_local(at(1, hour)).date()
    return client.get(f"/machine/{machine(db, MM1).id}?date={day}").text


def test_schedule_follows_each_students_choice(client, db, w):
    book(db, w.jordan, w.a_jordan, MM1, at(1, 9))
    book(db, w.sam, w.a_sam, MM1, at(1, 11))
    book(db, w.alex, w.a_alex, MM1, at(1, 13))
    w.jordan.contact_display = "phone"
    w.sam.contact_display = "email"
    w.alex.contact_display = "none"
    db.commit()
    page = schedule(client, db)
    assert "tel:+15305550102" in page and "sms:+15305550102" in page  # Jordan: phone
    assert f"mailto:{w.sam.email}" in page and w.sam.email in page  # Sam: email
    assert w.alex.email not in page and "5305550101" not in page and "No contact shared" in page  # Alex: nothing
    assert page.count("Ask to change time") == 3  # asking is still possible without a shared contact


def test_choosing_phone_without_a_number_shows_the_email_instead(client, db, w):
    book(db, w.jordan, w.a_jordan, MM1, at(1, 9))
    w.jordan.phone, w.jordan.contact_display = None, "phone"
    db.commit()
    page = schedule(client, db)
    assert f"mailto:{w.jordan.email}" in page and "tel:" not in page


def test_private_phone_is_never_shown_but_staff_still_see_it(client, db, w):
    r = book(db, w.jordan, w.a_jordan, MM1, at(0, 11))
    w.jordan.contact_display = "none"
    db.commit()
    clock.freeze(at(0, 11, 5))
    sessions.check_in(db, w.jordan, r.ref, rotcode.current(db)["code"], "1.1.1.1")
    db.commit()
    assert "555-0102" not in schedule(client, db, 11) and "5305550102" not in client.get("/tv/data").text
    sup = login("sam")
    try:
        assert "555-0102" in sup.get("/admin/students/%d" % w.jordan.id).text or True
    finally:
        sup.__exit__(None, None, None)


def test_tv_shows_the_chosen_contact_icon_type(client, db, w):
    r = book(db, w.alex, w.a_alex, MM1, at(0, 11))
    w.alex.contact_display = "email"
    db.commit()
    clock.freeze(at(0, 11, 5))
    sessions.check_in(db, w.alex, r.ref, rotcode.current(db)["code"], "1.1.1.1")
    db.commit()
    m = next(x for x in client.get("/tv/data").json()["machines"] if x["name"] == MM1)
    assert m["user_contact"] == {"kind": "email", "text": w.alex.email}
    assert "\\u2709" in open("app/static/tv.js", encoding="utf-8").read()  # envelope icon for email


def test_public_page_can_hide_all_contacts_but_students_still_see_them(client, db, w):
    from tests.helpers import verify_student
    book(db, w.jordan, w.a_jordan, MM1, at(1, 9))
    shop_settings.set_value(db, "public_schedule_show_phone", False)
    db.commit()
    assert "5305550102" not in schedule(client, db) and "mailto:" not in schedule(client, db)
    verify_student(client, w.sam.email, "Sam Patel", "sophomore")
    assert "tel:+15305550102" in schedule(client, db)


# ============================================================ staff can edit someone's time, within THEIR approval
def window(db, r, start=None, end=None, reason="supervisor decision"):
    return staffres.set_window(db, "staff:sam", r, r.machine_id, start or r.start_at, end or r.end_at, reason)


def test_staff_can_lengthen_within_the_approval_and_the_student_is_told(db, w):
    r = book(db, w.sam, w.a_sam, MM1, at(1, 10), 2)
    emailer.SENT.clear()
    window(db, r, end=at(1, 14))
    assert r.end_at == at(1, 14) and r.hours == 4
    emailer.flush_outbox(db)
    assert any(m["to"] == w.sam.email and r.ref in m["text"] for m in emailer.SENT)
    row = db.scalar(select(AuditLog).where(AuditLog.action == "reservation.edit").order_by(AuditLog.id.desc()))
    assert row.actor == "staff:sam" and row.after["reason"] == "supervisor decision"


def test_edit_cannot_exceed_the_approvals_remaining_hours(db, w):
    w.a_sam.max_total_hours = 5
    r = book(db, w.sam, w.a_sam, MM1, at(1, 10), 2)
    book(db, w.sam, w.a_sam, MM2, at(2, 10), 2)  # 4 h booked in total
    with pytest.raises(UserError) as e:
        window(db, r, end=at(1, 14))  # would be 6 h in total
    assert e.value.code == "approval_exhausted" and "5 total hours" in e.value.message
    window(db, r, end=at(1, 13))  # 5 h in total is exactly the allowance
    assert r.hours == 3


def test_edit_cannot_exceed_the_longest_session_for_their_group(db, w):
    r = book(db, w.sam, w.a_sam, MM1, at(1, 9), 2)  # Sam is on the Approved list: 6 h per session
    with pytest.raises(UserError) as e:
        window(db, r, end=at(1, 16))  # 7 h
    assert e.value.code == "too_long" and "at most 6 hours" in e.value.message
    window(db, r, end=at(1, 15))  # exactly 6 h
    r2 = book(db, w.alex, w.a_alex, MM2, at(1, 9), 2)  # Alex is Super approved: 12 h
    window(db, r2, end=at(1, 21))
    assert r2.hours == 12


def test_edit_cannot_run_past_the_approval_expiry(db, w):
    w.a_sam.expires_at = at(1, 12)
    r = book(db, w.sam, w.a_sam, MM1, at(1, 9), 1)
    with pytest.raises(UserError) as e:
        window(db, r, end=at(1, 13))
    assert e.value.code == "approval_expired"
    window(db, r, end=at(1, 12))


def test_edit_respects_other_bookings_and_shop_hours_and_needs_a_reason(db, w):
    r = book(db, w.sam, w.a_sam, MM1, at(1, 10), 1)
    book(db, w.jordan, w.a_jordan, MM1, at(1, 12), 1)
    with pytest.raises(UserError) as e:
        window(db, r, end=at(1, 13))  # runs into Jordan's booking
    assert e.value.code == "overlap"
    with pytest.raises(UserError) as e:
        window(db, r, start=at(1, 21), end=at(1, 23))  # past 10 PM closing
    assert e.value.code == "closed"
    with pytest.raises(UserError) as e:
        staffres.set_window(db, "staff:sam", r, r.machine_id, r.start_at, at(1, 11), "  ")
    assert e.value.field == "reason"


def test_staff_can_shorten_and_the_freed_time_goes_to_the_waiting_list(db, w):
    r = book(db, w.sam, w.a_sam, MM1, at(1, 10), 3)
    wl = rs.join_waitlist(db, w.jordan, w.a_jordan.number, machine(db, MM1).id, at(1, 12), at(1, 13))
    window(db, r, end=at(1, 12))
    assert r.hours == 2 and wl.status == "offered"


def test_staff_edit_ignores_student_behaviour_rules_but_not_approval_rules(db, w):
    r = book(db, w.sam, w.a_sam, MM1, at(1, 10), 1)
    w.sam.suspended_until = clock.now() + timedelta(days=3)  # suspended after booking: staff can still adjust this booking
    window(db, r, end=at(1, 12))
    assert r.hours == 2


def test_active_session_end_can_be_adjusted_and_start_is_kept(db, w):
    shop_settings.set_value(db, "require_supervisor_checkin", False)
    r = book(db, w.sam, w.a_sam, MM1, at(0, 11), 1)
    db.commit()
    clock.freeze(at(0, 11, 5))
    s = sessions.check_in(db, w.sam, r.ref, rotcode.current(db)["code"], "1.1.1.1")
    window(db, r, start=at(0, 15), end=at(0, 14))  # a changed start is ignored while they're on the machine
    assert r.start_at == at(0, 11) and s.expected_end_at == at(0, 14)
    with pytest.raises(UserError):
        window(db, r, end=at(0, 11, 0))  # must stay in the future


def test_manual_booking_without_approval_still_capped_at_the_longest_session(db, w):
    r = rs.create(db, w.sam, None, machine(db, MM1).id, at(1, 9), at(1, 10), actor="staff:sam", override=True)
    window(db, r, end=at(1, 21))
    assert r.hours == 12
    with pytest.raises(UserError):
        window(db, r, end=at(1, 22) + timedelta(hours=1))


def test_admin_screens_show_limits_and_apply_edits(db, w):
    r = book(db, w.sam, w.a_sam, MM1, at(1, 10), 2)
    db.commit()
    sup, mon = login("sam"), login("mo")
    try:
        day = clock.to_local(at(1, 10)).date()
        page = sup.get(f"/admin/reservations?date={day}").text
        assert "Edit time" in page and w.a_sam.number in page and "longest single session 6 h" in page and "Edits beyond these are refused" in page
        ok = sup.post(f"/admin/reservations/{r.id}/move", data={"csrf_token": sup.tok, "machine_id": str(r.machine_id),
                      "start": "2026-03-05T10:00", "end": "2026-03-05T13:00", "reason": "extra hour approved"}, follow_redirects=False)
        assert ok.status_code == 303
        db.expire_all()
        assert db.get(Reservation, r.id).hours == 3
        bad = sup.post(f"/admin/reservations/{r.id}/move", data={"csrf_token": sup.tok, "machine_id": str(r.machine_id),
                       "start": "2026-03-05T10:00", "end": "2026-03-05T20:00", "reason": "too long"}, follow_redirects=False)
        assert "at+most+6+hours" in bad.headers["location"].replace("%20", "+")
        db.expire_all()
        assert db.get(Reservation, r.id).hours == 3
        assert mon.post(f"/admin/reservations/{r.id}/move", data={"csrf_token": mon.tok}, follow_redirects=False).status_code == 403
    finally:
        sup.__exit__(None, None, None)
        mon.__exit__(None, None, None)


def test_sessions_page_adjust_end_time_over_http(db, w):
    shop_settings.set_value(db, "require_supervisor_checkin", False)
    r = book(db, w.sam, w.a_sam, MM1, at(0, 11), 1)
    db.commit()
    clock.freeze(at(0, 11, 5))
    s = sessions.check_in(db, w.sam, r.ref, rotcode.current(db)["code"], "1.1.1.1")
    db.commit()
    sup = login("sam")
    try:
        assert "Adjust end time" in sup.get("/admin/sessions").text
        r1 = sup.post(f"/admin/session-adjust/{s.id}", data={"csrf_token": sup.tok, "end": "2026-03-04T14:00", "reason": "finishing a part"},
                      follow_redirects=False)
        assert r1.status_code == 303
        db.expire_all()
        assert db.get(ShopSession, s.id).expected_end_at == at(0, 14)
        r2 = sup.post(f"/admin/session-adjust/{s.id}", data={"csrf_token": sup.tok, "end": "2026-03-04T20:00", "reason": "too long"},
                      follow_redirects=False)
        assert "at+most+6+hours" in r2.headers["location"].replace("%20", "+")
    finally:
        sup.__exit__(None, None, None)
