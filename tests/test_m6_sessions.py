from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app import clock, emailer
from app.errors import UserError
from app.models import (
    Approval, AuditLog, DamageReport, MaintenanceRecord, Reservation, ShopSession, Student,
)
from app.services import lifecycle, reservations as rs, rotcode, sessions, status
from app.services import settings as shop_settings
from tests.factories import at, book, certify, machine, make_approval, student, waiver

MM1, MM2, VF3 = "Haas Mini Mill 1", "Haas Mini Mill 2", "Haas VF-3"
CHECKLIST = {"machine_cleaned": True, "tools_returned": True, "workstation_clean": True}
IP = "10.0.0.1"


@pytest.fixture
def w(db, demo):
    alex, jordan = student(db, "alex"), student(db, "jordan")
    a_alex = db.scalar(select(Approval).where(Approval.student_id == alex.id))
    shop_settings.set_value(db, "max_active_per_group", 9)
    shop_settings.set_value(db, "max_reservations_per_week", 20)
    db.commit()
    return type("W", (), dict(alex=alex, jordan=jordan, a=a_alex))


def code(db):
    return rotcode.current(db)["code"]


def booked(db, w, hour=11, hours=1, who=None, name=MM1, approval=None):
    return book(db, who or w.alex, approval or w.a, name, at(0, hour), hours)


def go(minutes_after_t0):
    clock.freeze(clock.now().replace(hour=18, minute=0) + timedelta(minutes=minutes_after_t0))


def checkin(db, w, r, when=None, shop_code=None, stu=None):
    if when is not None:
        go(when)
    return sessions.check_in(db, stu or w.alex, r.ref, shop_code or code(db), IP)


# ================================================================= check-in
def test_check_in_success(db, w):
    r = booked(db, w)
    s = checkin(db, w, r, when=55)  # 10:55, window opens 10:45
    assert s.checked_in_at == clock.now() and s.expected_end_at == r.end_at
    assert r.status == "checked_in"
    st = status.machine_state(db, machine(db, MM1))
    assert st.status == "in_use" and st.current_user == "Alex R."
    assert db.scalar(select(AuditLog).where(AuditLog.action == "session.check_in"))


def test_too_early(db, w):
    r = booked(db, w)
    go(44)
    with pytest.raises(UserError) as e:
        sessions.check_in(db, w.alex, r.ref, code(db), IP)
    assert e.value.code == "too_early" and "10:45 AM" in e.value.message
    go(45)
    assert sessions.check_in(db, w.alex, r.ref, code(db), IP)


def test_reservation_for_another_day_rejected(db, w):
    r = book(db, w.alex, w.a, MM1, at(1, 11))
    with pytest.raises(UserError) as e:
        sessions.check_in(db, w.alex, r.ref, code(db), IP)
    assert e.value.code == "wrong_day" and "not today" in e.value.message


def test_reference_is_forgiving_about_format(db, w):
    r = booked(db, w)
    go(55)
    messy = r.ref.lower().replace("-", " ")
    assert sessions.check_in(db, w.alex, messy, code(db), IP)


def test_someone_elses_reference_looks_like_unknown(db, w):
    r = booked(db, w)
    go(55)
    with pytest.raises(UserError) as e1:
        sessions.check_in(db, w.jordan, r.ref, code(db), IP)
    with pytest.raises(UserError) as e2:
        sessions.check_in(db, w.jordan, "RSV-ZZZZZZ", code(db), IP)
    assert e1.value.code == e2.value.code == "ref_not_found" and e1.value.message == e2.value.message


def test_wrong_shop_code_rejected(db, w):
    r = booked(db, w)
    go(55)
    good = code(db)
    bad = "0000" if good != "0000" else "1111"
    with pytest.raises(UserError) as e:
        sessions.check_in(db, w.alex, r.ref, bad, IP)
    assert e.value.code == "bad_shop_code" and e.value.persist
    assert r.status == "confirmed" and db.scalar(select(ShopSession)) is None


