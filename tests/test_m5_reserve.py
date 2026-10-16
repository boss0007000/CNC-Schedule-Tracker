import re
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app import clock, emailer
from app.errors import UserError
from app.models import (
    Approval, Certification, MaintenanceRecord, Reservation, SafetyWaiver, ShiftSchedule, ShopHours, WaitlistEntry,
)
from app.services import reservations as rs
from app.services import settings as shop_settings
from tests.factories import (
    at, book, certify, machine, make_approval, new_student, student, waiver,
)

MM1, MM2, VF3, ST10 = "Haas Mini Mill 1", "Haas Mini Mill 2", "Haas VF-3", "Haas ST-10"


@pytest.fixture
def world(db, demo):
    alex, jordan = student(db, "alex"), student(db, "jordan")
    a_alex = db.scalar(select(Approval).where(Approval.student_id == alex.id))
    a_jordan = db.scalar(select(Approval).where(Approval.student_id == jordan.id))
    db.commit()
    return type("W", (), dict(alex=alex, jordan=jordan, a_alex=a_alex, a_jordan=a_jordan))


def try_book(db, stu, a, name, start, hours=1, **kw):
    return book(db, stu, a, name, start, hours, **kw)


def code_of(db, stu, a, name, start, hours=1, **kw):
    with pytest.raises(UserError) as e:
        book(db, stu, a, name, start, hours, **kw)
    return e.value


# ------------------------------------------------------------- happy path
def test_successful_booking_issues_ref_and_email(db, world):
    emailer.SENT.clear()
    r = try_book(db, world.alex, world.a_alex, MM1, at(1, 10))
    emailer.flush_outbox(db)
    assert re.fullmatch(r"RSV-[2-9A-HJKMNP-Z]{6}", r.ref)
    mail = emailer.SENT[-1]
    assert mail["to"] == world.alex.email and r.ref in mail["text"] and "/reservation/" in mail["text"]
    assert r.status == "confirmed" and r.hours == 1


def test_multi_slot_booking(db, world):
    r = try_book(db, world.alex, world.a_alex, MM1, at(1, 10), hours=3)
    assert r.hours == 3
    shop_settings.set_value(db, "max_session_hours_advanced", 4)
    assert code_of(db, world.alex, world.a_alex, MM2, at(1, 14), hours=5).code == "too_long"


# ------------------------------------------------------------- 1. approval
def test_approval_not_found_and_not_yours_look_identical(db, world):
    e1 = code_of(db, world.alex, type("A", (), {"number": "APR-2026-9999"}), MM1, at(1, 10))
    e2 = code_of(db, world.alex, world.a_jordan, MM1, at(1, 10))
    assert e1.code == e2.code == "approval_not_found" and e1.message == e2.message


def test_approval_number_typing_is_forgiving(db, world):
    r = rs.create(db, world.alex, world.a_alex.number.lower().replace("-", " "), machine(db, MM1).id, at(1, 10),
                  at(1, 11), waiver_accepted=True)
    assert r.approval_id == world.a_alex.id


def test_revoked_approval_blocked(db, world):
    world.a_alex.status = "revoked"
    db.flush()
    assert code_of(db, world.alex, world.a_alex, MM1, at(1, 10)).code == "approval_revoked"


def test_expired_approval_blocked(db, world):
    world.a_alex.expires_at = at(1, 9)
    db.flush()
    e = code_of(db, world.alex, world.a_alex, MM1, at(1, 10))
    assert e.code == "approval_expired" and "before this time slot" in e.message


def test_hours_exhausted(db, world):
    world.a_alex.max_total_hours = 3
    shop_settings.set_value(db, "max_active_per_group", 9)
    try_book(db, world.alex, world.a_alex, MM1, at(1, 10), hours=2)
    try_book(db, world.alex, world.a_alex, MM1, at(1, 14), hours=1)
    e = code_of(db, world.alex, world.a_alex, MM1, at(2, 10))
    assert e.code == "approval_exhausted" and "3 total hours" in e.message


