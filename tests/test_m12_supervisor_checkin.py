"""A supervisor must approve each student check-in (unless the student is a supervisor)."""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import clock, emailer, security
from app.errors import UserError
from app.main import create_app
from app.models import Approval, AuditLog, Reservation, ShopSession, StaffUser, Student
from app.services import lifecycle, reports, rotcode, sessions, status
from app.services import settings as shop_settings
from tests.factories import at, book, certify, machine, make_approval, new_student, student
from tests.helpers import staff_login, verify_student

MM1 = "Haas Mini Mill 1"
IP = "1.1.1.1"


@pytest.fixture
def w(db, demo):
    alex = student(db, "alex")
    a = db.scalar(select(Approval).where(Approval.student_id == alex.id))
    shop_settings.set_value(db, "require_supervisor_checkin", True)
    shop_settings.set_value(db, "max_active_per_group", 9)
    db.commit()
    return type("W", (), dict(alex=alex, a=a, jordan=student(db, "jordan")))


def code(db):
    c = rotcode.current(db)["code"]
    db.commit()
    return c


def arrive(db, w, hour=11, minute=5, stu=None, a=None, name=MM1):
    stu = stu or w.alex
    r = book(db, stu, a or w.a, name, at(0, hour))
    db.commit()
    clock.freeze(at(0, hour, minute))
    s = sessions.check_in(db, stu, r.ref, code(db), IP)
    db.commit()  # visible to the HTTP app's own connection (matters on Postgres)
    return r, s


def login(user):
    c = TestClient(create_app(), base_url="http://testserver")
    c.__enter__()
    c.tok = staff_login(c, user)
    return c


def test_student_check_in_waits_for_a_supervisor(db, w):
    emailer.SENT.clear()
    r, s = arrive(db, w)
    assert s.status == "pending" and r.status == "confirmed" and s.requested_at == at(0, 11, 5)
    assert sessions.active_session_for(db, w.alex.id) is None
    st = status.machine_state(db, machine(db, MM1))
    assert st.status == "reserved" and st.current_user == "Alex R." and "supervisor" in st.reason.lower()
    emailer.flush_outbox(db)
    mail = next(m for m in emailer.SENT if "Check-in waiting" in m["subject"])
    assert mail["to"] == "sam@example.edu" and "/admin/checkins" in mail["text"]  # supervisor on duty
    with pytest.raises(UserError) as e:  # cannot check out of a session that never started
        sessions.check_out(db, w.alex, None, code(db), {"machine_cleaned": True, "tools_returned": True,
                                                          "workstation_clean": True}, IP)
    assert e.value.code == "no_session"


def test_approval_starts_the_session_at_approval_time(db, w):
    r, s = arrive(db, w)
    clock.freeze(at(0, 11, 9))
    sessions.approve_checkin(db, s, "staff:sam")
    assert s.status == "active" and r.status == "checked_in" and s.approved_by == "sam"
    assert s.checked_in_at == at(0, 11, 9) and s.requested_at == at(0, 11, 5) and s.expected_end_at == r.end_at
    assert status.machine_state(db, machine(db, MM1)).status == "in_use"
    assert db.scalar(select(AuditLog).where(AuditLog.action == "session.approve")).actor == "staff:sam"
    with pytest.raises(UserError):
        sessions.approve_checkin(db, s, "staff:sam")  # already decided


def test_denied_student_keeps_their_reservation_and_can_ask_again(db, w):
    r, s = arrive(db, w)
    sessions.deny_checkin(db, s, "staff:sam", "cert paperwork missing")
    assert s.status == "denied" and r.status == "confirmed"
    assert status.machine_state(db, machine(db, MM1)).status == "reserved"  # still held until grace logic says otherwise
    s2 = sessions.check_in(db, w.alex, r.ref, code(db), IP)
    assert s2.status == "pending" and s2.id != s.id