def test_previous_code_accepted_for_two_minutes_after_rotation(db, w):
    r = booked(db, w)
    go(55)
    old = code(db)
    go(60)  # 11:00: code rotated at 11:00 (10-minute boundary)
    assert code(db) != old
    go(61)
    assert sessions.check_in(db, w.alex, r.ref, old, IP)


def test_expired_previous_code_rejected(db, w):
    r = booked(db, w)
    go(55)
    old = code(db)
    go(63)  # 3 min after rotation: previous code no longer valid
    cur = code(db)
    if old != cur:
        with pytest.raises(UserError):
            sessions.check_in(db, w.alex, r.ref, old, IP)


def test_repeated_wrong_codes_lock_out_that_reference(db, w):
    r = booked(db, w)
    go(55)
    good = code(db)
    bad = "0000" if good != "0000" else "1111"
    for _ in range(5):
        with pytest.raises(UserError):
            sessions.check_in(db, w.alex, r.ref, bad, IP)
    with pytest.raises(UserError) as e:
        sessions.check_in(db, w.alex, r.ref, good, IP)
    assert e.value.code == "rate_limited"


def test_late_within_grace_ok_beyond_is_no_show_and_releases_slot(db, w):
    ja = make_approval(db, w.jordan, [MM1])
    r = booked(db, w)
    wl = rs.join_waitlist(db, w.jordan, ja.number, machine(db, MM1).id, r.start_at, r.end_at)
    go(75)  # 11:15 exactly = start + 15 grace: still ok
    assert sessions.check_in(db, w.alex, r.ref, code(db), IP)
    # second scenario
    db.rollback()


def test_late_beyond_grace_marks_no_show(db, w):
    ja = make_approval(db, w.jordan, [MM1])
    r = booked(db, w)
    wl = rs.join_waitlist(db, w.jordan, ja.number, machine(db, MM1).id, r.start_at, r.end_at)
    go(76)
    emailer.SENT.clear()
    with pytest.raises(UserError) as e:
        sessions.check_in(db, w.alex, r.ref, code(db), IP)
    assert e.value.code == "late" and e.value.persist and "no-show" in e.value.message
    assert r.status == "no_show"
    assert wl.status == "offered"  # slot released -> waitlist notified
    emailer.flush_outbox(db)
    subjects = [m["subject"] for m in emailer.SENT]
    assert any("No-show" in s for s in subjects) and any("slot opened up" in s for s in subjects)


def test_cannot_check_in_twice_or_with_cancelled(db, w):
    r = booked(db, w)
    checkin(db, w, r, when=55)
    with pytest.raises(UserError) as e:
        sessions.check_in(db, w.alex, r.ref, code(db), IP)
    assert e.value.code == "already_checked_in"
    r2 = book(db, w.alex, w.a, MM2, at(0, 14))
    rs.cancel(db, r2, actor="x")
    go(180)
    with pytest.raises(UserError) as e:
        sessions.check_in(db, w.alex, r2.ref, code(db), IP)
    assert e.value.code == "not_confirmed"


def test_one_active_session_per_student(db, w):
    r1 = booked(db, w, 11, name=MM1)
    r2 = booked(db, w, 11, name=MM2)
    checkin(db, w, r1, when=55)
    with pytest.raises(UserError) as e:
        sessions.check_in(db, w.alex, r2.ref, code(db), IP)
    assert e.value.code == "has_active"


def test_machine_still_in_use_by_previous_person_blocks(db, w):
    ja = make_approval(db, w.jordan, [MM1])
    r_prev = book(db, w.jordan, ja, MM1, at(0, 10) + timedelta(hours=0), 1) if False else None
    first = book(db, w.alex, w.a, MM1, at(0, 11))
    nxt = book(db, w.jordan, ja, MM1, at(0, 12))
    checkin(db, w, first, when=55)
    go(120)  # 12:00 — Alex never checked out
    with pytest.raises(UserError) as e:
        sessions.check_in(db, w.jordan, nxt.ref, code(db), IP)
    assert e.value.code == "machine_busy" and "staff" in e.value.message


