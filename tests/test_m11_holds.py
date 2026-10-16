"""Setup/tools holds, mandatory phone, 8 AM - 10 PM hours."""
from datetime import date, timedelta
from html import unescape

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import clock, emailer, security
from app.errors import UserError
from app.main import create_app
from app.models import Approval, AuditLog, MachineHold, ShopSession
from app.services import contact, holds, lifecycle, reservations as rs, rotcode, sessions, shop, status
from app.services import settings as shop_settings
from tests.factories import at, book, certify, machine, make_approval, student
from tests.helpers import staff_login, verify_student

MM1, MM2 = "Haas Mini Mill 1", "Haas Mini Mill 2"
CL = {"machine_cleaned": True, "tools_returned": True, "workstation_clean": True}
MON, TUE, WED, THU, FRI, NEXT_MON, NEXT_TUE, NEXT_WED = (date(2026, 3, d) for d in (2, 3, 4, 5, 6, 9, 10, 11))


@pytest.fixture
def w(db, demo):
    alex, jordan, sam = student(db, "alex"), student(db, "jordan"), student(db, "sam")
    certify(db, sam, group="CNC Mills")
    a_alex = db.scalar(select(Approval).where(Approval.student_id == alex.id))
    a_jordan = make_approval(db, jordan, [MM1, MM2], hours=50)
    a_sam = make_approval(db, sam, [MM1, MM2], hours=50)
    shop_settings.set_value(db, "max_active_per_group", 9)
    db.commit()
    return type("W", (), dict(alex=alex, jordan=jordan, sam=sam, a_alex=a_alex, a_jordan=a_jordan, a_sam=a_sam))


def code(db):
    c = rotcode.current(db)["code"]
    db.commit()
    return c


def declare(db, stu, name, kind, when=None, **kw):
    if when:
        clock.freeze(when)
    return holds.declare(db, stu, machine(db, name), kind, via="staff", actor="staff:sam", **kw)


# ============================================================ hours
def test_default_hours_are_8am_to_10pm_on_weekdays(db, demo):
    for d in (MON, TUE, WED, THU, FRI):
        o, c = shop.hours_for_date(db, d)
        assert clock.to_local(o).hour == 8 and clock.to_local(c).hour == 22
    assert shop.hours_for_date(db, date(2026, 3, 7)) is None


def test_fresh_database_defaults_to_8_to_22(db):
    from app.services import shopconfig
    shopconfig.ensure_base_data(db)
    o, c = shop.hours_for_date(db, WED)
    assert (clock.to_local(o).hour, clock.to_local(c).hour) == (8, 22)


def test_can_book_a_late_evening_slot(db, w):
    r = book(db, w.alex, w.a_alex, MM1, at(1, 21))
    assert clock.to_local(r.end_at).hour == 22
    with pytest.raises(UserError) as e:
        book(db, w.alex, w.a_alex, MM2, at(1, 22))
    assert e.value.code == "closed"


# ============================================================ mandatory phone
def test_phone_is_required_to_reserve(db, w):
    w.sam.phone, w.sam.phone_ack_at = None, None
    with pytest.raises(UserError) as e:
        book_no_waiver_flag(db, w.sam, w.a_sam)
    assert e.value.code == "phone_required" and "shop TV" in e.value.message
    w.sam.phone = "+15305550103"  # number but no acknowledgement
    with pytest.raises(UserError) as e:
        book_no_waiver_flag(db, w.sam, w.a_sam)
    assert e.value.code == "phone_required"
    w.sam.phone_ack_at = clock.now()
    book_no_waiver_flag(db, w.sam, w.a_sam)


def book_no_waiver_flag(db, stu, a):
    return rs.create(db, stu, a.number, machine(db, MM1).id, at(1, 10), at(1, 11), waiver_accepted=True)


def test_slot_list_flags_missing_contact_instead_of_hiding_slots(db, w):
    w.sam.phone, w.sam.phone_ack_at = None, None
    res = rs.slots_for(db, w.sam, w.a_sam.number, machine(db, MM1).id, clock.to_local(at(1, 10)).date())
    assert len(res["slots"]) == 14 and res["needs_contact"] is True