def test_sessions_exhausted(db, world):
    world.a_alex.max_sessions = 1
    shop_settings.set_value(db, "max_active_per_group", 9)
    try_book(db, world.alex, world.a_alex, MM1, at(1, 10))
    assert code_of(db, world.alex, world.a_alex, MM1, at(1, 12)).code == "approval_exhausted"


def test_cancelled_reservations_dont_count_toward_approval(db, world):
    world.a_alex.max_sessions = 1
    r = try_book(db, world.alex, world.a_alex, MM1, at(2, 10))
    rs.cancel(db, r, actor="student:x")
    try_book(db, world.alex, world.a_alex, MM1, at(2, 12))


# ------------------------------------------------------------- 2. coverage
def test_machine_not_covered_message_lists_covered(db, world):
    e = code_of(db, world.alex, world.a_alex, ST10, at(1, 10))
    assert e.code == "not_covered"
    assert e.message == ("Your approval doesn't cover the Haas ST-10. Covered machines: "
                         "Haas Mini Mill 1, Haas Mini Mill 2, Haas VF-3.")


# ------------------------------------------------------------- 3. certification (hard block)
def test_missing_certification_is_hard_block_even_with_waiver_accepted(db, world):
    sam = student(db, "sam")
    waiver(db, sam)
    a = make_approval(db, sam, [MM1])
    e = code_of(db, sam, a, MM1, at(1, 10))
    assert e.code == "cert_missing" and "current certification" in e.message


def test_expired_certification_blocks(db, world):
    sam = student(db, "sam")
    a = make_approval(db, sam, [MM1])
    certify(db, sam, group="CNC Mills", expires_days=-1)
    assert code_of(db, sam, a, MM1, at(1, 10)).code == "cert_expired"


def test_cert_expiring_before_slot_blocks(db, world):
    sam = student(db, "sam")
    a = make_approval(db, sam, [MM1])
    certify(db, sam, group="CNC Mills", expires_days=3)
    try_book(db, sam, a, MM1, at(2, 10))  # inside validity
    assert code_of(db, sam, a, MM1, at(6, 10)).code == "cert_expired"  # Mon, after expiry


def test_revoked_certification_blocks(db, world):
    for c in db.scalars(select(Certification).where(Certification.student_id == world.alex.id)):
        c.revoked_at = clock.now()
    db.flush()
    assert code_of(db, world.alex, world.a_alex, MM1, at(1, 10)).code == "cert_missing"


def test_machine_level_certification_works_and_doesnt_leak_to_siblings(db, world):
    sam = student(db, "sam")
    a = make_approval(db, sam, [MM1, MM2])
    certify(db, sam, machine_name=MM1)
    try_book(db, sam, a, MM1, at(1, 10))
    assert code_of(db, sam, a, MM2, at(1, 12)).code == "cert_missing"


def test_group_certification_covers_all_group_machines(db, world):
    try_book(db, world.alex, world.a_alex, MM2, at(1, 10))


def test_uncertified_machine_allowed_when_not_required(db, world):
    sam = student(db, "sam")
    m = machine(db, MM1)
    m.requires_certification = False
    a = make_approval(db, sam, [MM1])
    try_book(db, sam, a, MM1, at(1, 10))


# ------------------------------------------------------------- 4. waiver
def test_waiver_required_then_recorded_inline(db, world):
    db.query(SafetyWaiver).filter_by(student_id=world.alex.id).delete()
    db.flush()
    with pytest.raises(UserError) as e:
        rs.create(db, world.alex, world.a_alex.number, machine(db, MM1).id, at(1, 10), at(1, 11))
    assert e.value.code == "waiver_required"
    r = rs.create(db, world.alex, world.a_alex.number, machine(db, MM1).id, at(1, 10), at(1, 11),
                  waiver_accepted=True, ip="1.2.3.4")
    w = db.scalar(select(SafetyWaiver).where(SafetyWaiver.student_id == world.alex.id))
    assert w.version == "1" and w.ip == "1.2.3.4" and r.status == "confirmed"


def test_new_waiver_version_forces_resigning(db, world):
    shop_settings.set_value(db, "waiver_version", "2")
    with pytest.raises(UserError) as e:
        rs.create(db, world.alex, world.a_alex.number, machine(db, MM1).id, at(1, 10), at(1, 11))
    assert e.value.code == "waiver_required"