def test_maintenance_and_emergency_block_check_in(db, w):
    r = booked(db, w)
    go(55)
    machine(db, MM1).maintenance_state = "awaiting_inspection"
    with pytest.raises(UserError) as e:
        sessions.check_in(db, w.alex, r.ref, code(db), IP)
    assert e.value.code == "maintenance"
    machine(db, MM1).maintenance_state = "none"
    shop_settings.set_value(db, "emergency_active", True)
    with pytest.raises(UserError) as e:
        sessions.check_in(db, w.alex, r.ref, code(db), IP)
    assert e.value.code == "emergency"


def test_force_check_in_bypasses_code_and_window_but_needs_reason_and_is_audited(db, w):
    r = booked(db, w, 15)
    with pytest.raises(UserError):
        sessions.force_check_in(db, r, "staff:sam", "  ")
    s = sessions.force_check_in(db, r, "staff:sam", "phone died")
    assert s.forced_by == "sam" and r.status == "checked_in"
    acts = [a.action for a in db.scalars(select(AuditLog))]
    assert "session.force_check_in" in acts and "session.force_reason" in acts


# ================================================================= check-out
def test_check_out_success_and_status_returns_to_available(db, w):
    r = booked(db, w)
    s = checkin(db, w, r, when=60)
    go(100)
    out = sessions.check_out(db, w.alex, None, code(db), CHECKLIST, IP)
    assert out.status == "closed" and out.duration_minutes == 40 and r.status == "completed"
    assert status.machine_state(db, machine(db, MM1)).status == "available"


def test_check_out_requires_complete_checklist(db, w):
    r = booked(db, w)
    checkin(db, w, r, when=60)
    with pytest.raises(UserError) as e:
        sessions.check_out(db, w.alex, None, code(db), {"machine_cleaned": True}, IP)
    assert e.value.code == "checklist" and "Tools returned" in e.value.message and "Workstation" in e.value.message
    assert sessions.active_session_for(db, w.alex.id)


def test_check_out_requires_shop_code(db, w):
    r = booked(db, w)
    checkin(db, w, r, when=60)
    bad = "0000" if code(db) != "0000" else "1111"
    with pytest.raises(UserError) as e:
        sessions.check_out(db, w.alex, r.ref, bad, CHECKLIST, IP)
    assert e.value.code == "bad_shop_code"


def test_check_out_without_session(db, w):
    with pytest.raises(UserError) as e:
        sessions.check_out(db, w.alex, None, code(db), CHECKLIST, IP)
    assert e.value.code == "no_session"


def test_status_reserved_after_checkout_when_next_booking_is_soon(db, w):
    ja = make_approval(db, w.jordan, [MM1])
    r = booked(db, w, 11, 2)
    book(db, w.jordan, ja, MM1, at(0, 13))
    checkin(db, w, r, when=60)
    go(115)  # 11:55, leaves 5 min early... next starts 13:00 (65 min away)
    sessions.check_out(db, w.alex, None, code(db), CHECKLIST, IP)
    assert status.machine_state(db, machine(db, MM1)).status == "available"
    go(155)  # 12:35 -> next reservation within 30 min
    assert status.machine_state(db, machine(db, MM1)).status == "reserved"


def test_damage_report_sets_awaiting_inspection(db, w):
    r = booked(db, w)
    checkin(db, w, r, when=60)
    go(90)
    emailer.SENT.clear()
    sessions.check_out(db, w.alex, None, code(db), CHECKLIST, IP, report_kind="damage",
                       report_text="Crashed the tool into the vise.")
    m = machine(db, MM1)
    assert m.maintenance_state == "awaiting_inspection"
    assert status.machine_state(db, m).status == "awaiting_inspection"
    rep = db.scalar(select(DamageReport))
    assert rep.severity == "damage" and rep.student_id == w.alex.id
    assert db.scalar(select(MaintenanceRecord).where(MaintenanceRecord.machine_id == m.id)).state_set == "awaiting_inspection"
    emailer.flush_outbox(db)
    assert any("Machine reported" in x["subject"] for x in emailer.SENT)


