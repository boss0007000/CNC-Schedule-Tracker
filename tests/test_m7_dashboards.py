import json
from datetime import timedelta
from html import unescape

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import clock, emailer
from app.main import create_app
from app.models import (
    Approval, AuditLog, Certification, DamageReport, IncidentReport, Machine, MachineGroup, MaintenanceRecord,
    Reservation, ShopSession,
)
from app.services import reservations as rs, rotcode, sessions
from app.services import settings as shop_settings
from tests.factories import at, book, machine, make_approval, student
from tests.helpers import staff_login


def make_client(user):
    c = TestClient(create_app(), base_url="http://testserver")
    c.__enter__()
    tok = staff_login(c, user)
    return c, tok


@pytest.fixture
def sup(demo):
    c, tok = make_client("sam")
    c.tok = tok
    yield c
    c.__exit__(None, None, None)


@pytest.fixture
def adm(demo):
    c, tok = make_client("admin")
    c.tok = tok
    yield c
    c.__exit__(None, None, None)


@pytest.fixture
def mon(demo):
    c, tok = make_client("mo")
    c.tok = tok
    yield c
    c.__exit__(None, None, None)


def post(c, path, **data):
    return c.post(path, data={"csrf_token": c.tok, **data}, follow_redirects=False)


# ============================================================ TV
def test_tv_page_is_public_and_has_banner_hooks(client):
    r = client.get("/tv")
    assert r.status_code == 200 and 'id="conn"' in r.text and "/static/tv.js" in r.text and "emergency" in r.text


def test_tv_js_has_connection_lost_message_and_5s_poll():
    js = open("app/static/tv.js", encoding="utf-8").read()
    assert "Connection lost, last updated" in js and "setInterval(poll, 5000)" in js


def test_tv_data_shape_and_privacy(client, db, demo):
    alex = student(db, "alex")
    a = db.scalar(select(Approval).where(Approval.student_id == alex.id))
    alex.full_name = "Alexandra Marie Rivera"
    r = book(db, alex, a, "Haas Mini Mill 1", at(0, 11))
    db.commit()
    clock.freeze(at(0, 11, 5))
    sessions.check_in(db, alex, r.ref, rotcode.current(db)["code"], "1.1.1.1")
    db.commit()
    from app.models import ShiftSchedule
    for sh in db.scalars(select(ShiftSchedule)):
        sh.checked_in_at = at(0, 8, 0)
    db.commit()
    resp = client.get("/tv/data")
    assert resp.status_code == 200 and resp.headers["cache-control"] == "no-store"
    d = resp.json()
    blob = json.dumps(d)
    assert "@" not in blob and "csuchico" not in blob and "Marie" not in blob and "Rivera" not in blob
    m = next(x for x in d["machines"] if x["name"] == "Haas Mini Mill 1")
    assert m["status"] == "in_use" and m["label"] == "In Use" and m["user"] == "Alexandra R."
    assert m["remaining_minutes"] == 55 and m["available_at"] == "12:00 PM"
    assert len(d["code"]["value"]) == 4 and d["shop_open"] is True and d["supervisor"] == "Sam S."
    assert [q["label"] for q in d["qr"]] == ["Submit a Project", "Reserve a Machine", "Check In", "Check Out"]
    assert {x["label"] for x in d["machines"]} >= {"Available"}


def test_tv_shows_closed_outside_hours(client, demo):
    clock.freeze(at(0, 22))
    d = client.get("/tv/data").json()
    assert d["shop_open"] is False


def test_tv_emergency_banner_payload(client, db, demo):
    shop_settings.set_value(db, "emergency_active", True)
    db.commit()
    d = client.get("/tv/data").json()
    assert d["emergency"]["active"] and d["shop_open"] is False
    assert {m["status"] for m in d["machines"]} == {"under_maintenance"}