def test_reserve_form_asks_for_phone_and_saves_it(client, db, w):
    w.sam.phone, w.sam.phone_ack_at = None, None
    db.commit()
    tok = verify_student(client, w.sam.email, "Sam Patel", "sophomore")
    base = {"csrf_token": tok, "approval": w.a_sam.number, "machine_id": str(machine(db, MM1).id),
            "start": at(1, 10).isoformat(), "nslots": "1", "action": "book"}
    r = client.post("/reserve", data={**base, "form_token": security.new_form_token()})
    assert r.status_code == 400 and 'name="phone"' in r.text and "shop TV" in unescape(r.text)
    r = client.post("/reserve", data={**base, "form_token": security.new_form_token(), "phone": "(530) 555-0144",
                                      "ack_phone": "on"}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert student(db, "sam").phone == "+15305550144"
    day = clock.to_local(at(1, 10)).date()
    page = client.get(f"/reserve?approval={w.a_sam.number}&machine={machine(db, MM1).id}&date={day}").text
    assert 'name="phone"' not in page  # already on file: no more prompting


# ============================================================ holds: rules
def test_setup_hold_cap_counts_open_days_only(db, w):
    h = declare(db, w.alex, MM1, "setup", at(FRI, 21))  # Fri = day 1, weekend closed, Mon = 2, Tue = 3
    assert h.expires_at == at(NEXT_TUE, 22)
    t = declare(db, w.jordan, MM2, "tools", at(FRI, 21))  # tools default cap: 1 open day
    assert t.expires_at == at(FRI, 22)


def test_cap_settings_are_configurable(db, w):
    shop_settings.set_value(db, "setup_hold_max_business_days", 5)
    h = declare(db, w.alex, MM1, "setup", at(MON, 20))
    assert h.expires_at == at(FRI, 22)


def test_holiday_does_not_count_as_a_business_day(db, w):
    from app.models import ShopHours
    db.add(ShopHours(location_id=1, on_date=TUE, closed=True, label="Holiday"))
    db.flush()
    h = declare(db, w.alex, MM1, "setup", at(MON, 20))  # Mon=1, Tue closed, Wed=2, Thu=3
    assert h.expires_at == at(THU, 22)


def test_hold_blocks_other_students_but_not_the_owner(db, w):
    declare(db, w.alex, MM1, "setup", at(0, 20))
    with pytest.raises(UserError) as e:
        book(db, w.sam, w.a_sam, MM1, at(1, 10))
    assert e.value.code == "machine_on_hold" and "Alex R." in e.value.message and "setup" in e.value.message and "don't touch" in e.value.message
    book(db, w.sam, w.a_sam, MM2, at(1, 10))  # other machines unaffected
    book(db, w.alex, w.a_alex, MM1, at(1, 10))  # the owner may use their own setup
    # a slot after the hold ends is fine
    book(db, w.sam, w.a_sam, MM1, at(5, 10))  # Monday, after the hold ends Friday night


def test_hold_status_phone_and_engine(db, w):
    h = declare(db, w.alex, MM1, "tools", at(0, 20))
    st = status.machine_state(db, machine(db, MM1))
    assert st.status == "on_hold" and st.label == "Do Not Touch" and st.hold_kind == "tools"
    assert st.current_user == "Alex R." and st.current_phone == "+15305550101" and st.hold_until == h.expires_at


def test_hold_blocks_check_in_by_someone_booked_earlier(db, w):
    r = book(db, w.sam, w.a_sam, MM1, at(1, 9))
    emailer.SENT.clear()
    declare(db, w.alex, MM1, "setup", at(0, 20))
    emailer.flush_outbox(db)
    assert any(m["to"] == w.sam.email and "Heads up" in m["subject"] and "(please don't touch it)" in m["text"] for m in emailer.SENT)
    clock.freeze(at(1, 8, 50))
    with pytest.raises(UserError) as e:
        sessions.check_in(db, w.sam, r.ref, code(db), "1.1.1.1")
    assert e.value.code == "machine_on_hold" and "Call them" in e.value.message


def test_only_one_hold_per_machine_and_owner_can_change_kind(db, w):
    h = declare(db, w.alex, MM1, "tools", at(FRI, 21))
    with pytest.raises(UserError) as e:
        declare(db, w.jordan, MM1, "setup")
    assert e.value.code == "already_held"
    h2 = declare(db, w.alex, MM1, "setup")  # upgrade tools -> setup: same hold, longer cap from the original start
    assert h2.id == h.id and h2.kind == "setup" and h2.expires_at == at(NEXT_TUE, 22)


def test_cannot_hold_a_machine_you_have_not_used_recently(db, w):
    with pytest.raises(UserError) as e:
        holds.declare(db, w.sam, machine(db, MM1), "tools", via="my")
    assert e.value.code == "no_recent_use"


def test_release_and_staff_extend(db, w):
    h = declare(db, w.alex, MM1, "setup", at(MON, 20))  # until Wed 10 PM
    assert h.expires_at == at(WED, 22)
    holds.extend(db, h, "staff:sam", 2)
    assert h.expires_at == at(FRI, 22)  # two more open days
    holds.release(db, h, "student:a", "owner")
    assert holds.active_hold(db, machine(db, MM1).id) is None
    assert status.machine_state(db, machine(db, MM1)).status == "available"
    with pytest.raises(UserError):
        holds.release(db, h, "student:a", "owner")
    book(db, w.sam, w.a_sam, MM1, at(THU, 10))


# ============================================================ checkout + closing prompts
def run_session(db, w, stu, a, hour=11, minutes_in=5, hours=1):
    r = book(db, stu, a, MM1, at(0, hour), hours)
    db.commit()
    clock.freeze(at(0, hour, minutes_in))
    s = sessions.check_in(db, stu, r.ref, code(db), "1.1.1.1")
    db.commit()
    return r, s


def test_checkout_requires_a_leave_state_choice(db, w):
    run_session(db, w, w.alex, w.a_alex)
    with pytest.raises(UserError) as e:
        sessions.check_out(db, w.alex, None, code(db), CL, "1.1.1.1", leave_state="maybe")
    assert e.value.code == "leave_state"


@pytest.mark.parametrize("state", ["tools", "setup"])
def test_checkout_leaving_tools_or_setup_creates_a_hold(db, w, state):
    run_session(db, w, w.alex, w.a_alex)
    clock.freeze(at(0, 11, 50))
    sessions.check_out(db, w.alex, None, code(db), CL, "1.1.1.1", leave_state=state)
    h = holds.active_hold(db, machine(db, MM1).id)
    assert h and h.kind == state and h.student_id == w.alex.id and h.declared_via == "checkout"
    assert status.machine_state(db, machine(db, MM1)).status == "on_hold"


def test_checkout_clear_releases_your_own_hold(db, w):
    declare(db, w.alex, MM1, "tools", at(0, 8))
    run_session(db, w, w.alex, w.a_alex)
    clock.freeze(at(0, 11, 50))
    sessions.check_out(db, w.alex, None, code(db), CL, "1.1.1.1", leave_state="clear")
    assert holds.active_hold(db, machine(db, MM1).id) is None


def test_early_checkout_with_a_hold_does_not_invite_the_next_student_in(db, w):
    nxt = book(db, w.sam, w.a_sam, MM1, at(0, 13))
    run_session(db, w, w.alex, w.a_alex, hour=11, hours=2)
    clock.freeze(at(0, 11, 30))
    emailer.SENT.clear()
    sessions.check_out(db, w.alex, None, code(db), CL, "1.1.1.1", leave_state="setup")
    emailer.flush_outbox(db)
    assert not [m for m in emailer.SENT if "free right now" in m["subject"]]
    assert nxt.status == "confirmed"


def test_closing_prompt_goes_once_to_people_still_on_a_machine(db, w):
    run_session(db, w, w.alex, w.a_alex, hour=20, hours=2)
    emailer.SENT.clear()
    clock.freeze(at(0, 21, 20))
    assert holds.closing_prompts(db) == 0  # 40 minutes before close: too early
    clock.freeze(at(0, 21, 35))
    assert lifecycle.sweep(db)["closing_prompts"] == 1
    assert lifecycle.sweep(db)["closing_prompts"] == 0  # once per session
    emailer.flush_outbox(db)
    mail = next(m for m in emailer.SENT if "Closing soon" in m["subject"])
    assert mail["to"] == w.alex.email and "/hold/session/" in mail["text"] and "do not touch" in mail["text"]


def test_one_tap_link_from_the_email_sets_the_hold_without_login(client, db, w):
    r, s = run_session(db, w, w.alex, w.a_alex, hour=20, hours=2)
    clock.freeze(at(0, 21, 40))
    link = __import__("app.services.notify", fromlist=["x"]).session_link(s.id).replace("http://localhost:8000", "")
    page = client.get(link)
    assert page.status_code == 200 and "Anything staying" in page.text and "My setup is in it" in page.text
    tok = client.cookies.get("csrf")
    t = link.split("t=")[1]
    assert client.post(f"/hold/session/{s.id}", data={"csrf_token": tok, "t": "forged", "kind": "setup"}).status_code == 404
    ok = client.post(f"/hold/session/{s.id}", data={"csrf_token": tok, "t": t, "kind": "setup"})
    assert ok.status_code == 200 and "not to be touched" in ok.text
    h = holds.active_hold(db, machine(db, MM1).id)
    assert h and h.kind == "setup" and h.declared_via == "closing"
    # and a second tap to clear it
    client.get(link)
    tok = client.cookies.get("csrf")
    assert client.post(f"/hold/session/{s.id}", data={"csrf_token": tok, "t": t, "kind": "clear"}).status_code == 200
    db.expire_all()
    assert holds.active_hold(db, machine(db, MM1).id) is None


# ============================================================ reminders + expiry
def test_daily_reminder_once_per_open_day_then_final_day_then_expiry(db, w):
    declare(db, w.alex, MM1, "setup", at(NEXT_MON, 21))  # Mon 1, Tue 2, Wed 3 -> expires Wed 10 PM
    emailer.SENT.clear()
    clock.freeze(at(NEXT_MON, 21, 30))
    assert holds.daily_reminders(db) == 0  # same day it was set
    clock.freeze(at(NEXT_TUE, 7, 0))
    assert holds.daily_reminders(db) == 0  # shop not open yet
    clock.freeze(at(NEXT_TUE, 9, 0))
    assert holds.daily_reminders(db) == 1 and holds.daily_reminders(db) == 0
    clock.freeze(at(NEXT_WED, 9, 0))
    assert holds.daily_reminders(db) == 1
    emailer.flush_outbox(db)
    subjects = [m["subject"] for m in emailer.SENT if m["to"] == w.alex.email]
    assert any("still in the" in s and "(last day)" not in s for s in subjects) and any("(last day)" in s for s in subjects)
    text = next(m["text"] for m in emailer.SENT if "(last day)" in m["subject"])
    assert "day 3 of 3" in text and "/hold/" in text
    # expiry at closing time of the last day: released, owner + staff told, flagged on the dashboard
    emailer.SENT.clear()
    clock.freeze(at(NEXT_WED, 22, 1))
    assert lifecycle.sweep(db)["holds_expired"] == 1
    emailer.flush_outbox(db)
    assert {m["to"] for m in emailer.SENT} >= {w.alex.email, "shop@csuchico.edu"}
    h = db.scalar(select(MachineHold))
    assert h.released_at and h.release_reason == "expired"
    assert status.machine_state(db, machine(db, MM1)).status == "available"


def test_reminder_link_lets_the_owner_clear_it_in_one_tap(client, db, w):
    h = declare(db, w.alex, MM1, "tools", at(NEXT_MON, 21))
    db.commit()  # the HTTP request uses its own connection on Postgres
    link = __import__("app.services.notify", fromlist=["x"]).hold_link(h.id).replace("http://localhost:8000", "")
    assert "marked: do not touch" in client.get(link).text.lower()
    tok = client.cookies.get("csrf")
    t = link.split("t=")[1]
    assert client.post(f"/hold/{h.id}/release", data={"csrf_token": tok, "t": "bad"}).status_code == 404
    r = client.post(f"/hold/{h.id}/release", data={"csrf_token": tok, "t": t})
    assert r.status_code == 200 and "marked clear" in r.text
    db.expire_all()
    assert db.get(MachineHold, h.id).released_at and db.get(MachineHold, h.id).release_reason == "owner"


def test_my_shop_lists_holds_and_lets_you_mark_a_recent_machine(client, db, w):
    r, s = run_session(db, w, w.alex, w.a_alex)
    clock.freeze(at(0, 11, 50))
    sessions.check_out(db, w.alex, None, code(db), CL, "1.1.1.1", leave_state="clear")
    db.commit()
    tok = verify_student(client, w.alex.email, "Alex Rivera")
    page = client.get("/my").text
    assert "Leaving something in a machine you just used?" in page and "Haas Mini Mill 1" in page
    assert client.post("/my/hold", data={"csrf_token": tok, "machine_id": str(machine(db, MM1).id), "kind": "tools"},
                       follow_redirects=False).status_code == 303
    page = client.get("/my").text
    assert "marked do-not-touch" in page and "I've cleared it out" in page
    other = client.post("/my/hold", data={"csrf_token": tok, "machine_id": str(machine(db, MM2).id), "kind": "tools"},
                        follow_redirects=False)
    assert other.status_code == 303 and "recently" in unescape(other.headers["location"]).replace("+", " ")


# ============================================================ schedule + TV + staff
def test_schedule_and_tv_show_the_hold_with_a_phone_number(client, db, w):
    declare(db, w.alex, MM1, "setup", at(0, 20))
    db.commit()
    verify_student(client, w.sam.email, "Sam Patel", "sophomore")
    page = client.get(f"/machine/{machine(db, MM1).id}").text
    assert "Do not touch" in page and "Alex R." in page and "(530) 555-0101" in page and "tel:+15305550101" in page
    m = next(x for x in client.get("/tv/data").json()["machines"] if x["name"] == MM1)
    assert m["status"] == "on_hold" and m["label"] == "Do Not Touch" and m["user"] == "Alex R."
    assert m["user_phone"] == "(530) 555-0101" and m["hold_kind"] == "setup" and m["hold_until"]
    assert m["timeline"][0]["k"] == "hold"


def test_open_ended_machine_shows_on_tv_without_overdue(client, db, w):
    r, s = run_session(db, w, w.alex, w.a_alex)
    clock.freeze(at(0, 12, 30))  # booked until 12:00, nobody next
    m = next(x for x in client.get("/tv/data").json()["machines"] if x["name"] == MM1)
    assert m["status"] == "in_use" and m["open_ended"] and "keep going" in (m["reason"] or "")
    assert m["remaining_minutes"] == 570 and m["available_at"] == "10:00 PM"


def test_staff_can_place_extend_and_release_holds(db, w):
    c = TestClient(create_app(), base_url="http://testserver")
    with c:
        tok = staff_login(c, "sam")
        post = lambda p, **d: c.post(p, data={"csrf_token": tok, **d}, follow_redirects=False)  # noqa: E731
        mid = machine(db, MM1).id
        assert post(f"/admin/hold-place/{mid}", email=w.alex.email, kind="setup").status_code == 303
        h = db.scalar(select(MachineHold))
        assert h and h.declared_via == "staff"
        assert "Setup hold" in c.get(f"/admin/machines/{mid}").text and "Setups and tools left" in c.get("/admin").text
        before = h.expires_at
        assert post(f"/admin/holds/{h.id}/extend", days="1").status_code == 303
        db.expire_all()
        assert db.get(MachineHold, h.id).expires_at > before
        assert post(f"/admin/holds/{h.id}/release").status_code == 303
        db.expire_all()
        assert db.get(MachineHold, h.id).release_reason == "staff"
        acts = {a.action for a in db.scalars(select(AuditLog))}
        assert {"hold.declare", "hold.extend", "hold.release"} <= acts


def test_checkout_form_over_http_has_the_leave_choices_and_requires_one(client, db, w):
    r, s = run_session(db, w, w.alex, w.a_alex)
    tok = verify_student(client, w.alex.email, "Alex Rivera")
    clock.freeze(at(0, 11, 40))
    page = client.get("/checkout").text
    for label in ("the machine is clear", "Only my tools are in it", "My setup is in it"):
        assert label in unescape(page)
    base = {"csrf_token": tok, "form_token": security.new_form_token(), "shop_code": code(db), "machine_cleaned": "on",
            "tools_returned": "on", "workstation_clean": "on"}
    missing = client.post("/checkout", data=base)
    assert missing.status_code == 400 and "whether anything is staying" in unescape(missing.text)
    ok = client.post("/checkout", data={**base, "form_token": security.new_form_token(), "leave_state": "setup"},
                     follow_redirects=False)
    assert ok.status_code == 303
    assert holds.active_hold(db, machine(db, MM1).id).kind == "setup"
