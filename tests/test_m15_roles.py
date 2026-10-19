"""Permission levels: each staff role may do only what it is for; enforced on every admin write route."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import AuditLog, StaffUser
from app.services import staffauth
from tests.factories import student
from tests.helpers import csrf, staff_login
from tests.test_m9_hardening import concrete, routes

ROLE_USERS = {"monitor": "mo", "floor_supervisor": "fran", "part_reviewer": "pat", "trainer": "tara",
              "scheduler": "sky", "maintenance": "max", "supervisor": "sam", "admin": "admin"}


def login_as(username):
    c = TestClient(create_app(), base_url="http://testserver")
    c.__enter__()
    return c, staff_login(c, username)


def admin_posts(app):
    return [r for r in routes(app) if "POST" in r.methods and r.path.startswith("/admin")
            and r.path not in ("/admin/login", "/admin/logout", "/admin/password")]


def test_demo_roles_have_expected_permissions(demo, db):
    for role, uname in ROLE_USERS.items():
        u = db.scalar(select(StaffUser).where(StaffUser.username == uname))
        assert role in staffauth.roles_of(u)
    mo = db.scalar(select(StaffUser).where(StaffUser.username == "mo"))
    assert staffauth.perms_of(mo) == {"view"}
    fran = db.scalar(select(StaffUser).where(StaffUser.username == "fran"))
    assert staffauth.can(fran, "emergency") and staffauth.can(fran, "checkins.decide")
    assert not staffauth.can(fran, "projects.review") and not staffauth.can(fran, "admin")
    pat = db.scalar(select(StaffUser).where(StaffUser.username == "pat"))
    assert staffauth.can(pat, "projects.review") and not staffauth.can(pat, "emergency")


def test_monitor_cannot_write_anywhere(client, demo):
    c, tok = login_as("mo")
    try:
        for r in admin_posts(c.app):
            resp = c.post(concrete(r.path), data={"csrf_token": tok}, follow_redirects=False)
            assert resp.status_code == 403, (r.path, resp.status_code)
    finally:
        c.__exit__(None, None, None)


@pytest.mark.parametrize("uname", ["fran", "pat", "tara", "sky", "max"])
def test_specialised_roles_cannot_manage_staff_or_settings(client, demo, uname):
    c, tok = login_as(uname)
    try:
        for path in ("/admin/users", "/admin/settings", "/admin/shop"):
            assert c.post(path, data={"csrf_token": tok, "action": "create"}, follow_redirects=False).status_code in (403, 404, 405), path
        assert c.get("/admin/users").status_code == 403
    finally:
        c.__exit__(None, None, None)


def test_only_part_reviewer_style_roles_can_decide_projects(client, demo):
    for uname, allowed in (("fran", False), ("tara", False), ("sky", False), ("max", False), ("pat", True)):
        c, tok = login_as(uname)
        try:
            r = c.post("/admin/approvals/999999/decide", data={"csrf_token": tok, "decision": "approve"}, follow_redirects=False)
            if allowed:
                assert r.status_code != 403, uname
            else:
                assert r.status_code == 403, uname
        finally:
            c.__exit__(None, None, None)


def test_floor_supervisor_can_use_emergency_stop_but_scheduler_cannot(client, demo):
    c1, t1 = login_as("fran")
    c2, t2 = login_as("sky")
    try:
        assert c2.post("/admin/emergency", data={"csrf_token": t2}, follow_redirects=False).status_code == 403
        assert c1.post("/admin/emergency", data={"csrf_token": t1}, follow_redirects=False).status_code != 403
    finally:
        c1.__exit__(None, None, None)
        c2.__exit__(None, None, None)


def test_admin_can_assign_multiple_roles_and_union_applies(client, db, demo):
    tok = staff_login(client, "admin")
    r = client.post("/admin/users", data={"csrf_token": tok, "action": "create", "username": "combo",
                                          "email": "combo@example.edu", "roles": ["trainer", "part_reviewer"]},
                    follow_redirects=False)
    assert r.status_code in (200, 303)
    u = db.scalar(select(StaffUser).where(StaffUser.username == "combo"))
    assert set(u.roles) == {"trainer", "part_reviewer"}
    assert staffauth.can(u, "certs.manage") and staffauth.can(u, "projects.review")
    assert not staffauth.can(u, "emergency")
    row = db.scalar(select(AuditLog).where(AuditLog.action == "staff.create").order_by(AuditLog.id.desc()))
    assert row.actor == "staff:admin"


def test_set_roles_and_last_admin_protection(client, db, demo):
    tok = staff_login(client, "admin")
    admin = db.scalar(select(StaffUser).where(StaffUser.username == "admin"))
    sam = db.scalar(select(StaffUser).where(StaffUser.username == "sam"))
    client.post("/admin/users", data={"csrf_token": tok, "action": "set_roles", "id": sam.id, "roles": ["scheduler"]})
    db.expire_all()
    assert db.get(StaffUser, sam.id).roles == ["scheduler"]
    # the only admin can't strip their own admin role or be deactivated
    client.post("/admin/users", data={"csrf_token": tok, "action": "set_roles", "id": admin.id, "roles": ["monitor"]})
    client.post("/admin/users", data={"csrf_token": tok, "action": "deactivate", "id": admin.id})
    db.expire_all()
    a = db.get(StaffUser, admin.id)
    assert a.active and "admin" in staffauth.roles_of(a)


def test_users_page_lists_roles_for_admin(client, demo):
    staff_login(client, "admin")
    html = client.get("/admin/users").text
    for label in ("Floor supervisor", "Part reviewer", "Trainer", "Scheduler"):
        assert label.lower() in html.lower()


# ---------------------------------------------------------------- emailed temporary password
def _create(client, tok, **kw):
    data = {"csrf_token": tok, "action": "create", "username": "newhire", "email": "newhire@example.edu",
            "roles": ["floor_supervisor"], **kw}
    return client.post("/admin/users", data=data, follow_redirects=False)


def test_new_user_gets_emailed_temp_password_and_must_change_it(client, db, demo):
    import re
    from app import emailer
    emailer.SENT.clear()
    tok = staff_login(client, "admin")
    assert _create(client, tok).status_code == 303
    from app.emailer import flush_outbox
    flush_outbox(db)
    db.commit()
    mail = [m for m in emailer.SENT if m["to"] == "newhire@example.edu"][-1]
    pw = re.search(r"Temporary password: (\S+)", mail["text"]).group(1)
    assert len(pw) >= 10 and "/admin/login" in mail["text"]
    u = db.scalar(select(StaffUser).where(StaffUser.username == "newhire"))
    assert u.must_change_password
    # temp password is not kept in the outbox after sending
    from app.models import EmailOutbox
    row = db.scalar(select(EmailOutbox).where(EmailOutbox.to_email == "newhire@example.edu"))
    assert pw not in row.body_text

    c = TestClient(create_app(), base_url="http://testserver")
    c.__enter__()
    try:
        staff_login(c, "newhire", pw)
        r = c.get("/admin/machines", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/admin/password"
        t = c.cookies.get("csrf")
        assert c.post("/admin/emergency", data={"csrf_token": t}, follow_redirects=False).headers["location"] == "/admin/password"
        # wrong current password / same password / mismatch are refused
        for data in ({"current": "nope", "password": "brand-new-pass-1", "confirm": "brand-new-pass-1"},
                     {"current": pw, "password": pw, "confirm": pw},
                     {"current": pw, "password": "brand-new-pass-1", "confirm": "different-pass-22"}):
            assert c.post("/admin/password", data={"csrf_token": t, **data}).status_code == 400
        r = c.post("/admin/password", data={"csrf_token": t, "current": pw, "password": "brand-new-pass-1",
                                            "confirm": "brand-new-pass-1"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/admin"
        assert c.get("/admin/machines", follow_redirects=False).status_code == 200
    finally:
        c.__exit__(None, None, None)
    db.expire_all()
    assert not db.scalar(select(StaffUser).where(StaffUser.username == "newhire")).must_change_password
    # the old temporary password no longer works
    c2 = TestClient(create_app(), base_url="http://testserver")
    c2.__enter__()
    try:
        t2 = csrf(c2, "/admin/login")
        assert c2.post("/admin/login", data={"csrf_token": t2, "username": "newhire", "password": pw}).status_code == 401
    finally:
        c2.__exit__(None, None, None)


def test_create_requires_email_and_admin_reset_emails_new_password(client, db, demo):
    from app import emailer
    tok = staff_login(client, "admin")
    r = client.post("/admin/users", data={"csrf_token": tok, "action": "create", "username": "noemail",
                                          "roles": ["monitor"]})
    assert r.status_code == 400
    emailer.SENT.clear()
    mo = db.scalar(select(StaffUser).where(StaffUser.username == "mo"))
    client.post("/admin/users", data={"csrf_token": tok, "action": "reset_password", "id": mo.id, "password": ""})
    from app.emailer import flush_outbox
    flush_outbox(db)
    db.commit()
    db.expire_all()
    assert db.get(StaffUser, mo.id).must_change_password
    assert any("Temporary password" in m["text"] and m["to"] == "mo@example.edu" for m in emailer.SENT)


# ---------------------------------------------------------------- supervisors clock in to shifts
def test_shift_clock_in_requires_the_shop_code_and_changes_who_is_on_duty(client, db, demo):
    from datetime import timedelta

    from app import clock
    from app.models import ShiftSchedule
    from app.services import rotcode, shifts, shop
    sam = db.scalar(select(StaffUser).where(StaffUser.username == "sam"))
    now = clock.now()
    db.add(ShiftSchedule(staff_id=sam.id, start_at=now - timedelta(minutes=5), end_at=now + timedelta(hours=4)))
    db.commit()
    assert shop.supervisor_scheduled(db, now).username == "sam" and shop.supervisor_on_duty(db, now) is None
    from app.errors import UserError
    with pytest.raises(UserError):
        shifts.clock_in(db, sam, "0000" if rotcode.current(db)["code"] != "0000" else "1111", "1.1.1.1")
    db.rollback()
    shifts.clock_in(db, sam, rotcode.current(db)["code"], "1.1.1.1")
    db.commit()
    assert shop.supervisor_on_duty(db, clock.now()).username == "sam"
    shifts.clock_out(db, sam)
    db.commit()
    assert shop.supervisor_on_duty(db, clock.now() + timedelta(seconds=1)) is None


def test_missing_clock_in_alerts_admins_once_and_dashboard_form_works(client, db, demo):
    from datetime import timedelta
    from app import clock, emailer
    from app.models import ShiftSchedule
    from app.services import rotcode, shifts
    sam = db.scalar(select(StaffUser).where(StaffUser.username == "sam"))
    now = clock.now()
    db.add(ShiftSchedule(staff_id=sam.id, start_at=now - timedelta(minutes=30), end_at=now + timedelta(hours=3)))
    db.commit()
    emailer.SENT.clear()
    assert shifts.alert_missing(db, now) >= 1 and shifts.alert_missing(db, now) == 0
    db.commit()
    emailer.flush_outbox(db)
    assert any("clock in" in m["subject"].lower() for m in emailer.SENT)
    c, tok = login_as("sam")
    try:
        assert "Clock in to your shift" in c.get("/admin").text
        bad = c.post("/admin/shift/clock-in", data={"csrf_token": tok, "code": "abc"}, follow_redirects=False)
        assert bad.status_code == 400
        ok = c.post("/admin/shift/clock-in", data={"csrf_token": tok, "code": rotcode.current(db)["code"]}, follow_redirects=False)
        assert ok.status_code == 303
        assert "You are clocked in" in c.get("/admin").text
    finally:
        c.__exit__(None, None, None)
    m, tok = login_as("mo")
    try:
        assert m.post("/admin/shift/clock-in", data={"csrf_token": tok, "code": "1234"}).status_code == 403
    finally:
        m.__exit__(None, None, None)


def test_403_page_go_back_is_a_real_link(client, demo):
    c, tok = login_as("mo")
    try:
        r = c.post("/admin/emergency", data={"csrf_token": tok}, headers={"referer": "http://testserver/admin/machines"})
        assert r.status_code == 403
        assert 'href="/admin/machines"' in r.text and "javascript:" not in r.text
        r = c.post("/admin/emergency", data={"csrf_token": tok})
        assert 'href="/admin"' in r.text
    finally:
        c.__exit__(None, None, None)


# ---------------------------------------------------------------- safety holds
def _mach(db, name):
    from tests.factories import machine
    return machine(db, name)


def test_scheduler_cannot_touch_any_safety_hold(client, db, demo):
    m = _mach(db, "Haas Mini Mill 1")
    c, tok = login_as("sky")
    try:
        for path, data in ((f"/admin/machines/{m.id}/override", {"status": "available", "reason": "x y z"}),
                           (f"/admin/machines/{m.id}/maintenance", {"kind": "issue", "description": "abc", "state": "none"}),
                           (f"/admin/machines/{m.id}/clear", {}),
                           ("/admin/maintenance/1/close", {}), ("/admin/holds/1/release", {}),
                           (f"/admin/hold-place/{m.id}", {}), ("/admin/emergency", {"action": "reopen"}),
                           ("/admin/damage/1/status", {})):
            assert c.post(path, data={"csrf_token": tok, **data}).status_code == 403, path
    finally:
        c.__exit__(None, None, None)


def test_floor_supervisor_cannot_lift_a_maintenance_hold_but_maintenance_can(client, db, demo):
    from app.models import Machine
    m = _mach(db, "Haas Mini Mill 1")
    mx, tx = login_as("max")
    fr, tf = login_as("fran")
    try:
        mx.post(f"/admin/machines/{m.id}/maintenance", data={"csrf_token": tx, "kind": "issue", "description": "spindle noise",
                                                              "state": "under_maintenance"})
        mx.post(f"/admin/machines/{m.id}/override", data={"csrf_token": tx, "status": "under_maintenance", "reason": "spindle noise"})
        db.expire_all()
        assert db.get(Machine, m.id).maintenance_state == "under_maintenance" and db.get(Machine, m.id).override_by == "max"
        fr.post(f"/admin/machines/{m.id}/clear", data={"csrf_token": tf, "note": "looks fine"})
        fr.post(f"/admin/machines/{m.id}/override", data={"csrf_token": tf, "status": "", "reason": ""})
        db.expire_all()
        mm = db.get(Machine, m.id)
        assert mm.maintenance_state == "under_maintenance" and mm.override_status == "under_maintenance"
        mx.post(f"/admin/machines/{m.id}/override", data={"csrf_token": tx, "status": "", "reason": ""})
        mx.post(f"/admin/machines/{m.id}/clear", data={"csrf_token": tx, "note": "fixed"})
        db.expire_all()
        mm = db.get(Machine, m.id)
        assert mm.maintenance_state == "none" and mm.override_status is None
        # a floor supervisor can place a hold and lift their own
        fr.post(f"/admin/machines/{m.id}/override", data={"csrf_token": tf, "status": "on_hold", "reason": "coolant spill"})
        fr.post(f"/admin/machines/{m.id}/override", data={"csrf_token": tf, "status": "", "reason": ""})
        db.expire_all()
        assert db.get(Machine, m.id).override_status is None
    finally:
        mx.__exit__(None, None, None)
        fr.__exit__(None, None, None)


# ---------------------------------------------------------------- machine-specific training authorization
def test_trainer_can_only_certify_on_authorized_machines(client, db, demo):
    from app.models import Certification, Machine
    alex = student(db, "alex")
    m1, vf3 = _mach(db, "Haas Mini Mill 1"), _mach(db, "Haas VF-3")
    for old in db.scalars(select(Certification).where(Certification.student_id == alex.id)).all():
        db.delete(old)
    db.commit()
    t, tt = login_as("tara")
    adm_tok = staff_login(client, "admin")
    tara = db.scalar(select(StaffUser).where(StaffUser.username == "tara"))

    def grant(target):
        t.post(f"/admin/students/{alex.id}/cert", data={"csrf_token": tt, "target": target})
        db.expire_all()
        return db.scalars(select(Certification).where(Certification.student_id == alex.id)).all()

    try:
        assert grant(f"m:{m1.id}") == []  # no authorization yet
        client.post("/admin/users", data={"csrf_token": adm_tok, "action": "set_training", "id": tara.id, "machines": [m1.id]})
        assert [c.machine_id for c in grant(f"m:{m1.id}")] == [m1.id]
        assert [c.machine_id for c in grant(f"m:{vf3.id}")] == [m1.id]  # still not allowed on the VF-3
        # a group authorization covers every machine in that group
        grp = db.get(Machine, vf3.id).group_id
        client.post("/admin/users", data={"csrf_token": adm_tok, "action": "set_training", "id": tara.id, "groups": [grp]})
        assert vf3.id in [c.machine_id for c in grant(f"m:{vf3.id}")]
        # revoking outside the scope is refused
        client.post("/admin/users", data={"csrf_token": adm_tok, "action": "set_training", "id": tara.id, "machines": []})
        cert = db.scalars(select(Certification).where(Certification.student_id == alex.id)).first()
        t.post(f"/admin/certs/{cert.id}/revoke", data={"csrf_token": tt})
        db.expire_all()
        assert db.get(Certification, cert.id).revoked_at is None
        # the student page offers no targets
        assert "not authorized to certify" in t.get(f"/admin/students/{alex.id}").text
    finally:
        t.__exit__(None, None, None)


def test_lead_supervisor_and_admin_can_certify_anywhere(client, db, demo):
    from app.models import Certification
    alex = student(db, "alex")
    vf3 = _mach(db, "Haas VF-3")
    c, tok = login_as("sam")
    try:
        c.post(f"/admin/students/{alex.id}/cert", data={"csrf_token": tok, "target": f"m:{vf3.id}"})
        db.expire_all()
        assert db.scalars(select(Certification).where(Certification.student_id == alex.id)).all()
    finally:
        c.__exit__(None, None, None)


# ---------------------------------------------------------------- deleting staff, keeping history
def test_delete_user_keeps_their_history_and_logs_the_deletion(client, db, demo):
    from app.models import AuditLog, ShiftSchedule
    from app.services import audit
    tok = staff_login(client, "admin")
    sam = db.scalar(select(StaffUser).where(StaffUser.username == "sam"))
    audit.record(db, "staff:sam", "machine.override", "machine", 1, after={"status": "on_hold"})
    sid = sam.id
    db.commit()
    # wrong confirmation / self / last admin are refused
    assert "Type the username" in client.post("/admin/users", data={"csrf_token": tok, "action": "delete", "id": sid,
                                                                    "confirm": "nope"}).text
    admin = db.scalar(select(StaffUser).where(StaffUser.username == "admin"))
    assert client.post("/admin/users", data={"csrf_token": tok, "action": "delete", "id": admin.id, "confirm": "admin"}).status_code == 400
    r = client.post("/admin/users", data={"csrf_token": tok, "action": "delete", "id": sid, "confirm": "sam"}, follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    assert db.get(StaffUser, sid) is None
    assert not db.scalars(select(ShiftSchedule).where(ShiftSchedule.staff_id == sid)).all()
    assert db.scalar(select(AuditLog).where(AuditLog.actor == "staff:sam", AuditLog.action == "machine.override"))  # history kept
    gone = db.scalar(select(AuditLog).where(AuditLog.action == "staff.delete"))
    assert gone.actor == "staff:admin" and gone.before["username"] == "sam" and "supervisor" in gone.before["roles"]
    # the name can't be reused, so old entries never get attributed to someone new
    r = client.post("/admin/users", data={"csrf_token": tok, "action": "create", "username": "sam", "email": "x@example.edu",
                                          "roles": ["monitor"]})
    assert r.status_code == 400 and "deleted account" in r.text
    assert "staff.delete" in client.get("/admin/audit?q=staff.delete").text