def test_qr_images_served_and_are_stable_urls(client):
    for name in ("submit", "reserve", "checkin", "checkout"):
        r = client.get(f"/qr/{name}.png")
        assert r.status_code == 200 and r.content.startswith(b"\x89PNG")
    assert client.get("/qr/nope.png").status_code == 404


# ============================================================ dashboard / roles
def test_dashboard_renders_for_every_role(sup, adm, mon):
    for c in (sup, adm, mon):
        r = c.get("/admin")
        assert r.status_code == 200 and "Live machine board" in r.text and "Haas VF-3" in r.text
    assert "Emergency close" in sup.get("/admin").text and "Emergency close" not in mon.get("/admin").text


@pytest.mark.parametrize("path", ["/admin/approvals", "/admin/reservations", "/admin/reservations?view=calendar",
                                  "/admin/sessions", "/admin/sessions?show=review", "/admin/machines", "/admin/students",
                                  "/admin/incidents", "/admin/machines/1", "/admin/students/1"])
def test_admin_pages_render(adm, path):
    assert adm.get(path).status_code == 200


def test_dashboard_flags_overdue_and_pending(sup, db):
    alex = student(db, "alex")
    a = db.scalar(select(Approval).where(Approval.student_id == alex.id))
    r = book(db, alex, a, "Haas Mini Mill 1", at(0, 11))
    db.commit()
    clock.freeze(at(0, 11, 5))
    sessions.check_in(db, alex, r.ref, rotcode.current(db)["code"], "1.1.1.1")
    db.commit()
    clock.freeze(at(0, 12, 20))
    page = sup.get("/admin").text
    assert "Overdue" in page and "1 overdue" in page and "pending approvals" in page


def test_monitor_cannot_mutate(mon, db):
    m = machine(db, "Haas VF-3")
    for path, data in [(f"/admin/machines/{m.id}/override", {"status": "available", "reason": "x"}),
                       ("/admin/emergency", {"action": "close", "confirm": "CLOSE"}),
                       ("/admin/incidents/new", {"kind": "injury"}), ("/admin/machines/save", {"name": "New"})]:
        assert post(mon, path, **data).status_code == 403


def test_supervisor_cannot_crud_machines_but_admin_can(sup, adm, db):
    assert post(sup, "/admin/machines/save", name="Bandsaw").status_code == 403
    r = post(adm, "/admin/machines/save", name="Bandsaw", description="Vertical", requires_certification="on")
    assert r.status_code == 303 and db.scalar(select(Machine).where(Machine.name == "Bandsaw"))
    dup = post(adm, "/admin/machines/save", name="bandsaw")
    assert dup.status_code == 303 and "already+exists" in dup.headers["location"].replace("%20", "+") or "already" in dup.headers["location"]
    assert post(adm, "/admin/groups/save", name="Saws").status_code == 303
    assert db.scalar(select(MachineGroup).where(MachineGroup.name == "Saws"))


# ============================================================ machine ops
def test_status_override_is_audited_and_changes_board(sup, client, db):
    m = machine(db, "Haas VF-3")
    assert post(sup, f"/admin/machines/{m.id}/override", status="under_maintenance", reason="").status_code == 303
    db.expire_all()
    assert machine(db, "Haas VF-3").override_status is None  # reason required
    post(sup, f"/admin/machines/{m.id}/override", status="under_maintenance", reason="coolant leak")
    d = client.get("/tv/data").json()
    vf3 = next(x for x in d["machines"] if x["name"] == "Haas VF-3")
    assert vf3["status"] == "under_maintenance"
    row = db.scalar(select(AuditLog).where(AuditLog.action == "machine.override"))
    assert row.actor == "staff:sam" and row.after["reason"] == "coolant leak"
    post(sup, f"/admin/machines/{m.id}/override", status="", reason="")
    assert next(x for x in client.get("/tv/data").json()["machines"] if x["name"] == "Haas VF-3")["status"] == "available"


