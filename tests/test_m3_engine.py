from datetime import date, datetime, time, timedelta, timezone

import pytest

from app import clock
from app.errors import UserError
from app.models import (
    MS_AVAILABLE, MS_INSPECTION, MS_IN_USE, MS_MAINTENANCE, MS_OVERDUE, MS_RESERVED,
    Machine, Reservation, ShiftSchedule, ShopHours, ShopSession, Student,
)
from app.services import rotcode, shop, status as st
from app.services import settings as shop_settings
from app.services.status import ResInfo, SessionInfo, StatusInputs, derive

NOW = datetime(2026, 3, 4, 18, 0, tzinfo=timezone.utc)  # Wed 10:00 PST
M = timedelta(minutes=1)


def res(start_off, end_off, name="Sam P.", anchor_off=None):
    s, e = NOW + start_off * M, NOW + end_off * M
    return ResInfo(s, e, NOW + (anchor_off if anchor_off is not None else start_off) * M, name)


# ---------------- status engine: one test per transition ----------------
def test_available_when_nothing_scheduled():
    s = derive(StatusInputs(), NOW)
    assert s.status == MS_AVAILABLE and s.label == "Available" and s.estimated_available_at is None


def test_available_to_reserved_within_lead_window():
    s = derive(StatusInputs(reservations=[res(20, 80)]), NOW)  # starts in 20 min, lead is 30
    assert s.status == MS_RESERVED and s.next_reservation_start == NOW + 20 * M
    assert s.estimated_available_at == NOW + 80 * M


def test_stays_available_if_reservation_is_far_off():
    s = derive(StatusInputs(reservations=[res(45, 105)]), NOW)
    assert s.status == MS_AVAILABLE and s.next_reservation_start == NOW + 45 * M


def test_reserved_while_waiting_inside_grace():
    s = derive(StatusInputs(reservations=[res(-10, 50)]), NOW)  # started 10 min ago, grace 15
    assert s.status == MS_RESERVED


def test_reserved_back_to_available_after_grace_no_show():
    s = derive(StatusInputs(reservations=[res(-16, 44)]), NOW)  # 16 min late: no longer holds the machine
    assert s.status == MS_AVAILABLE


def test_reserved_to_in_use_on_check_in():
    sess = SessionInfo("Alex R.", NOW - 5 * M, NOW + 55 * M)
    s = derive(StatusInputs(session=sess), NOW)
    assert s.status == MS_IN_USE and s.current_user == "Alex R." and s.remaining_minutes == 55
    assert s.estimated_available_at == NOW + 55 * M


def test_in_use_to_overdue_after_expected_end():
    sess = SessionInfo("Alex R.", NOW - 70 * M, NOW - 10 * M)
    s = derive(StatusInputs(session=sess), NOW)
    assert s.status == MS_OVERDUE and s.overdue_minutes == 10 and s.current_user == "Alex R."


def test_in_use_not_overdue_at_exact_end():
    sess = SessionInfo("Alex R.", NOW - 60 * M, NOW)
    assert derive(StatusInputs(session=sess), NOW).status == MS_IN_USE


def test_in_use_back_to_available_after_checkout():
    assert derive(StatusInputs(session=None), NOW).status == MS_AVAILABLE


def test_in_use_to_reserved_when_next_booking_is_soon():
    # Checked out; next student arrives in 10 minutes
    assert derive(StatusInputs(reservations=[res(10, 70)]), NOW).status == MS_RESERVED


def test_estimated_available_follows_back_to_back_reservations():
    sess = SessionInfo("A B.", NOW - 30 * M, NOW + 30 * M)
    s = derive(StatusInputs(session=sess, reservations=[res(30, 90), res(90, 150)]), NOW)
    assert s.estimated_available_at == NOW + 150 * M


def test_under_maintenance_flag():
    s = derive(StatusInputs(maintenance_state="under_maintenance", reservations=[res(5, 65)]), NOW)
    assert s.status == MS_MAINTENANCE and s.label == "Under Maintenance" and s.estimated_available_at is None


def test_awaiting_inspection_flag():
    assert derive(StatusInputs(maintenance_state="awaiting_inspection"), NOW).status == MS_INSPECTION


def test_inspection_cleared_returns_to_available():
    assert derive(StatusInputs(maintenance_state="none"), NOW).status == MS_AVAILABLE


def test_manual_override_wins_with_reason():
    s = derive(StatusInputs(override_status=MS_MAINTENANCE, override_reason="coolant leak",
                            session=SessionInfo("A B.", NOW, NOW + 60 * M)), NOW)
    assert s.status == MS_MAINTENANCE and s.reason == "coolant leak"


