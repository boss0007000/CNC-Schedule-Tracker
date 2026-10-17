"""Access lists + caps, phone consent, schedules, polite time-change requests, early check-in."""
import json
from datetime import timedelta
from html import unescape

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import clock, emailer
from app.errors import UserError
from app.main import create_app
from app.models import (
    AccessRoster, Approval, AuditLog, Machine, Reservation, ShopSession, Student, TimeChangeRequest, WaitlistEntry,
)
from app.services import access, changes, contact, reservations as rs, rotcode, sessions
from app.services import settings as shop_settings
from tests.factories import at, book, certify, machine, make_approval, student
from tests.helpers import staff_login, verify_student

MM1, MM2, VF3, ST10, FC50 = "Haas Mini Mill 1", "Haas Mini Mill 2", "Haas VF-3", "Haas ST-10", "Fibercell FC50"
CL = {"machine_cleaned": True, "tools_returned": True, "workstation_clean": True}


@pytest.fixture
def w(db, demo):
    alex, jordan, sam = student(db, "alex"), student(db, "jordan"), student(db, "sam")  # super, super, approved
    a_alex = db.scalar(select(Approval).where(Approval.student_id == alex.id))
    certify(db, sam, group="CNC Mills")
    a_sam = make_approval(db, sam, [MM1, MM2, VF3, FC50, ST10], hours=50)
    a_jordan = make_approval(db, jordan, [MM1, MM2], hours=50)
    shop_settings.set_value(db, "max_active_per_group", 9)
    db.commit()
    return type("W", (), dict(alex=alex, jordan=jordan, sam=sam, a_alex=a_alex, a_sam=a_sam, a_jordan=a_jordan))


def err(db, stu, a, name, start, hours=1):
    with pytest.raises(UserError) as e:
        book(db, stu, a, name, start, hours)
    return e.value


def code(db):
    c = rotcode.current(db)["code"]
    db.commit()
    return c


# ============================================================ access lists
def test_must_be_on_a_list_to_book(db, w):
    stranger = student(db, "riley")
    db.query(AccessRoster).filter_by(email=stranger.email).delete()
    a = make_approval(db, stranger, [MM1])
    certify(db, stranger, group="CNC Mills")
    e = err(db, stranger, a, MM1, at(1, 10))
    assert e.code == "not_listed" and "added to the shop's approved list" in e.message


def test_approved_can_book_standard_machines_only(db, w):
    book(db, w.sam, w.a_sam, MM1, at(1, 10))
    book(db, w.sam, w.a_sam, MM2, at(1, 12))
    for name in (VF3, ST10):
        e = err(db, w.sam, w.a_sam, name, at(1, 10))
        assert e.code == "needs_more_access" and "additional shop sign-off" in e.message
        assert "super approved" not in e.message.lower()  # nothing in messages ranks people


def test_super_approved_can_book_everything_except_staff_run(db, w):
    for name, h in ((MM1, 9), (VF3, 11), (ST10, 13)):
        a = w.a_alex if name != ST10 else make_approval(db, w.alex, [ST10])
        certify(db, w.alex, group="CNC Lathes") if name == ST10 else None
        book(db, w.alex, a, name, at(1, h))


def test_staff_run_machine_cannot_be_booked_by_anyone_but_message_points_to_submitting(db, w):
    for stu, a in ((w.sam, w.a_sam), (w.alex, make_approval(db, w.alex, [FC50]))):
        e = err(db, stu, a, FC50, at(1, 10))
        assert e.code == "staff_only" and "Submit your part" in e.message


def test_staff_can_still_book_staff_run_machine_manually(db, w):
    r = rs.create(db, w.sam, None, machine(db, FC50).id, at(1, 10), at(1, 11), actor="staff:sam", override=True)
    assert r.manual


def test_session_caps_per_level(db, w):
    shop_settings.set_value(db, "max_session_hours_standard", 2)
    shop_settings.set_value(db, "max_session_hours_advanced", 3)
    assert err(db, w.sam, w.a_sam, MM1, at(1, 9), 3).code == "too_long"
    book(db, w.sam, w.a_sam, MM1, at(1, 9), 2)
    book(db, w.alex, w.a_alex, MM2, at(1, 9), 3)
    e = err(db, w.alex, w.a_alex, MM1, at(2, 9), 4)
    assert e.code == "too_long" and "at most 3 hours" in e.message


def test_default_caps_are_six_and_twelve_hours(db, demo):
    cfg = shop_settings.load(db)
    assert cfg.max_session_hours_standard == 6 and cfg.max_session_hours_advanced == 12
    assert access.session_cap_hours(db, "approved") == 6 and access.session_cap_hours(db, "super_approved") == 12


def test_no_weekly_limits_by_default(db, w):
    for h in (9, 10, 11, 12, 13, 14):
        book(db, w.sam, w.a_sam, MM1, at(1, h))


def test_access_list_can_be_switched_off(db, w):
    stranger = student(db, "riley")
    db.query(AccessRoster).filter_by(email=stranger.email).delete()
    a = make_approval(db, stranger, [MM1])
    certify(db, stranger, group="CNC Mills")
    shop_settings.set_value(db, "require_access_list", False)
    book(db, stranger, a, MM1, at(1, 10))
    assert err(db, stranger, make_approval(db, stranger, [VF3]), VF3, at(1, 12)).code == "needs_more_access"