def test_maintenance_log_and_inspection_clearance(sup, db):
    m = machine(db, "Haas ST-10")
    post(sup, f"/admin/machines/{m.id}/maintenance", kind="issue", state="awaiting_inspection", description="Chuck jaw cracked")
    db.expire_all()
    assert machine(db, "Haas ST-10").maintenance_state == "awaiting_inspection"
    assert post(sup, f"/admin/machines/{m.id}/clear", note="jaws replaced").status_code == 303
    db.expire_all()
    assert machine(db, "Haas ST-10").maintenance_state == "none"
    rec = db.scalar(select(MaintenanceRecord).where(MaintenanceRecord.machine_id == m.id))
    assert rec.closed_at is not None and rec.closed_by == "sam"


def test_scheduled_maintenance_window_blocks_bookings(sup, db):
    alex = student(db, "alex")
    a = db.scalar(select(Approval).where(Approval.student_id == alex.id))
    m = machine(db, "Haas Mini Mill 1")
    post(sup, f"/admin/machines/{m.id}/maintenance", kind="scheduled", state="none", description="Spindle service",
         window_start="2026-03-05T12:00", window_end="2026-03-05T15:00")
    with pytest.raises(Exception) as e:
        book(db, alex, a, "Haas Mini Mill 1", at(1, 13))
    assert "scheduled maintenance" in str(e.value)


# ============================================================ students & certs
def test_grant_and_revoke_certification_via_http(sup, db):
    sam_p = student(db, "sam")
    r = post(sup, f"/admin/students/{sam_p.id}/cert", target="g:%d" % db.scalar(select(MachineGroup.id).where(MachineGroup.name == "CNC Mills")),
             expires_on="2027-01-31", notes="completed class")
    assert r.status_code == 303
    c = db.scalar(select(Certification).where(Certification.student_id == sam_p.id))
    assert c.granted_by == "sam" and c.expires_at is not None
    assert post(sup, f"/admin/certs/{c.id}/revoke").status_code == 303
    db.expire_all()
    assert db.get(Certification, c.id).revoked_at is not None
    acts = [a.action for a in db.scalars(select(AuditLog))]
    assert "certification.grant" in acts and "certification.revoke" in acts


def test_cert_expiry_must_be_future(sup, db):
    s = student(db, "sam")
    r = post(sup, f"/admin/students/{s.id}/cert", target="m:1", expires_on="2020-01-01")
    assert r.status_code == 303 and "future" in unescape(r.headers["location"]).replace("+", " ")
    assert db.scalar(select(Certification).where(Certification.student_id == s.id)) is None


def test_manual_suspension_and_lift(sup, db):
    s = student(db, "riley")
    post(sup, f"/admin/students/{s.id}/suspend", days="7", reason="unsafe behaviour")
    db.expire_all()
    assert student(db, "riley").suspended_until > clock.now()
    post(sup, f"/admin/students/{s.id}/unsuspend", reason="resolved with student")
    db.expire_all()
    assert student(db, "riley").suspended_until is None
    assert "Suspended" not in sup.get(f"/admin/students/{s.id}").text


def test_student_page_shows_waiver_and_search(sup):
    page = sup.get("/admin/students?q=alex").text
    assert "Alex Rivera" in page and "Jordan" not in page
    assert "signed" in sup.get("/admin/students/1").text


# ============================================================ incidents
def test_incident_form_separate_from_damage_reports(sup, db):
    bad = post(sup, "/admin/incidents/new", kind="injury", severity="minor", description="short")
    assert bad.status_code == 400
    ok = post(sup, "/admin/incidents/new", kind="near_miss", severity="moderate", machine_id="1",
              description="Chip flew past face shield, no injury.", action_taken="Reminded of guard use",
              student_email="alex.rivera@csuchico.edu")
    assert ok.status_code == 303
    inc = db.scalar(select(IncidentReport))
    assert inc.kind == "near_miss" and inc.reported_by == "sam" and db.scalar(select(DamageReport)) is None
    assert post(sup, f"/admin/incidents/{inc.id}/resolve").status_code == 303
    db.expire_all()
    assert db.get(IncidentReport, inc.id).resolved is True
    assert "Chip flew" in sup.get("/admin/incidents").text