def test_emergency_close_beats_everything():
    s = derive(StatusInputs(emergency=True, override_status=MS_AVAILABLE,
                            session=SessionInfo("A B.", NOW, NOW + 60 * M)), NOW)
    assert s.status == MS_MAINTENANCE and s.reason == "Emergency close"


def test_late_claimed_slot_uses_booking_time_anchor():
    # Slot began 30 min ago but it was only booked now -> still holds the machine
    assert derive(StatusInputs(reservations=[res(-30, 30, anchor_off=0)]), NOW).status == MS_RESERVED


def test_db_backed_status_matches_pure_function(db, demo):
    m = db.query(Machine).filter_by(name="Haas VF-3").one()
    stu = db.query(Student).first()
    assert st.machine_state(db, m).status == MS_AVAILABLE
    db.add(Reservation(ref="RSV-AAAAAA", student_id=stu.id, machine_id=m.id,
                       start_at=NOW + 10 * M, end_at=NOW + 70 * M))
    db.flush()
    assert st.machine_state(db, m).status == MS_RESERVED
    db.add(ShopSession(student_id=stu.id, machine_id=m.id, checked_in_at=NOW, expected_end_at=NOW + 60 * M))
    db.flush()
    state = st.machine_state(db, m)
    assert state.status == MS_IN_USE and state.current_user == stu.public_name
    clock.advance(minutes=61)
    assert st.machine_state(db, m).status == MS_OVERDUE


def test_emergency_setting_flips_all_machines(db, demo):
    shop_settings.set_value(db, "emergency_active", True)
    assert {s.status for _, s in st.board(db)} == {MS_MAINTENANCE}


# ---------------- shop hours ----------------
def test_hours_weekday_vs_weekend(db, demo):
    assert shop.hours_for_date(db, date(2026, 3, 4)) is not None  # Wed
    assert shop.hours_for_date(db, date(2026, 3, 7)) is None  # Sat closed
    o, c = shop.hours_for_date(db, date(2026, 3, 4))
    assert o == datetime(2026, 3, 4, 16, 0, tzinfo=timezone.utc) and c == datetime(2026, 3, 5, 2, 0, tzinfo=timezone.utc)


def test_holiday_exception_closes_shop(db, demo):
    db.add(ShopHours(location_id=1, on_date=date(2026, 3, 4), closed=True, label="Holiday"))
    db.flush()
    assert shop.hours_for_date(db, date(2026, 3, 4)) is None
    assert not shop.is_open(db, NOW)


def test_special_hours_exception(db, demo):
    db.add(ShopHours(location_id=1, on_date=date(2026, 3, 4), open_time=time(12), close_time=time(14)))
    db.flush()
    assert not shop.is_open(db, NOW)  # 10:00 local is before special opening
    assert shop.is_open(db, NOW + 150 * M)


def test_is_open_boundaries(db, demo):
    open_ = datetime(2026, 3, 4, 16, 0, tzinfo=timezone.utc)
    assert shop.is_open(db, open_) and not shop.is_open(db, open_ - M)
    close = datetime(2026, 3, 5, 2, 0, tzinfo=timezone.utc)
    assert not shop.is_open(db, close) and shop.is_open(db, close - M)


def test_slot_grid_normal_day(db, demo):
    slots = shop.slot_grid(db, date(2026, 3, 4), 60)
    assert len(slots) == 10 and slots[0][0].hour == 16 and slots[-1][1].hour == 2


def test_slot_grid_dst_spring_forward_has_one_less_hour(db, demo):
    # 2026-03-08 is the US spring-forward Sunday: 00:00-06:00 local is only 5 real hours.
    db.add(ShopHours(location_id=1, on_date=date(2026, 3, 8), open_time=time(0), close_time=time(6)))
    db.flush()
    slots = shop.slot_grid(db, date(2026, 3, 8), 60)
    assert len(slots) == 5
    assert all((e - s) == timedelta(hours=1) for s, e in slots)


def test_slot_grid_dst_fall_back_has_one_extra_hour(db, demo):
    # 2026-11-01 fall back: 00:00-06:00 local is 7 real hours.
    db.add(ShopHours(location_id=1, on_date=date(2026, 11, 1), open_time=time(0), close_time=time(6)))
    db.flush()
    assert len(shop.slot_grid(db, date(2026, 11, 1), 60)) == 7