# ------------------------------------------------------------- 5. slot rules
def test_outside_shop_hours(db, world):
    e = code_of(db, world.alex, world.a_alex, MM1, at(1, 7))
    assert e.code == "closed" and "hours" in e.message
    assert code_of(db, world.alex, world.a_alex, MM1, at(1, 21), hours=2).code == "closed"  # runs past 10 PM


def test_closed_weekend_and_holiday(db, world):
    assert code_of(db, world.alex, world.a_alex, MM1, at(date(2026, 3, 7), 10)).code == "closed"
    db.add(ShopHours(location_id=1, on_date=date(2026, 3, 5), closed=True, label="Holiday"))
    db.flush()
    assert code_of(db, world.alex, world.a_alex, MM1, at(1, 10)).code == "closed"


def test_cannot_book_past_or_started_slot(db, world):
    assert code_of(db, world.alex, world.a_alex, MM1, at(0, 9)).code == "past"
    assert code_of(db, world.alex, world.a_alex, MM1, at(0, 10)).code == "past"  # starts exactly now
    try_book(db, world.alex, world.a_alex, MM1, at(0, 11))


def test_supervisor_required_machine_needs_shift(db, world):
    db.query(ShiftSchedule).delete()
    db.flush()
    e = code_of(db, world.alex, world.a_alex, VF3, at(1, 10))
    assert e.code == "no_supervisor" and "supervisor" in e.message
    try_book(db, world.alex, world.a_alex, MM1, at(1, 10))  # mini mill needs none


def test_supervisor_shift_must_cover_whole_booking(db, world):
    sh = db.scalars(select(ShiftSchedule).where(ShiftSchedule.start_at >= at(1, 0), ShiftSchedule.start_at < at(2, 0))).one()
    sh.end_at = at(1, 12)
    db.flush()
    try_book(db, world.alex, world.a_alex, VF3, at(1, 10), hours=2)
    assert code_of(db, world.alex, world.a_alex, VF3, at(1, 11, 0), hours=2).code in ("no_supervisor", "overlap")
    assert code_of(db, world.alex, world.a_alex, VF3, at(1, 14)).code == "no_supervisor"


def test_machine_under_maintenance_blocks(db, world):
    machine(db, MM1).maintenance_state = "under_maintenance"
    db.flush()
    assert code_of(db, world.alex, world.a_alex, MM1, at(1, 10)).code == "maintenance"


def test_scheduled_maintenance_window_blocks_only_that_time(db, world):
    db.add(MaintenanceRecord(machine_id=machine(db, MM1).id, kind="scheduled", description="spindle service",
                             opened_by="sam", window_start=at(1, 12), window_end=at(1, 15)))
    db.flush()
    assert code_of(db, world.alex, world.a_alex, MM1, at(1, 13)).code == "maintenance"
    try_book(db, world.alex, world.a_alex, MM1, at(1, 10))


def test_emergency_close_blocks_booking(db, world):
    shop_settings.set_value(db, "emergency_active", True)
    assert code_of(db, world.alex, world.a_alex, MM1, at(1, 10)).code == "emergency"


def test_no_overlap(db, world):
    shop_settings.set_value(db, "max_active_per_group", 9)
    try_book(db, world.alex, world.a_alex, MM1, at(1, 10), hours=2)
    for start, hrs in [(at(1, 10), 1), (at(1, 11), 1), (at(1, 9), 2), (at(1, 9), 4)]:
        assert code_of(db, world.alex, world.a_alex, MM1, start, hrs).code == "overlap"
    try_book(db, world.alex, world.a_alex, MM1, at(1, 12))  # adjacent is fine
    try_book(db, world.alex, world.a_alex, MM2, at(1, 10))  # other machine fine


def test_cancelled_slot_can_be_rebooked(db, world):
    r = try_book(db, world.alex, world.a_alex, MM1, at(2, 10))
    rs.cancel(db, r, actor="x")
    try_book(db, world.alex, world.a_alex, MM1, at(2, 10))