def test_access_is_rechecked_at_check_in(db, w):
    r = book(db, w.sam, w.a_sam, MM1, at(0, 11))
    db.query(AccessRoster).filter_by(email=w.sam.email).delete()
    db.commit()
    clock.freeze(at(0, 10, 55))
    with pytest.raises(UserError) as e:
        sessions.check_in(db, w.sam, r.ref, code(db), "1.1.1.1")
    assert e.value.code == "not_listed"


def test_roster_management_and_audit(db, w):
    emails = access.parse_emails("A@csuchico.edu, b@csuchico.edu\nnot-an-email  a@csuchico.edu")
    assert emails == ["a@csuchico.edu", "b@csuchico.edu"]
    res = access.set_levels(db, "staff:admin", emails, "approved", "spring cohort")
    assert res == {"added": 2, "moved": 0, "unchanged": 0}
    assert access.set_levels(db, "staff:admin", ["a@csuchico.edu"], "super_approved")["moved"] == 1
    assert access.level_for(db, "A@CSUCHICO.EDU") == "super_approved"
    access.remove(db, "staff:admin", "b@csuchico.edu")
    assert access.level_for(db, "b@csuchico.edu") is None
    acts = [a.action for a in db.scalars(select(AuditLog))]
    assert {"access.add", "access.move", "access.remove"} <= set(acts)


def login(user):
    c = TestClient(create_app(), base_url="http://testserver")
    c.__enter__()
    c.tok = staff_login(c, user)
    return c


def test_only_admins_manage_the_super_approved_list(db, w):
    sup, adm, mon = login("sam"), login("admin"), login("mo")
    try:
        post = lambda c, **d: c.post("/admin/access", data={"csrf_token": c.tok, **d}, follow_redirects=False)  # noqa: E731
        r = post(sup, action="add", level="super_approved", emails="new1@csuchico.edu")
        assert "Only+an+admin" in r.headers["location"].replace("%20", "+") and access.level_for(db, "new1@csuchico.edu") is None
        assert post(sup, action="add", level="approved", emails="new1@csuchico.edu").status_code == 303
        assert access.level_for(db, "new1@csuchico.edu") == "approved"
        assert post(adm, action="add", level="super_approved", emails="new1@csuchico.edu").status_code == 303
        db.expire_all()
        assert access.level_for(db, "new1@csuchico.edu") == "super_approved"
        assert post(mon, action="add", level="approved", emails="x@csuchico.edu").status_code == 403
        r2 = post(sup, action="remove", email="new1@csuchico.edu")
        assert "Only+an+admin" in r2.headers["location"].replace("%20", "+")
        assert "Access lists" in sup.get("/admin/access").text
        assert sup.get("/admin/requests").status_code == 200
    finally:
        for c in (sup, adm, mon):
            c.__exit__(None, None, None)


def test_reserve_page_only_offers_machines_the_student_can_book(client, db, w):
    verify_student(client, w.sam.email, "Sam Patel", "sophomore")
    page = client.get("/reserve").text
    assert "Haas Mini Mill 1" in page and "Haas Mini Mill 2" in page
    for hidden in ("Haas VF-3", "Haas ST-10", "Fibercell FC50"):
        assert hidden not in page


def test_machine_admin_can_set_access_level(db, w):
    adm = login("admin")
    try:
        m = machine(db, MM1)
        r = adm.post("/admin/machines/save", data={"csrf_token": adm.tok, "id": str(m.id), "name": m.name,
                                                   "access_level": "staff_only", "active": "on"}, follow_redirects=False)
        assert r.status_code == 303
        db.expire_all()
        assert machine(db, MM1).access_level == "staff_only"
    finally:
        adm.__exit__(None, None, None)


# ============================================================ phone + consent
@pytest.mark.parametrize("raw,out", [("(530) 555-0123", "+15305550123"), ("530.555.0123", "+15305550123"),
                                     ("1-530-555-0123", "+15305550123"), ("+44 20 7946 0958", "+442079460958"), ("", None)])
def test_phone_normalisation(raw, out):
    assert contact.normalize_phone(raw) == out


@pytest.mark.parametrize("raw", ["12345", "call me", "555-0123", "+1", "530-555-01234x"])
def test_bad_phone_numbers_rejected(raw):
    with pytest.raises(UserError):
        contact.normalize_phone(raw)


def test_contact_choice_phone_email_or_nothing_and_audit_never_stores_it(db, w):
    s = w.sam
    contact.update_contact(db, s, "(530) 555-0123", "phone")
    assert s.phone == "+15305550123" and s.shown_contact == ("phone", "+15305550123") and contact.has_contact(s)
    contact.update_contact(db, s, "(530) 555-0123", "email")  # number kept private, email shown instead
    assert s.phone == "+15305550123" and s.shown_contact == ("email", s.email)
    contact.update_contact(db, s, "(530) 555-0123", "none")  # nothing shown, number still on file for staff
    assert s.shown_contact is None and s.phone == "+15305550123" and not contact.has_contact(s)
    contact.update_contact(db, s, "", "phone")  # chose phone but gave none: email is shown instead
    assert s.shown_contact == ("email", s.email)
    contact.update_contact(db, s, "", "bogus")  # anything unknown means private
    assert s.shown_contact is None
    rows = db.scalars(select(AuditLog).where(AuditLog.action == "student.contact_update")).all()
    blob = json.dumps([[r.before, r.after] for r in rows])
    assert "5305550123" not in blob and s.email not in blob