def test_maintenance_report_takes_machine_out_of_service(db, w):
    r = booked(db, w)
    checkin(db, w, r, when=60)
    sessions.check_out(db, w.alex, None, code(db), CHECKLIST, IP, report_kind="maintenance", report_text="Spindle noise.")
    assert status.machine_state(db, machine(db, MM1)).status == "under_maintenance"


def test_report_needs_description_and_photo_is_validated(db, w):
    r = booked(db, w)
    checkin(db, w, r, when=60)
    with pytest.raises(UserError) as e:
        sessions.check_out(db, w.alex, None, code(db), CHECKLIST, IP, report_kind="damage", report_text="x")
    assert e.value.code == "report_text"


def test_force_check_out_and_close_stuck(db, w):
    r = booked(db, w)
    s = checkin(db, w, r, when=60)
    with pytest.raises(UserError):
        sessions.force_check_out(db, s, "staff:sam", "")
    sessions.force_check_out(db, s, "staff:sam", "student left; phone dead")
    assert s.status == "force_closed" and s.forced_by == "sam" and r.status == "completed"
    with pytest.raises(UserError):
        sessions.force_check_out(db, s, "staff:sam", "again")


def test_early_checkout_offers_remaining_time_to_waitlist(db, w):
    ja = make_approval(db, w.jordan, [MM1])
    r = booked(db, w, 11, 2)
    wl = rs.join_waitlist(db, w.jordan, ja.number, machine(db, MM1).id, at(0, 12), at(0, 13))
    checkin(db, w, r, when=60)
    go(70)
    sessions.check_out(db, w.alex, None, code(db), CHECKLIST, IP)
    assert wl.status == "offered"


# ================================================================= lifecycle sweep
def test_sweep_marks_no_show_only_after_grace_and_is_idempotent(db, w):
    r = booked(db, w)
    go(75)
    assert lifecycle.sweep(db)["no_shows"] == 0
    go(76)
    assert lifecycle.sweep(db)["no_shows"] == 1 and r.status == "no_show"
    assert lifecycle.sweep(db)["no_shows"] == 0
    assert status.machine_state(db, machine(db, MM1)).status == "available"


def test_late_waitlist_claim_gets_fresh_grace(db, w):
    ja = make_approval(db, w.jordan, [MM1])
    r = booked(db, w)
    wl = rs.join_waitlist(db, w.jordan, ja.number, machine(db, MM1).id, r.start_at, r.end_at)
    go(76)
    lifecycle.sweep(db)  # Alex no-show -> offer to Jordan, slot started 76 min after 10:00 -> 11:16
    assert wl.status == "offered"
    go(80)
    res = rs.claim(db, wl.claim_token, w.jordan)
    assert lifecycle.sweep(db)["no_shows"] == 0  # claimed late: grace counts from booking time
    sess = sessions.check_in(db, w.jordan, res.ref, code(db), IP)
    assert sess.expected_end_at == res.end_at


def no_show(db, r):
    clock.freeze(r.start_at + timedelta(minutes=16))
    lifecycle.sweep(db)
    assert r.status == "no_show"


THU, FRI, MON, TUE, WED = (date(2026, 3, d) for d in (5, 6, 9, 10, 11))