def test_double_tap_returns_the_same_request(db, w):
    r, s = arrive(db, w)
    again = sessions.check_in(db, w.alex, r.ref, code(db), IP)
    assert again.id == s.id and len(db.scalars(select(ShopSession)).all()) == 1


def test_only_one_waiting_request_per_student(db, w):
    r2 = book(db, w.alex, w.a, "Haas Mini Mill 2", at(0, 11))
    db.commit()
    r1, s1 = arrive(db, w)
    with pytest.raises(UserError) as e:
        sessions.check_in(db, w.alex, r2.ref, code(db), IP)
    assert e.value.code == "has_pending"


@pytest.mark.parametrize("email,role", [("sam@example.edu", "supervisor"), ("admin@example.edu", "admin")])
def test_supervisors_do_not_need_a_second_person(db, w, email, role):
    stu = new_student(db, email, "Staff Member", "graduate")
    from app.models import AccessRoster
    db.add(AccessRoster(email=email, level="super_approved", added_by="t"))
    certify(db, stu, group="CNC Mills")
    a = make_approval(db, stu, [MM1])
    r, s = arrive(db, w, stu=stu, a=a)
    assert s.status == "active" and s.approved_by == "exempt" and r.status == "checked_in"


def test_staff_email_match_needs_an_active_supervisor_or_admin(db, w):
    assert sessions.is_supervisor(db, "SAM@example.edu") and sessions.is_supervisor(db, "admin@example.edu")
    assert not sessions.is_supervisor(db, "mo@example.edu")  # monitors are read-only, not authorizers
    assert not sessions.is_supervisor(db, w.alex.email)
    sam = db.scalar(select(StaffUser).where(StaffUser.username == "sam"))
    sam.active = False
    assert not sessions.is_supervisor(db, "sam@example.edu")


def test_setting_can_turn_the_rule_off(db, w):
    shop_settings.set_value(db, "require_supervisor_checkin", False)
    r, s = arrive(db, w)
    assert s.status == "active"


def test_force_check_in_by_staff_replaces_a_waiting_request(db, w):
    r, s = arrive(db, w)
    forced = sessions.force_check_in(db, r, "staff:sam", "approved in person")
    assert s.status == "expired" and forced.status == "active" and forced.approved_by == "sam"


def test_waiting_requests_expire_but_the_student_is_never_a_no_show(db, w):
    r, s = arrive(db, w, 11, 5)
    clock.freeze(at(0, 11, 24))
    assert lifecycle.sweep(db)["no_shows"] == 0 and s.status == "pending"  # past grace but waiting
    clock.freeze(at(0, 11, 26))
    out = lifecycle.sweep(db)
    assert out["checkins_expired"] == 1 and s.status == "expired" and out["no_shows"] == 0
    assert r.status == "confirmed" and w.alex.suspended_until is None
    # nobody ever came: when the slot ends it's released quietly (not a no-show, no suspension)
    clock.freeze(at(0, 12, 5))
    lifecycle.sweep(db)
    assert r.status == "cancelled" and "supervisor" in r.cancel_reason
    assert not [x for x in db.scalars(select(Reservation)) if x.status == "no_show"]


def test_late_arrival_after_grace_with_no_request_is_still_a_no_show(db, w):
    r = book(db, w.alex, w.a, MM1, at(0, 11))
    db.commit()
    clock.freeze(at(0, 11, 20))
    assert lifecycle.sweep(db)["no_shows"] == 1 and r.status == "no_show"


def test_waiting_student_can_cancel_their_request(db, w):
    r, s = arrive(db, w)
    sessions.cancel_request(db, s, w.alex)
    assert s.status == "expired"
    with pytest.raises(UserError):
        sessions.cancel_request(db, s, w.alex)


def test_requests_are_not_counted_as_machine_use_in_reports(db, w):
    r, s = arrive(db, w)
    row = next(x for x in reports.utilization(db, clock.to_local(clock.now()).date(), clock.to_local(clock.now()).date())
               if x["machine"] == MM1)
    assert row["used_hours"] == 0
    clock.freeze(at(0, 11, 30))
    sessions.approve_checkin(db, s, "staff:sam")
    clock.freeze(at(0, 12, 0))
    row = next(x for x in reports.utilization(db, clock.to_local(clock.now()).date(), clock.to_local(clock.now()).date())
               if x["machine"] == MM1)
    assert row["used_hours"] == 0.5