def test_contact_collected_on_submit_and_checkin_and_my_page(client, db, w):
    from app import security
    w.sam.phone, w.sam.phone_ack_at, w.sam.contact_display = None, None, None
    db.commit()
    tok = verify_student(client, w.sam.email, "Sam Patel", "sophomore")
    d = {"csrf_token": tok, "form_token": security.new_form_token(), "title": "Part", "description": "A test part for phones.",
         "machine_ids": [str(machine(db, MM1).id)], "estimated_hours": "2", "phone": "530-555-0123", "show_contact": "phone"}
    step = [("files", ("p.step", b"ISO-10303-21;\nDATA;\n", "application/octet-stream"))]
    assert client.post("/submit", data=d, files=step, follow_redirects=False).status_code == 303
    db.expire_all()
    assert student(db, "sam").phone == "+15305550123" and student(db, "sam").shown_contact == ("phone", "+15305550123")
    # phone given but kept private
    r = client.post("/my/contact", data={"csrf_token": tok, "phone": "(530) 555-0888", "show_contact": "none", "allow_requests": "on"})
    db.expire_all()
    assert student(db, "sam").phone == "+15305550888" and student(db, "sam").shown_contact is None
    # My shop: show the email instead
    client.post("/my/contact", data={"csrf_token": tok, "phone": "", "show_contact": "email", "allow_requests": "on"})
    db.expire_all()
    assert student(db, "sam").shown_contact == ("email", w.sam.email)
    # check-in form saves the choice too
    rr = book(db, w.sam, w.a_sam, MM1, at(0, 11))
    db.commit()
    clock.freeze(at(0, 10, 55))
    resp = client.post("/checkin", data={"csrf_token": tok, "form_token": security.new_form_token(), "ref": rr.ref,
                                         "shop_code": code(db), "phone": "(530) 555-9999", "show_contact": "phone"},
                       follow_redirects=False)
    assert resp.status_code == 303
    db.expire_all()
    assert student(db, "sam").shown_contact == ("phone", "+15305559999")
    bad = client.post("/submit", data={**d, "form_token": security.new_form_token(), "phone": "abc"}, files=step)
    assert bad.status_code == 400 and "valid phone number" in unescape(bad.text)


# ============================================================ schedules
def test_schedule_pages_need_no_login(client, db, w):
    book(db, w.jordan, w.a_jordan, MM1, at(1, 10))
    db.commit()
    day = clock.to_local(at(1, 10)).date()
    idx = client.get("/schedule")
    assert idx.status_code == 200 and "Haas VF-3" in idx.text
    page = client.get(f"/machine/{machine(db, MM1).id}?date={day}")
    assert page.status_code == 200 and "Jordan L." in page.text
    assert "(530) 555-0102" in page.text and "tel:+15305550102" in page.text  # numbers are shown, as requested
    for leak in ("@csuchico.edu", "APR-2026", "RSV-", "super approved"):
        assert leak.lower() not in page.text.lower()
    assert "Sign in to reserve a time" in page.text
    assert 'name="robots" content="noindex' in page.text
    robots = client.get("/robots.txt").text
    assert "Disallow: /schedule" in robots and "Disallow: /machine/" in robots
    assert client.get("/machine/9999").status_code == 404


def test_public_schedule_can_hide_phone_numbers_but_students_still_see_them(client, db, w):
    book(db, w.jordan, w.a_jordan, MM1, at(1, 10))
    shop_settings.set_value(db, "public_schedule_show_phone", False)
    db.commit()
    day = clock.to_local(at(1, 10)).date()
    url = f"/machine/{machine(db, MM1).id}?date={day}"
    anon = client.get(url).text
    assert "Jordan L." in anon and "5305550102" not in anon and "555-0102" not in anon
    verify_student(client, w.alex.email, "Alex Rivera")
    assert "tel:+15305550102" in client.get(url).text  # a signed-in student still can call


def test_public_schedule_lookups_are_rate_limited_per_address(client, db, w):
    url = f"/machine/{machine(db, MM1).id}"
    codes = [client.get(url).status_code for _ in range(302)]
    assert codes.count(200) == 300 and codes[-1] in (400, 429)


def test_schedule_always_shows_phone_numbers_of_other_students(client, db, w):
    book(db, w.jordan, w.a_jordan, MM1, at(1, 10))
    book(db, w.sam, w.a_sam, MM1, at(1, 13))
    db.commit()
    verify_student(client, w.alex.email, "Alex Rivera")
    day = clock.to_local(at(1, 10)).date()
    page = client.get(f"/machine/{machine(db, MM1).id}?date={day}").text
    assert "Jordan L." in page and "Sam P." in page
    assert "(530) 555-0102" in page and "tel:+15305550102" in page and "sms:+15305550102" in page  # Jordan: always shown
    assert "(530) 555-0103" in page and "tel:+15305550103" in page
    assert page.count("Ask to change time") == 2
    for leak in ("@csuchico.edu", "APR-2026", "super approved", "RSV-"):
        assert leak.lower() not in page.lower()
    assert db.scalar(select(AuditLog).where(AuditLog.action == "contact.view"))


def test_schedule_does_not_show_your_own_number_as_a_contact(client, db, w):
    book(db, w.alex, w.a_alex, MM1, at(1, 10))
    db.commit()
    verify_student(client, w.alex.email, "Alex Rivera")
    day = clock.to_local(at(1, 10)).date()
    page = client.get(f"/machine/{machine(db, MM1).id}?date={day}").text
    assert "<b>You</b>" in page and "tel:+15305550101" not in page