# ------------------------------------------------------------- 6. fairness
def test_weekly_reservation_count(db, world):
    shop_settings.set_value(db, "max_active_per_group", 9)
    shop_settings.set_value(db, "max_reservations_per_week", 3)  # off by default: project approval gates use
    for h in (9, 11, 13):
        try_book(db, world.alex, world.a_alex, MM1, at(1, h))
    e = code_of(db, world.alex, world.a_alex, MM1, at(1, 15))
    assert e.code == "weekly_count" and "at most 3" in e.message
    try_book(db, world.alex, world.a_alex, MM1, at(7, 10))  # next week resets


def test_weekly_hours_limit(db, world):
    shop_settings.set_value(db, "max_active_per_group", 9)
    shop_settings.set_value(db, "max_hours_per_week", 5)
    try_book(db, world.alex, world.a_alex, MM1, at(1, 9), hours=4)
    e = code_of(db, world.alex, world.a_alex, MM1, at(2, 9), hours=2)
    assert e.code == "weekly_hours" and "5 hours" in e.message
    try_book(db, world.alex, world.a_alex, MM1, at(2, 9), hours=1)


def test_max_advance_window(db, world):
    far = at(15, 10)  # 15 days out; default window is 14
    d = clock.to_local(far).date()
    while d.weekday() > 4:
        d += timedelta(days=1)
    e = code_of(db, world.alex, world.a_alex, MM1, at(d, 10))
    assert e.code == "too_far" and "14 days" in e.message


def test_one_active_reservation_per_machine_group(db, world):
    try_book(db, world.alex, world.a_alex, MM1, at(1, 10))
    e = code_of(db, world.alex, world.a_alex, MM2, at(2, 10))  # same group (CNC Mills), different machine
    assert e.code == "group_limit" and "CNC Mills" in e.message
    assert code_of(db, world.alex, world.a_alex, VF3, at(2, 10)).code == "group_limit"


def test_group_limit_ignores_other_groups_and_finished_reservations(db, world):
    try_book(db, world.alex, world.a_alex, MM1, at(1, 10))
    jordan_ok = try_book(db, world.jordan, world.a_jordan, ST10, at(1, 10))  # lathe group
    assert jordan_ok
    r = db.scalar(select(Reservation).where(Reservation.student_id == world.alex.id))
    r.status = "completed"
    db.flush()
    try_book(db, world.alex, world.a_alex, MM2, at(2, 10))


def test_suspended_student_cannot_book(db, world):
    world.alex.suspended_until = clock.now() + timedelta(days=3)
    world.alex.suspension_reason = "3 no-shows"
    db.flush()
    e = code_of(db, world.alex, world.a_alex, MM1, at(1, 10))
    assert e.code == "suspended" and "3 no-shows" in e.message
    world.alex.suspended_until = clock.now() - timedelta(seconds=1)
    try_book(db, world.alex, world.a_alex, MM1, at(1, 10))


# ------------------------------------------------------------- slot listing for the UI
def test_slots_for_only_lists_genuinely_available(db, world):
    shop_settings.set_value(db, "max_active_per_group", 9)
    day = clock.to_local(at(1, 10)).date()
    try_book(db, world.alex, world.a_alex, MM1, at(1, 10))
    res = rs.slots_for(db, world.alex, world.a_alex.number, machine(db, MM1).id, day)
    states = {clock.to_local(s["start"]).hour: s["state"] for s in res["slots"]}
    assert states[10] == "taken" and states[9] == "open" and states[11] == "open" and len(res["slots"]) == 14


def test_slots_for_shows_reason_instead_of_slots_on_hard_block(db, world):
    res = rs.slots_for(db, world.alex, world.a_alex.number, machine(db, ST10).id, clock.to_local(at(1, 10)).date())
    assert res["slots"] == [] and "doesn't cover the Haas ST-10" in res["error"]


def test_slots_for_closed_day(db, world):
    res = rs.slots_for(db, world.alex, world.a_alex.number, machine(db, MM1).id, date(2026, 3, 7))
    assert res["slots"] == [] and "closed" in res["error"]