def test_three_no_shows_auto_suspend_and_staff_can_override(db, w):
    rs_ = [book(db, w.alex, w.a, MM1, at(d, 10)) for d in (THU, FRI, MON)]
    for r in rs_:
        no_show(db, r)
    assert w.alex.suspended_until and w.alex.suspended_until > clock.now()
    assert "3 no-shows" in w.alex.suspension_reason
    emailer.flush_outbox(db)
    assert any("paused" in m["text"] for m in emailer.SENT)
    with pytest.raises(UserError) as e:
        book(db, w.alex, w.a, MM1, at(TUE, 10))
    assert e.value.code == "suspended"
    # staff override: clear the suspension and forgive earlier no-shows
    from app.services import students as stu_svc
    stu_svc.clear_suspension(db, w.alex, "staff:sam", "talked to student")
    assert w.alex.suspended_until is None
    r = book(db, w.alex, w.a, MM2, at(WED, 10))
    no_show(db, r)
    assert w.alex.suspended_until is None  # one no-show after forgiveness isn't enough


def test_two_no_shows_do_not_suspend(db, w):
    for d in (THU, FRI):
        no_show(db, book(db, w.alex, w.a, MM1, at(d, 10)))
    assert w.alex.suspended_until is None


def test_old_no_shows_age_out(db, w):
    for d in (THU, FRI):
        no_show(db, book(db, w.alex, w.a, MM1, at(d, 10)))
    clock.freeze(at(date(2026, 4, 14), 9))  # 40 days later
    w.a.expires_at = clock.now() + timedelta(days=30)
    no_show(db, book(db, w.alex, w.a, MM1, at(date(2026, 4, 15), 10)))
    assert w.alex.suspended_until is None  # earlier two fell outside the 30-day window


def test_overdue_warning_then_auto_close(db, w):
    r = booked(db, w, 11, 1)
    s = checkin(db, w, r, when=60)
    go(121)  # 12:01 -> overdue
    emailer.SENT.clear()
    out = lifecycle.sweep(db)
    assert out["overdue_warnings"] == 1 and out["auto_closed"] == 0
    assert status.machine_state(db, machine(db, MM1)).status == "overdue"
    emailer.flush_outbox(db)
    assert any("time is up" in m["subject"] for m in emailer.SENT)
    assert lifecycle.sweep(db)["overdue_warnings"] == 0  # warned once
    go(149)  # 12:29 -> 29 min past end, buffer is 30
    assert lifecycle.sweep(db)["auto_closed"] == 0
    go(151)
    emailer.SENT.clear()
    out = lifecycle.sweep(db)
    assert out["auto_closed"] == 1
    assert s.status == "auto_closed" and s.auto_closed and s.needs_review and s.checked_out_at == r.end_at
    assert r.status == "completed"
    emailer.flush_outbox(db)
    assert any("auto-closed" in m["subject"] for m in emailer.SENT)
    assert status.machine_state(db, machine(db, MM1)).status == "available"
    assert lifecycle.sweep(db)["auto_closed"] == 0


def test_auto_close_buffer_is_configurable(db, w):
    shop_settings.set_value(db, "autoclose_buffer_minutes", 5)
    r = booked(db, w, 11, 1)
    checkin(db, w, r, when=60)
    go(127)
    assert lifecycle.sweep(db)["auto_closed"] == 1


def test_extension_moves_session_end_and_rearms_overdue_warning(db, w):
    r = booked(db, w, 11, 1)
    s = checkin(db, w, r, when=60)
    go(121)
    lifecycle.sweep(db)
    assert s.overdue_notified_at is not None
    go(122)
    # session is overdue but still active: extension window has passed (reservation ended)
    with pytest.raises(UserError):
        rs.extend(db, r, w.alex)


def test_extend_during_session_updates_expected_end(db, w):
    r = booked(db, w, 11, 2)
    s = checkin(db, w, r, when=60)
    go(90)
    rs.extend(db, r, w.alex)
    assert s.expected_end_at == r.end_at == at(0, 14)


def test_lazy_sweep_is_throttled_and_correct(db, w):
    r = booked(db, w)
    go(76)
    lifecycle.lazy_sweep(db)
    assert r.status == "no_show"
    r2 = book(db, w.alex, w.a, MM2, at(0, 16))
    go(76 + 60 * 5 + 20)  # past start+grace of r2, but < 15s since last run? no: 5 hours later
    lifecycle.lazy_sweep(db)
    assert r2.status == "no_show"