def test_authorization_summary_shows_what_the_supervisor_confirms(db, w):
    r, s = arrive(db, w)
    a = sessions.authorization_summary(db, s)
    assert a["approval"] == w.a.number and a["covers"] and a["on_list"] and a["machine_ok"] and a["cert_ok"] and a["waiver"]
    from app.models import Certification
    for c in db.scalars(select(Certification).where(Certification.student_id == w.alex.id)):
        c.revoked_at = clock.now()
    db.flush()
    assert sessions.authorization_summary(db, s)["cert_ok"] is False


def test_tv_shows_waiting_for_supervisor(client, db, w):
    arrive(db, w)
    m = next(x for x in client.get("/tv/data").json()["machines"] if x["name"] == MM1)
    assert m["status"] == "reserved" and m["user"] == "Alex R." and "supervisor" in m["reason"].lower()


def test_admin_page_approve_deny_and_roles(client, db, w):
    r, s = arrive(db, w)
    sup, mon = login("sam"), login("mo")
    try:
        page = sup.get("/admin/checkins").text
        assert "Alex Rivera" in page and "Haas Mini Mill 1" in page and "covers this machine" in page and "Approve" in page
        assert "1 check-in waiting" in sup.get("/admin").text
        assert "Alex Rivera" in mon.get("/admin/checkins").text
        assert mon.post(f"/admin/checkins/{s.id}/approve", data={"csrf_token": mon.tok}, follow_redirects=False).status_code == 403
        ok = sup.post(f"/admin/checkins/{s.id}/approve", data={"csrf_token": sup.tok}, follow_redirects=False)
        assert ok.status_code == 303
        db.expire_all()
        assert db.get(ShopSession, s.id).status == "active"
        assert "Nobody is waiting" in sup.get("/admin/checkins").text
    finally:
        sup.__exit__(None, None, None)
        mon.__exit__(None, None, None)


def test_student_pages_follow_the_whole_flow(client, db, w):
    tok = verify_student(client, w.alex.email, "Alex Rivera")
    r = book(db, w.alex, w.a, MM1, at(0, 11))
    db.commit()
    clock.freeze(at(0, 11, 5))
    resp = client.post("/checkin", data={"csrf_token": tok, "form_token": security.new_form_token(), "ref": r.ref,
                                         "shop_code": code(db)}, follow_redirects=False)
    assert resp.status_code == 303
    url = resp.headers["location"]
    page = client.get(url).text
    assert "You're in the queue" in page and 'data-refresh="5"' in page and "Cancel my request" in page
    sup = login("sam")
    try:
        sid = int(url.rsplit("/", 1)[1])
        sup.post(f"/admin/checkins/{sid}/approve", data={"csrf_token": sup.tok})
    finally:
        sup.__exit__(None, None, None)
    done = client.get(url).text
    assert "You're checked in" in done and "confirmed by sam" in done


def test_student_can_cancel_waiting_request_over_http(client, db, w):
    tok = verify_student(client, w.alex.email, "Alex Rivera")
    r = book(db, w.alex, w.a, MM1, at(0, 11))
    db.commit()
    clock.freeze(at(0, 11, 5))
    url = client.post("/checkin", data={"csrf_token": tok, "form_token": security.new_form_token(), "ref": r.ref,
                                        "shop_code": code(db)}, follow_redirects=False).headers["location"]
    sid = int(url.rsplit("/", 1)[1])
    assert client.post(f"/checkin/done/{sid}/cancel", data={"csrf_token": tok}, follow_redirects=False).status_code == 303
    db.expire_all()
    assert db.get(ShopSession, sid).status == "expired"
    assert "ended without approval" in client.get(url).text