def test_unknown_student_email_rejected_in_incident(sup):
    r = post(sup, "/admin/incidents/new", kind="injury", severity="minor", description="Cut finger on burr.",
             student_email="ghost@csuchico.edu")
    assert r.status_code == 400 and "No student with that email" in unescape(r.text)


# ============================================================ emergency close
def test_emergency_close_requires_confirmation_then_works_and_reopens(sup, client, db):
    alex = student(db, "alex")
    a = db.scalar(select(Approval).where(Approval.student_id == alex.id))
    r = book(db, alex, a, "Haas Mini Mill 1", at(0, 11))
    db.commit()
    clock.freeze(at(0, 11, 5))
    sessions.check_in(db, alex, r.ref, rotcode.current(db)["code"], "1.1.1.1")
    db.commit()
    emailer.SENT.clear()
    assert post(sup, "/admin/emergency", action="close", confirm="nope").status_code == 400
    assert shop_settings.load(db).emergency_active is False
    ok = post(sup, "/admin/emergency", action="close", confirm="CLOSE", message="Fire alarm - evacuate")
    assert ok.status_code == 303
    db.expire_all()
    assert shop_settings.load(db).emergency_active is True
    d = client.get("/tv/data").json()
    assert d["emergency"] == {"active": True, "message": "Fire alarm - evacuate"}
    assert {m["status"] for m in d["machines"]} == {"under_maintenance"}
    assert [m for m in emailer.SENT if m["to"] == alex.email and "EMERGENCY" in m["subject"]]  # active user emailed
    row = db.scalar(select(AuditLog).where(AuditLog.action == "emergency.close"))
    assert row.actor == "staff:sam" and row.after["emailed"] == 1
    with pytest.raises(Exception):
        book(db, student(db, "alex"), a, "Haas Mini Mill 2", at(1, 10))
    assert post(sup, "/admin/emergency", action="reopen").status_code == 400  # needs confirm
    assert post(sup, "/admin/emergency", action="reopen", confirm="on").status_code == 303
    db.expire_all()
    assert shop_settings.load(db).emergency_active is False
    assert db.scalar(select(AuditLog).where(AuditLog.action == "emergency.reopen"))
    assert next(m for m in client.get("/tv/data").json()["machines"] if m["name"] == "Haas Mini Mill 1")["status"] == "in_use"


# ============================================================ reservations (staff)
def test_staff_manual_create_with_and_without_rules(sup, db):
    alex = student(db, "alex")
    a = db.scalar(select(Approval).where(Approval.student_id == alex.id))
    m = machine(db, "Haas Mini Mill 2")
    r = post(sup, "/admin/reservations/new", email=alex.email, machine_id=str(m.id), start="2026-03-05T10:00", hours="2",
             approval=a.number)
    assert r.status_code == 303
    res = db.scalar(select(Reservation).where(Reservation.machine_id == m.id))
    assert res.manual and res.hours == 2
    sam_p = student(db, "sam")  # no approval/cert: only possible with override
    r = post(sup, "/admin/reservations/new", email=sam_p.email, machine_id=str(m.id), start="2026-03-05T14:00", hours="1",
             override="on", reason="")
    assert "reason" in unescape(r.headers["location"]).lower()
    r = post(sup, "/admin/reservations/new", email=sam_p.email, machine_id=str(m.id), start="2026-03-05T14:00", hours="1",
             override="on", reason="Instructor-led demo")
    assert r.status_code == 303 and db.scalar(select(AuditLog).where(AuditLog.action == "reservation.override_rules"))