def test_slots_for_hides_past_slots_today(db, world):
    res = rs.slots_for(db, world.alex, world.a_alex.number, machine(db, MM1).id, clock.to_local(at(0, 10)).date())
    assert min(clock.to_local(s["start"]).hour for s in res["slots"]) == 11


# ------------------------------------------------------------- cancel / extend
def test_cancel_respects_cutoff(db, world):
    r = try_book(db, world.alex, world.a_alex, MM1, at(0, 13))  # starts in 3h
    clock.advance(hours=1, minutes=1)  # now 1h59 before start; cutoff is 2h
    e = pytest.raises(UserError, rs.cancel, db, r, actor="x").value
    assert e.code == "cutoff" and "120 minutes" in e.message
    rs.cancel(db, r, actor="staff:sam", by_staff=True, reason="machine down")  # staff bypass
    assert r.status == "cancelled"


def test_cancel_before_cutoff_ok_and_audited(db, world):
    from app.models import AuditLog
    r = try_book(db, world.alex, world.a_alex, MM1, at(0, 15))
    rs.cancel(db, r, actor="student:a")
    assert r.status == "cancelled" and db.scalar(select(AuditLog).where(AuditLog.action == "reservation.cancel"))
    e = pytest.raises(UserError, rs.cancel, db, r, actor="x").value
    assert e.code == "not_cancellable"


def test_extend_when_next_slot_free(db, world):
    r = try_book(db, world.alex, world.a_alex, MM1, at(1, 10))
    rs.extend(db, r, world.alex)
    assert r.end_at == at(1, 12)


def test_extend_blocked_by_next_reservation(db, world):
    r = try_book(db, world.alex, world.a_alex, MM1, at(1, 10))
    try_book(db, world.jordan, make_approval(db, world.jordan, [MM1]), MM1, at(1, 11))
    e = pytest.raises(UserError, rs.extend, db, r, world.alex).value
    assert e.code == "overlap" and "next time slot" in e.message


def test_extend_respects_hours_cap_and_closing_time(db, world):
    r = try_book(db, world.alex, world.a_alex, MM1, at(1, 20), hours=2)
    e = pytest.raises(UserError, rs.extend, db, r, world.alex).value
    assert e.code == "closed"  # would run past 10 PM


def test_cannot_extend_someone_elses_or_finished(db, world):
    r = try_book(db, world.alex, world.a_alex, MM1, at(1, 10))
    assert pytest.raises(UserError, rs.extend, db, r, world.jordan).value
    clock.advance(days=3)
    assert pytest.raises(UserError, rs.extend, db, r, world.alex).value.code == "not_extendable"


# ------------------------------------------------------------- waiting list
def full_slot(db, world):
    """Alex holds MM1 10:00 tomorrow; Jordan is certified and approved too."""
    ja = make_approval(db, world.jordan, [MM1])
    r = try_book(db, world.alex, world.a_alex, MM1, at(1, 10))
    return r, ja


def test_join_waitlist_only_when_slot_is_full(db, world):
    r, ja = full_slot(db, world)
    m = machine(db, MM1)
    w = rs.join_waitlist(db, world.jordan, ja.number, m.id, at(1, 10), at(1, 11))
    assert w.status == "waiting"
    assert pytest.raises(UserError, rs.join_waitlist, db, world.jordan, ja.number, m.id, at(1, 10), at(1, 11)).value.code == "dup"
    assert pytest.raises(UserError, rs.join_waitlist, db, world.jordan, ja.number, m.id, at(1, 14), at(1, 15)).value.code == "not_full"


def test_waitlist_requires_eligibility_too(db, world):
    r, _ = full_slot(db, world)
    sam = student(db, "sam")
    a = make_approval(db, sam, [MM1])  # no certification
    assert pytest.raises(UserError, rs.join_waitlist, db, sam, a.number, machine(db, MM1).id, at(1, 10), at(1, 11)).value.code == "cert_missing"