def test_schedule_hides_request_button_when_student_opted_out(client, db, w):
    w.jordan.allow_requests = False
    book(db, w.jordan, w.a_jordan, MM1, at(1, 10))
    db.commit()
    verify_student(client, w.alex.email, "Alex Rivera")
    day = clock.to_local(at(1, 10)).date()
    assert "Ask to change time" not in client.get(f"/machine/{machine(db, MM1).id}?date={day}").text


def test_staff_run_machine_page_explains_and_index_lists_all(client, db, w):
    verify_student(client, w.alex.email, "Alex Rivera")
    page = client.get(f"/machine/{machine(db, FC50).id}").text
    assert "run by shop staff" in page
    idx = client.get("/schedule").text
    for n in (MM1, MM2, VF3, ST10, FC50):
        assert n in idx


def test_tv_has_machine_qr_and_staff_run_flag(client, db, w):
    d = client.get("/tv/data").json()
    fc = next(m for m in d["machines"] if m["name"] == FC50)
    assert fc["staff_run"] is True and fc["qr"].startswith("/qr/machine/")
    assert not next(m for m in d["machines"] if m["name"] == MM1)["staff_run"]
    png = client.get(fc["qr"])
    assert png.status_code == 200 and png.content.startswith(b"\x89PNG")
    assert client.get("/qr/machine/9999.png").status_code == 404


def test_tv_shows_contact_of_current_user_and_next_booking_and_can_hide_it(client, db, w):
    r = book(db, w.alex, w.a_alex, MM1, at(0, 11))
    book(db, w.jordan, w.a_jordan, MM1, at(0, 14))
    db.commit()
    clock.freeze(at(0, 11, 5))
    sessions.check_in(db, w.alex, r.ref, code(db), "1.1.1.1")
    db.commit()
    m = next(x for x in client.get("/tv/data").json()["machines"] if x["name"] == MM1)
    assert m["user"] == "Alex R." and m["user_contact"] == {"kind": "phone", "text": "(530) 555-0101"}
    assert m["next_user"] == "Jordan L." and m["next_contact"] == {"kind": "phone", "text": "(530) 555-0102"}
    assert "alex.rivera" not in json.dumps(m)  # no email unless the student chose to show it
    # Jordan chooses to show his email instead; Alex shows nothing
    w.jordan.contact_display, w.alex.contact_display = "email", "none"
    db.commit()
    m = next(x for x in client.get("/tv/data").json()["machines"] if x["name"] == MM1)
    assert m["user_contact"] is None and m["next_contact"] == {"kind": "email", "text": w.jordan.email}
    shop_settings.set_value(db, "tv_show_phone", False)
    db.commit()
    m = next(x for x in client.get("/tv/data").json()["machines"] if x["name"] == MM1)
    assert m["user_contact"] is None and m["next_contact"] is None and m["user"] == "Alex R."


# ============================================================ time-change requests (ask for a time, target picks a new one)
@pytest.fixture
def pair(db, w):
    """Jordan (student A) holds the Mini Mill 1 from 2 PM to 5 PM today; Alex (student B) would like 1-3 PM."""
    target = book(db, w.jordan, w.a_jordan, MM1, at(0, 14), 3)
    db.commit()
    return target


def ask(db, w, target, start=13, end=15, note="Thanks!"):
    return changes.create(db, w.alex, target.id, at(0, start), at(0, end), note)


def win(db, target, ws=13, we=15):
    return [(o["start"], o["end"], o["kind"]) for o in changes.target_options(db, target, (at(0, ws), at(0, we)), clock.now())]


def test_wanted_options_overlap_the_target_and_are_otherwise_free(db, w, pair):
    book(db, w.sam, w.a_sam, MM1, at(0, 11), 1) if False else None
    opts = changes.wanted_options(db, w.alex, pair, clock.now())
    assert (at(0, 13), at(0, 15)) in opts and (at(0, 14), at(0, 16)) in opts and (at(0, 16), at(0, 18)) in opts
    assert all(s < pair.end_at and e > pair.start_at for s, e in opts)  # every option overlaps their booking
    assert all(s > clock.now() for s, _ in opts)
    assert (at(0, 17), at(0, 18)) not in opts  # doesn't touch their booking


def test_wanted_options_exclude_times_blocked_by_other_bookings(db, w, pair):
    other = new_student_with_booking(db, w, MM1, at(0, 12), 1)  # someone else holds 12-1 PM
    opts = changes.wanted_options(db, w.alex, pair, clock.now())
    assert (at(0, 13), at(0, 15)) in opts and (at(0, 12), at(0, 15)) not in opts and (at(0, 12), at(0, 14)) not in opts
    assert other.status == "confirmed"


def new_student_with_booking(db, w, name, start, hours):
    riley = student(db, "riley")
    a = make_approval(db, riley, [name])
    certify(db, riley, group="CNC Mills")
    return book(db, riley, a, name, start, hours)


def test_request_email_is_polite_names_the_wanted_time_and_leaks_nothing(db, w, pair):
    emailer.SENT.clear()
    req = ask(db, w, pair)
    emailer.flush_outbox(db)
    mail = next(m for m in emailer.SENT if "would like the" in m["subject"])
    t = mail["text"]
    assert mail["to"] == w.jordan.email and "Alex R." in t and "1:00 PM" in t and "3:00 PM" in t and "overlaps your booking" in t
    assert "optional" in t and "pick a new time that suits YOU" in t and f"/requests/{req.id}" in t and "Thanks!" in t
    for leak in ("5305550101", w.alex.email, "super approved", "approved list"):
        assert leak not in t
    assert req.wanted_start == at(0, 13) and req.wanted_end == at(0, 15) and req.status == "pending"