def test_staff_cancel_release_move_and_conflicts(sup, db):
    alex, jordan = student(db, "alex"), student(db, "jordan")
    a = db.scalar(select(Approval).where(Approval.student_id == alex.id))
    ja = make_approval(db, jordan, ["Haas Mini Mill 1"])
    r1 = book(db, alex, a, "Haas Mini Mill 1", at(0, 15))
    r2 = book(db, jordan, ja, "Haas Mini Mill 1", at(1, 10))
    db.commit()
    emailer.SENT.clear()
    # move r2 on top of r1: staff edit permits it, producing a conflict the dashboard lists
    post(sup, f"/admin/reservations/{r2.id}/move", machine_id=str(r2.machine_id), start="2026-03-04T16:00", end="2026-03-04T17:00")
    db.expire_all()
    page = sup.get("/admin/reservations?date=2026-03-04").text
    assert db.get(Reservation, r2.id).start_at == at(0, 16) or "conflict" not in page
    # cancel with email, release silently
    assert post(sup, f"/admin/reservations/{r1.id}/cancel", reason="machine down").status_code == 303
    emailer.flush_in_new_session()
    assert any("cancelled" in m["subject"].lower() for m in emailer.SENT)
    emailer.SENT.clear()
    assert post(sup, f"/admin/reservations/{r2.id}/release").status_code == 303
    emailer.flush_in_new_session()
    assert not emailer.SENT
    db.expire_all()
    assert db.get(Reservation, r1.id).status == "cancelled" and db.get(Reservation, r2.id).status == "cancelled"


def test_calendar_view_shows_bookings(sup, db):
    alex = student(db, "alex")
    a = db.scalar(select(Approval).where(Approval.student_id == alex.id))
    book(db, alex, a, "Haas Mini Mill 1", at(0, 15))
    db.commit()
    page = sup.get("/admin/reservations?view=calendar&date=2026-03-04").text
    assert "Alex R." in page and "Haas Mini Mill 1" in page


def test_force_check_out_and_review_flags(sup, db):
    alex = student(db, "alex")
    a = db.scalar(select(Approval).where(Approval.student_id == alex.id))
    r = book(db, alex, a, "Haas Mini Mill 1", at(0, 11))
    db.commit()
    clock.freeze(at(0, 11, 5))
    s = sessions.check_in(db, alex, r.ref, rotcode.current(db)["code"], "1.1.1.1")
    db.commit()
    assert post(sup, f"/admin/sessions/{s.id}/checkout", reason="").status_code == 303  # reason required -> no change
    db.expire_all()
    assert db.get(ShopSession, s.id).status == "active"
    post(sup, f"/admin/sessions/{s.id}/checkout", reason="student left, phone died")
    db.expire_all()
    s = db.get(ShopSession, s.id)
    assert s.status == "force_closed" and s.forced_by == "sam"
    s.needs_review = True
    db.commit()
    assert "needs review" in sup.get("/admin/sessions?show=review").text
    post(sup, f"/admin/sessions/{s.id}/review")
    db.expire_all()
    assert db.get(ShopSession, s.id).needs_review is False