def test_cancel_promotes_first_in_line_and_emails_with_claim_link(db, world):
    r, ja = full_slot(db, world)
    m = machine(db, MM1)
    riley = student(db, "riley")
    certify(db, riley, group="CNC Mills")
    waiver(db, riley)
    ra = make_approval(db, riley, [MM1])
    clock.advance(seconds=1)
    w1 = rs.join_waitlist(db, world.jordan, ja.number, m.id, at(1, 10), at(1, 11))
    clock.advance(seconds=1)
    w2 = rs.join_waitlist(db, riley, ra.number, m.id, at(1, 10), at(1, 11))
    emailer.SENT.clear()
    rs.cancel(db, r, actor="student:a")
    emailer.flush_outbox(db)
    assert w1.status == "offered" and w2.status == "waiting"
    mail = next(x for x in emailer.SENT if x["to"] == world.jordan.email)
    assert w1.claim_token in mail["text"] and "30 minutes" in mail["text"]
    # the slot is held: a third party cannot grab it during the claim window
    assert code_of(db, riley, ra, MM1, at(1, 10)).code == "overlap"


def test_claim_creates_reservation(db, world):
    r, ja = full_slot(db, world)
    w = rs.join_waitlist(db, world.jordan, ja.number, machine(db, MM1).id, at(1, 10), at(1, 11))
    rs.cancel(db, r, actor="x")
    res = rs.claim(db, w.claim_token, world.jordan)
    assert res.student_id == world.jordan.id and res.start_at == at(1, 10) and w.status == "claimed"


def test_only_the_offered_student_can_claim(db, world):
    r, ja = full_slot(db, world)
    w = rs.join_waitlist(db, world.jordan, ja.number, machine(db, MM1).id, at(1, 10), at(1, 11))
    rs.cancel(db, r, actor="x")
    assert pytest.raises(UserError, rs.claim, db, w.claim_token, world.alex).value.code == "bad_claim"


def test_claim_window_expiry_passes_to_next_student(db, world):
    r, ja = full_slot(db, world)
    m = machine(db, MM1)
    riley = student(db, "riley")
    certify(db, riley, group="CNC Mills")
    waiver(db, riley)
    ra = make_approval(db, riley, [MM1])
    w1 = rs.join_waitlist(db, world.jordan, ja.number, m.id, at(1, 10), at(1, 11))
    clock.advance(seconds=5)
    w2 = rs.join_waitlist(db, riley, ra.number, m.id, at(1, 10), at(1, 11))
    rs.cancel(db, r, actor="x")
    assert w1.status == "offered"
    clock.advance(minutes=29)
    assert rs.expire_offers(db) == 0
    clock.advance(minutes=2)
    assert rs.expire_offers(db) == 1
    assert w1.status == "expired" and w2.status == "offered"
    assert pytest.raises(UserError, rs.claim, db, w1.claim_token, world.jordan).value.code == "claim_expired"
    emailer.SENT.clear()
    assert rs.claim(db, w2.claim_token, riley).start_at == at(1, 10)


def test_promotion_skips_students_who_became_ineligible(db, world):
    r, ja = full_slot(db, world)
    m = machine(db, MM1)
    w = rs.join_waitlist(db, world.jordan, ja.number, m.id, at(1, 10), at(1, 11))
    ja.status = "revoked"
    rs.cancel(db, r, actor="x")
    assert w.status == "waiting"  # nobody eligible: stays waiting, slot simply opens up


def test_waitlist_limit_three(db, world):
    ja = make_approval(db, world.jordan, [MM1, MM2], hours=50)
    shop_settings.set_value(db, "max_active_per_group", 9)
    shop_settings.set_value(db, "max_reservations_per_week", 9)
    for i, (h, name) in enumerate([(9, MM1), (11, MM1), (13, MM1), (15, MM1)]):
        try_book(db, world.alex, world.a_alex, name, at(1, h))
    m = machine(db, MM1)
    for h in (9, 11, 13):
        rs.join_waitlist(db, world.jordan, ja.number, m.id, at(1, h), at(1, h + 1))
    assert pytest.raises(UserError, rs.join_waitlist, db, world.jordan, ja.number, m.id, at(1, 15), at(1, 16)).value.code == "waitlist_limit"