def test_cannot_ask_for_own_muted_or_too_soon_bookings(db, w, pair):
    own = book(db, w.alex, w.a_alex, MM2, at(0, 12))
    with pytest.raises(UserError) as e:
        changes.create(db, w.alex, own.id, at(0, 12), at(0, 13), "")
    assert e.value.code == "own"
    w.jordan.allow_requests = False
    with pytest.raises(UserError) as e:
        ask(db, w, pair)
    assert e.value.code == "muted"
    w.jordan.allow_requests = True
    clock.freeze(at(0, 12, 30))  # their booking is 90 minutes away: too close to ask
    with pytest.raises(UserError) as e:
        ask(db, w, pair)
    assert e.value.code == "too_late"


def test_wanted_time_must_overlap_and_be_free_and_legal(db, w, pair):
    with pytest.raises(UserError) as e:
        ask(db, w, pair, 18, 19)  # nowhere near their booking
    assert e.value.code == "bad_window"
    new_student_with_booking(db, w, MM1, at(0, 12), 1)
    with pytest.raises(UserError) as e:
        ask(db, w, pair, 12, 15)  # 12-1 belongs to somebody else
    assert e.value.code == "bad_window"
    from tests.factories import new_student
    stranger = new_student(db, "nobody@csuchico.edu")  # on no list and with no approval
    with pytest.raises(UserError) as e:
        changes.create(db, stranger, pair.id, at(0, 13), at(0, 15), "")
    assert e.value.code in ("bad_window", "approval_not_found")


def test_limits_against_pestering(db, w, pair):
    shop_settings.set_value(db, "max_open_change_requests", 2)
    ask(db, w, pair)
    with pytest.raises(UserError) as e:  # one open ask per booking
        ask(db, w, pair, 14, 16)
    assert e.value.code == "dup"
    t2 = book(db, w.sam, w.a_sam, MM2, at(1, 9), 3)
    t3 = book(db, w.sam, w.a_sam, MM1, at(1, 14), 2)
    changes.create(db, w.alex, t2.id, at(1, 8), at(1, 10), "")
    with pytest.raises(UserError) as e:
        changes.create(db, w.alex, t3.id, at(1, 13), at(1, 15), "")
    assert e.value.code == "limit"


def test_target_options_offer_trim_and_moves_including_the_example_3_to_6(db, w, pair):
    opts = win(db, pair)
    assert (at(0, 15), at(0, 17), "trim") in opts  # keep just the part that doesn't clash
    assert (at(0, 15), at(0, 18), "move") in opts  # "3-6 instead"
    assert all(not (s < at(0, 15) and e > at(0, 13)) for s, e, _ in opts)  # nothing overlaps what B wants
    assert all(e - s <= timedelta(hours=3) for s, e, _ in opts)  # never longer than their current time
    new_student_with_booking(db, w, MM1, at(0, 17), 1)  # 5-6 PM taken: 3-6 is no longer possible
    assert (at(0, 15), at(0, 18), "move") not in win(db, pair)


def test_accept_moves_the_target_books_the_asker_and_alerts_everyone(db, w, pair):
    req = ask(db, w, pair)
    emailer.SENT.clear()
    new_res = changes.accept(db, req, w.jordan, at(0, 15), at(0, 18))
    assert pair.start_at == at(0, 15) and pair.end_at == at(0, 18) and pair.status == "confirmed"
    assert new_res.student_id == w.alex.id and new_res.start_at == at(0, 13) and new_res.end_at == at(0, 15)
    assert new_res.status == "confirmed" and new_res.machine_id == pair.machine_id
    assert req.status == "accepted" and req.new_start == at(0, 15) and req.new_end == at(0, 18)
    emailer.flush_outbox(db)
    by_to = {}
    for m in emailer.SENT:
        by_to.setdefault(m["to"], []).append(m)
    assert any("Confirmed" in m["subject"] and new_res.ref in m["text"] for m in by_to[w.alex.email])
    assert any("moved to" in m["subject"] and pair.ref in m["text"] for m in by_to[w.jordan.email])
    staff = by_to["sam@example.edu"]  # supervisor on duty
    assert any("Schedule swap" in m["subject"] and new_res.ref in m["text"] for m in staff)
    row = db.scalar(select(AuditLog).where(AuditLog.action == "change_request.accept"))
    assert row.actor == f"student:{w.jordan.email}" and row.after["requester_ref"] == new_res.ref


def test_accept_can_keep_just_the_part_that_does_not_clash(db, w, pair):
    req = ask(db, w, pair)
    changes.accept(db, req, w.jordan, at(0, 15), at(0, 17))
    assert pair.start_at == at(0, 15) and pair.end_at == at(0, 17) and pair.hours == 2


def test_accept_with_a_time_not_offered_changes_nothing(db, w, pair):
    req = ask(db, w, pair)
    with pytest.raises(UserError) as e:
        changes.accept(db, req, w.jordan, at(0, 14), at(0, 17))  # clashes with what the asker wants
    assert e.value.code == "bad_choice"
    assert pair.start_at == at(0, 14) and req.status == "pending"
    assert db.scalar(select(Reservation).where(Reservation.student_id == w.alex.id)) is None