def test_slot_grid_partial_final_slot_dropped(db, demo):
    assert len(shop.slot_grid(db, date(2026, 3, 4), 90)) == 6  # 10h / 1.5h = 6.67 -> 6


def test_supervisor_on_duty_and_coverage(db, demo):
    assert shop.supervisor_on_duty(db, NOW).username == "sam"
    assert shop.supervisor_covers(db, NOW, NOW + 120 * M)
    saturday = datetime(2026, 3, 7, 18, 0, tzinfo=timezone.utc)
    assert shop.supervisor_on_duty(db, saturday) is None
    assert not shop.supervisor_covers(db, saturday, saturday + 60 * M)


def test_supervisor_coverage_across_back_to_back_shifts(db, demo):
    sam = shop.supervisor_on_duty(db, NOW)
    t = datetime(2026, 3, 6, 0, 0, tzinfo=timezone.utc)
    db.add_all([ShiftSchedule(staff_id=sam.id, start_at=t + timedelta(days=30), end_at=t + timedelta(days=30, hours=4)),
                ShiftSchedule(staff_id=sam.id, start_at=t + timedelta(days=30, hours=4), end_at=t + timedelta(days=30, hours=8))])
    db.flush()
    assert shop.supervisor_covers(db, t + timedelta(days=30, hours=2), t + timedelta(days=30, hours=6))
    assert not shop.supervisor_covers(db, t + timedelta(days=30, hours=7), t + timedelta(days=30, hours=9))


# ---------------- rotating code ----------------
def test_code_is_four_digits_and_stable_within_interval(db):
    a = rotcode.current(db)
    assert len(a["code"]) == 4 and a["code"].isdigit()
    clock.advance(minutes=1)
    assert rotcode.current(db)["code"] == a["code"]


def test_code_rotates_every_ten_minutes(db):
    # T0 is 10:00 PST = 18:00 UTC, exactly on a 10-minute boundary
    a = rotcode.current(db)
    assert a["seconds_left"] == 600
    clock.advance(minutes=10)
    b = rotcode.current(db)
    assert b["code"] != a["code"]


def test_previous_code_valid_two_more_minutes(db):
    a = rotcode.current(db)["code"]
    clock.advance(minutes=10)
    b = rotcode.current(db)["code"]
    assert rotcode.valid_codes(db) == {a, b}
    clock.advance(minutes=1, seconds=59)
    assert a in rotcode.valid_codes(db)
    clock.advance(seconds=2)
    assert rotcode.valid_codes(db) == {b}


def test_rotation_interval_is_configurable(db):
    shop_settings.set_value(db, "rotating_code_interval_min", 5)
    a = rotcode.current(db)
    assert a["seconds_left"] == 300
    clock.advance(minutes=5)
    assert rotcode.current(db)["code"] != a["code"]


def test_verify_accepts_current_and_rejects_wrong(db):
    code = rotcode.current(db)["code"]
    rotcode.verify(db, code, "1.1.1.1", "RSV-AAAAAA")
    wrong = "0000" if code != "0000" else "1111"
    with pytest.raises(UserError) as e:
        rotcode.verify(db, wrong, "1.1.1.1", "RSV-AAAAAA")
    assert e.value.code == "bad_shop_code"


def test_verify_rate_limited_per_reference(db):
    code = rotcode.current(db)["code"]
    wrong = "0000" if code != "0000" else "1111"
    for _ in range(5):
        with pytest.raises(UserError):
            rotcode.verify(db, wrong, "9.9.9.9", "RSV-ZZZZZZ")
    with pytest.raises(UserError) as e:  # now even the correct code is blocked for this reference
        rotcode.verify(db, code, "8.8.8.8", "RSV-ZZZZZZ")
    assert e.value.code == "rate_limited"
    rotcode.verify(db, code, "8.8.8.8", "RSV-OTHER1")  # other references unaffected


def test_verify_rate_limited_per_ip(db):
    code = rotcode.current(db)["code"]
    wrong = "0000" if code != "0000" else "1111"
    for i in range(10):
        with pytest.raises(UserError):
            rotcode.verify(db, wrong, "7.7.7.7", f"RSV-{i:06d}")
    with pytest.raises(UserError) as e:
        rotcode.verify(db, code, "7.7.7.7", "RSV-FRESH1")
    assert e.value.code == "rate_limited"
    clock.advance(minutes=11)
    rotcode.verify(db, rotcode.current(db)["code"], "7.7.7.7", "RSV-FRESH1")