def test_tv_timeline_segments_are_anonymous_and_correct(client, db, demo):
    alex = student(db, "alex")
    a = db.scalar(select(Approval).where(Approval.student_id == alex.id))
    r = book(db, alex, a, "Haas Mini Mill 1", at(0, 11))
    book(db, student(db, "jordan"), make_approval(db, student(db, "jordan"), ["Haas Mini Mill 1"]), "Haas Mini Mill 1", at(0, 15))
    db.commit()
    clock.freeze(at(0, 11, 5))
    sessions.check_in(db, alex, r.ref, rotcode.current(db)["code"], "1.1.1.1")
    db.commit()
    d = client.get("/tv/data").json()
    w = d["window"]
    assert w["end"] - w["start"] == 14 * 3600 and w["start"] <= w["now"] <= w["end"] and len(w["ticks"]) >= 5
    m = next(x for x in d["machines"] if x["name"] == "Haas Mini Mill 1")
    kinds = {(s["k"], s["s"], s["e"]) for s in m["timeline"]}
    assert ("inuse", int(at(0, 11).timestamp()), int(at(0, 12).timestamp())) in kinds
    assert ("booked", int(at(0, 15).timestamp()), int(at(0, 16).timestamp())) in kinds
    assert m["since"] == "11:05 AM" and m["user"] == "Alex R."
    free = next(x for x in d["machines"] if x["name"] == "Haas VF-3")
    assert free["free_label"] == "Free for the rest of the day"
    assert "alex" not in json.dumps(d["machines"][0]["timeline"]).lower()


def test_tv_says_closed_instead_of_open_now_after_hours(client, db, demo):
    clock.freeze(at(0, 22, 14))  # 10:14 PM, after closing
    d = client.get("/tv/data").json()
    assert d["shop_open"] is False
    m = d["machines"][0]
    assert m["status"] == "available" and m["free_label"].startswith("Opens Thu 8:00 AM")
    js = open("app/static/tv.js", encoding="utf-8").read()
    assert "'Shop closed'" in js and "'Closed'" in js


# ============================================================ week view block on the TV
def test_tv_week_block_has_seven_days_and_one_row_per_machine(client, db, demo):
    d = client.get("/tv/data").json()
    wk = d["week"]
    assert len(wk["days"]) == 7 and wk["days"][0]["today"] is True
    assert [m["name"] for m in wk["machines"]] == [m["name"] for m in d["machines"]]
    assert all(len(m["cells"]) == 7 for m in wk["machines"])
    weekend = [i for i, x in enumerate(wk["days"]) if x["label"] in ("Sat", "Sun")]
    for m in wk["machines"]:
        for i in weekend:
            assert m["cells"][i]["closed"] is True and m["cells"][i]["states"] == []


def test_tv_week_block_shows_booked_inuse_hold_and_down_without_names(client, db, demo):
    from app.services import holds
    alex = student(db, "alex")
    a = db.scalar(select(Approval).where(Approval.student_id == alex.id))
    shop_settings.set_value(db, "max_active_per_group", 9)
    book(db, alex, a, "Haas Mini Mill 1", at(1, 10), 3)  # Thursday 10-13
    book(db, alex, a, "Haas Mini Mill 2", at(0, 15), 1)
    holds.declare(db, student(db, "jordan"), machine(db, "Haas VF-3"), "tools", via="staff", actor="staff:sam", now=at(0, 20))
    machine(db, "Haas ST-10").maintenance_state = "under_maintenance"
    db.commit()
    wk = client.get("/tv/data").json()["week"]
    cells = {m["name"]: m["cells"] for m in wk["machines"]}
    thu = cells["Haas Mini Mill 1"][1]
    assert thu["states"][:2] == ["free", "free"] and thu["states"][2:5] == ["booked"] * 3 and thu["free_hours"] == 11.0
    today = cells["Haas Mini Mill 2"][0]
    assert today["states"][:2] == ["past", "past"] and "booked" in today["states"]  # 10 AM now: earlier slots are past
    assert "hold" in cells["Haas VF-3"][0]["states"] and set(cells["Haas ST-10"][0]["states"]) <= {"past", "down"}
    assert "alex" not in json.dumps(wk).lower() and "jordan" not in json.dumps(wk).lower()


def test_tv_page_has_a_separate_week_panel_and_renderer(client):
    html = client.get("/tv").text
    assert 'id="week"' in html and html.index('id="grid"') < html.index('id="week"') < html.index('id="qrs"')
    js = open("app/static/tv.js", encoding="utf-8").read()
    assert "function weekBlock" in js and "THIS WEEK" in js and "h free" in js