def test_accept_is_all_or_nothing_if_the_asker_stops_being_eligible(db, w, pair):
    req = ask(db, w, pair)
    w.a_alex.status = "revoked"  # the asker's approval is revoked before the answer arrives
    db.flush()
    with pytest.raises(UserError):
        changes.accept(db, req, w.jordan, at(0, 15), at(0, 18))
    db.refresh(pair)
    assert pair.start_at == at(0, 14) and pair.end_at == at(0, 17) and req.status == "pending"
    assert db.scalar(select(Reservation).where(Reservation.student_id == w.alex.id)) is None


def test_decline_is_neutral_cooled_down_and_changes_nothing(db, w, pair):
    shop_settings.set_value(db, "change_request_cooldown_hours", 1)
    req = ask(db, w, pair)
    emailer.SENT.clear()
    changes.decline(db, req, w.jordan)
    emailer.flush_outbox(db)
    assert pair.start_at == at(0, 14) and req.status == "declined"
    t = emailer.SENT[-1]["text"]
    assert emailer.SENT[-1]["to"] == w.alex.email and "weren't able" in t and "Jordan" not in t
    with pytest.raises(UserError) as e:
        ask(db, w, pair, 14, 16)
    assert e.value.code == "cooldown"
    clock.advance(hours=2)  # cooldown over: asking again is fine
    ask(db, w, pair, 14, 16)


def test_only_the_asked_student_can_answer_and_only_while_open(db, w, pair):
    req = ask(db, w, pair)
    for fn, args in ((changes.accept, (at(0, 15), at(0, 18))), (changes.decline, ())):
        with pytest.raises(UserError) as e:
            fn(db, req, w.sam, *args)
        assert e.value.code == "not_yours"
    with pytest.raises(UserError):
        changes.cancel(db, req, w.jordan)  # only the asker can cancel
    changes.cancel(db, req, w.alex)
    with pytest.raises(UserError) as e:
        changes.accept(db, req, w.jordan, at(0, 15), at(0, 18))
    assert e.value.code == "closed"


def test_requests_expire(db, w, pair):
    req = ask(db, w, pair)
    assert req.expires_at <= pair.start_at - timedelta(hours=1)
    clock.freeze(req.expires_at + timedelta(minutes=1))
    from app.services import lifecycle
    assert lifecycle.sweep(db)["requests_expired"] == 1 and req.status == "expired"
    with pytest.raises(UserError):
        changes.accept(db, req, w.jordan, at(0, 15), at(0, 18))


def test_request_flow_over_http_ask_then_pick_a_new_time(client, db, w, pair):
    from app import security
    tok = verify_student(client, w.alex.email, "Alex Rivera")
    page = client.get(f"/requests/new?res={pair.id}")
    assert page.status_code == 200 and "Ask for a different time" in page.text and 'name="window"' in page.text
    wanted = f"{at(0, 13).isoformat()}|{at(0, 15).isoformat()}"
    r = client.post("/requests/new", data={"csrf_token": tok, "form_token": security.new_form_token(), "res": str(pair.id),
                                           "window": wanted, "note": "please?"}, follow_redirects=False)
    assert r.status_code == 303
    req = db.scalar(select(TimeChangeRequest))
    with TestClient(client.app, base_url="http://testserver") as jc:
        jt = verify_student(jc, w.jordan.email, "Jordan Lee", "senior")
        assert "wants the" in jc.get("/my").text
        view = jc.get(f"/requests/{req.id}")
        assert "Alex R." in view.text and 'name="choice"' in view.text and "No thanks" in view.text and "move me to the selected time" in view.text
        bad = jc.post(f"/requests/{req.id}/accept", data={"csrf_token": jt, "choice": f"{at(0, 14).isoformat()}|{at(0, 17).isoformat()}"})
        assert bad.status_code == 400 and "isn&#39;t available" in bad.text.replace("’", "&#39;") or bad.status_code == 400
        choice = f"{at(0, 15).isoformat()}|{at(0, 18).isoformat()}"
        ok = jc.post(f"/requests/{req.id}/accept", data={"csrf_token": jt, "choice": choice}, follow_redirects=False)
        assert ok.status_code == 303
    db.expire_all()
    assert db.get(TimeChangeRequest, req.id).status == "accepted"
    assert db.get(Reservation, pair.id).start_at == at(0, 15)
    mine = db.scalar(select(Reservation).where(Reservation.student_id == w.alex.id))
    assert mine and mine.start_at == at(0, 13) and mine.end_at == at(0, 15)
    assert "You asked" in client.get(f"/requests/{req.id}").text
    other = TestClient(client.app, base_url="http://testserver")
    with other:
        verify_student(other, w.sam.email, "Sam Patel", "sophomore")
        assert other.get(f"/requests/{req.id}").status_code == 404


def test_staff_can_see_requests_with_the_wanted_and_new_time(db, w, pair):
    req = ask(db, w, pair)
    changes.accept(db, req, w.jordan, at(0, 15), at(0, 18))
    db.commit()
    sup = login("sam")
    try:
        page = sup.get("/admin/requests").text
        assert "Alex Rivera" in page and "Jordan Lee" in page and "accepted" in page and "1:00 PM" in page
    finally:
        sup.__exit__(None, None, None)


def test_no_priority_or_rank_fields_exist():
    cols = {c.name for c in TimeChangeRequest.__table__.columns}
    assert not cols & {"priority", "urgency", "critical", "rank", "score", "level"}
    assert {"wanted_start", "wanted_end", "new_start", "new_end"} <= cols
    assert not {c.name for c in Student.__table__.columns} & {"score", "rank", "points", "karma"}


# ============================================================ early start
def test_early_checkin_when_machine_is_idle_and_nobody_is_before_you(db, w):
    r = book(db, w.alex, w.a_alex, MM1, at(0, 13))
    db.commit()
    clock.freeze(at(0, 11, 30))  # 90 min early: inside the 120-minute window
    s = sessions.check_in(db, w.alex, r.ref, code(db), "1.1.1.1")
    assert s.checked_in_at == at(0, 11, 30) and s.expected_end_at == r.end_at  # end time unchanged


def test_early_checkin_blocked_beyond_window_or_when_someone_is_booked_first(db, w):
    r = book(db, w.alex, w.a_alex, MM1, at(0, 15))
    db.commit()
    clock.freeze(at(0, 12, 30))  # 150 minutes early
    with pytest.raises(UserError) as e:
        sessions.check_in(db, w.alex, r.ref, code(db), "1.1.1.1")
    assert e.value.code == "too_early" and "earlier if the machine is free" in e.value.message
    book(db, w.jordan, w.a_jordan, MM1, at(0, 14))
    db.commit()
    clock.freeze(at(0, 13, 30))  # within window by time, but Jordan is booked 14:00-15:00 first
    with pytest.raises(UserError) as e:
        sessions.check_in(db, w.alex, r.ref, code(db), "1.1.1.1")
    assert e.value.code == "too_early"


def test_early_checkin_blocked_while_machine_is_in_use(db, w):
    first = book(db, w.jordan, w.a_jordan, MM1, at(0, 11))
    r = book(db, w.alex, w.a_alex, MM1, at(0, 13))
    db.commit()
    clock.freeze(at(0, 11, 5))
    sessions.check_in(db, w.jordan, first.ref, code(db), "1.1.1.1")
    db.commit()
    clock.freeze(at(0, 12, 0))
    with pytest.raises(UserError) as e:
        sessions.check_in(db, w.alex, r.ref, code(db), "1.1.1.1")
    assert e.value.code == "too_early"


def test_early_checkout_tells_only_the_next_student_once(db, w):
    first = book(db, w.jordan, w.a_jordan, MM1, at(0, 11), 2)
    nxt = book(db, w.alex, w.a_alex, MM1, at(0, 13))
    later = book(db, w.sam, w.a_sam, MM1, at(0, 15))
    db.commit()
    clock.freeze(at(0, 11, 5))
    sessions.check_in(db, w.jordan, first.ref, code(db), "1.1.1.1")
    db.commit()
    clock.freeze(at(0, 11, 30))
    emailer.SENT.clear()
    sessions.check_out(db, w.jordan, None, code(db), CL, "1.1.1.1")
    emailer.flush_outbox(db)
    free = [m for m in emailer.SENT if "is free right now" in m["subject"]]
    assert [m["to"] for m in free] == [w.alex.email]  # next in line only
    assert "No obligation" in free[0]["text"] and "end time stays" in free[0]["text"]
    sessions.notify_next_up(db, nxt.machine_id, clock.now())
    emailer.flush_outbox(db)
    assert len([m for m in emailer.SENT if "is free right now" in m["subject"]]) == 1  # not repeated


def test_cancellation_also_offers_early_start_to_next_student(db, w):
    gone = book(db, w.jordan, w.a_jordan, MM1, at(0, 11))
    nxt = book(db, w.alex, w.a_alex, MM1, at(0, 12, 30) if False else at(0, 12))
    db.commit()
    emailer.SENT.clear()
    clock.freeze(at(0, 10, 5))
    rs.cancel(db, gone, actor="staff:sam", by_staff=True, quiet=True)
    emailer.flush_outbox(db)
    assert any(m["to"] == w.alex.email and "free right now" in m["subject"] for m in emailer.SENT)


# ============================================================ access list = authorization; clearer reserve page
def test_access_list_alone_authorizes_a_machine_marked_requires_training(db, w):
    riley = student(db, "riley")  # approved list, NO certification record
    assert machine(db, MM1).requires_certification
    a = make_approval(db, riley, [MM1])
    book(db, riley, a, MM1, at(1, 10))  # works: the list is the authorization


def test_individual_certifications_can_still_be_required_on_top(db, w):
    riley = student(db, "riley")
    a = make_approval(db, riley, [MM1])
    shop_settings.set_value(db, "require_certifications", True)
    assert err(db, riley, a, MM1, at(1, 10)).code == "cert_missing"
    certify(db, riley, group="CNC Mills")
    book(db, riley, a, MM1, at(1, 10))


def test_without_access_lists_certifications_are_the_gate(db, w):
    riley = student(db, "riley")
    a = make_approval(db, riley, [MM1])
    shop_settings.set_value(db, "require_access_list", False)
    assert err(db, riley, a, MM1, at(1, 10)).code == "cert_missing"


def test_reserve_page_jumps_to_times_and_always_says_why_nothing_is_shown(client, db, w):
    verify_student(client, w.sam.email, "Sam Patel", "sophomore")
    page = client.get("/reserve").text
    assert 'action="/reserve#times"' in page and 'id="times"' in page
    day = clock.to_local(at(1, 10)).date()
    a = make_approval(db, w.sam, [MM1])
    certify(db, w.sam, group="CNC Mills")
    db.commit()
    ok = client.get(f"/reserve?approval={a.number}&machine={machine(db, MM1).id}&date={day}").text
    assert "Step 2: choose a time" in ok and ok.count('name="start"') == 14  # a certification-flagged machine just works
    clock.freeze(at(0, 22, 30))  # after closing: today has nothing left, and the page must say so
    today = clock.to_local(clock.now()).date()
    none = client.get(f"/reserve?approval={a.number}&machine={machine(db, MM1).id}&date={today}").text
    assert "no open times left today" in unescape(none)


# ============================================================ per-group machine matrix + never-silent time list
def test_matrix_lets_staff_pick_machines_per_group(db, w):
    sam_m = machine(db, MM1)
    assert access.machine_levels(sam_m) == {"approved", "super_approved"}
    access.set_machine_levels(db, "staff:admin", machine(db, VF3), {"approved", "super_approved"})  # open the VF-3 to both
    book(db, w.sam, w.a_sam, VF3, at(1, 10))  # an Approved student can now book it
    access.set_machine_levels(db, "staff:admin", sam_m, {"super_approved"})  # Mini Mill 1: super approved only
    assert err(db, w.sam, w.a_sam, MM1, at(1, 12)).code == "needs_more_access"
    book(db, w.alex, w.a_alex, MM1, at(1, 12))
    access.set_machine_levels(db, "staff:admin", machine(db, MM2), set())  # staff-run
    assert err(db, w.alex, w.a_alex, MM2, at(1, 14)).code == "staff_only"
    row = db.scalar(select(AuditLog).where(AuditLog.action == "machine.access").order_by(AuditLog.id.desc()))
    assert row.actor == "staff:admin" and row.after == {"groups": []}


def test_approved_only_machine_is_possible_too(db, w):
    access.set_machine_levels(db, "staff:admin", machine(db, MM1), {"approved"})
    book(db, w.sam, w.a_sam, MM1, at(1, 10))
    assert err(db, w.alex, w.a_alex, MM1, at(1, 12)).code == "needs_more_access"


def test_matrix_page_saves_and_only_admins_edit(db, w):
    db.commit()
    adm, sup = login("admin"), login("sam")
    try:
        page = adm.get("/admin/access").text
        assert "Which machines can each group use?" in page and "Haas VF-3" in page
        data = {"csrf_token": adm.tok}
        for m in db.scalars(select(Machine).where(Machine.active.is_(True))):
            data[f"s_{m.id}"] = "on"  # everything: super approved
            if m.name in (MM1, MM2, VF3):
                data[f"a_{m.id}"] = "on"  # approved group: mills only
        assert adm.post("/admin/access/machines", data=data, follow_redirects=False).status_code == 303
        db.expire_all()
        assert access.machine_levels(machine(db, VF3)) == {"approved", "super_approved"}
        assert access.machine_levels(machine(db, ST10)) == {"super_approved"}
        assert access.machine_levels(machine(db, FC50)) == {"super_approved"}
        assert sup.post("/admin/access/machines", data={"csrf_token": sup.tok}, follow_redirects=False).status_code == 403
        assert "disabled" in sup.get("/admin/access").text  # supervisors can look, not change
    finally:
        adm.__exit__(None, None, None)
        sup.__exit__(None, None, None)


def test_editing_a_machine_keeps_its_matrix_unless_the_simple_choice_changes(db, w):
    access.set_machine_levels(db, "staff:admin", machine(db, MM1), {"approved"})
    db.commit()  # the HTTP app uses its own connection (Postgres would otherwise wait on our row locks)
    adm = login("admin")
    try:
        m = machine(db, MM1)
        adm.post("/admin/machines/save", data={"csrf_token": adm.tok, "id": str(m.id), "name": "Haas Mini Mill 1 (renamed)",
                                               "access_level": m.access_level, "active": "on"}, follow_redirects=False)
        db.expire_all()
        m = machine(db, "Haas Mini Mill 1 (renamed)")
        assert access.machine_levels(m) == {"approved"}  # name change did not reset the matrix
        adm.post("/admin/machines/save", data={"csrf_token": adm.tok, "id": str(m.id), "name": m.name,
                                               "access_level": "staff_only", "active": "on"}, follow_redirects=False)
        db.expire_all()
        assert access.is_staff_run(machine(db, "Haas Mini Mill 1 (renamed)"))
    finally:
        adm.__exit__(None, None, None)


def test_new_machine_defaults_to_both_groups(db, w):
    from app.services import machines as ms
    m = ms.save_machine(db, "staff:admin", None, {"name": "Band Saw", "requires_certification": False})
    assert access.machine_levels(m) == {"approved", "super_approved"}


def test_no_open_times_always_says_why_even_when_no_supervisor_is_scheduled(db, w):
    from app.models import ShiftSchedule
    db.query(ShiftSchedule).delete()
    db.commit()
    day = clock.to_local(at(1, 10)).date()
    res = rs.slots_for(db, w.alex, w.a_alex.number, machine(db, VF3).id, day)
    assert res["slots"] == [] and "supervisor" in res["error"].lower() and "Supervisor shifts" in res["error"]
    sup = login("sam")
    try:
        assert "No supervisor shifts are scheduled" in sup.get("/admin").text
    finally:
        sup.__exit__(None, None, None)
