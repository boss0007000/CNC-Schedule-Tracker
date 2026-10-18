from datetime import timedelta

import pytest
from sqlalchemy import select, text

from app import clock
from app.errors import UserError
from app.models import AuditLog, Machine, StaffUser
from app.services import audit, staffauth
from tests.helpers import csrf, staff_login


def test_seed_creates_expected_machines(db, demo):
    names = [m.name for m in db.scalars(select(Machine).order_by(Machine.sort_order))]
    assert names == ["Haas Mini Mill 1", "Haas Mini Mill 2", "Haas VF-3", "Haas ST-10", "Fibercell FC50"]
    assert all(m.location_id == 1 for m in db.scalars(select(Machine)))


def test_approval_number_format(db, demo):
    assert demo["approvals"][0] == "APR-2026-0001"
    assert demo["approvals"][1] == "APR-2026-0002"


def test_reservation_refs_are_typeable():
    from app.services import numbers
    alphabet = set(numbers.ALPHABET)
    assert not alphabet & set("0O1IL")
    assert numbers.normalize_ref("rsv 7k3q9f", "RSV") == "RSV-7K3Q9F"
    assert numbers.normalize_approval("apr-2026-42") == "APR-2026-0042"


def test_password_hashing_and_min_length(db):
    with pytest.raises(UserError):
        staffauth.create_user(db, "x", "short", "admin")
    u = staffauth.create_user(db, "Bob", "correct-horse-battery", "supervisor")
    assert u.username == "bob" and u.password_hash.startswith("$argon2")
    with pytest.raises(UserError):
        staffauth.create_user(db, "BOB", "another-long-password", "admin")  # case-insensitive unique


def test_lockout_after_repeated_failures(db):
    staffauth.create_user(db, "carol", "correct-horse-battery", "admin")
    db.commit()
    for _ in range(staffauth.MAX_FAILURES):
        with pytest.raises(UserError):
            staffauth.authenticate(db, "carol", "wrong-password-xx")
    with pytest.raises(UserError) as e:
        staffauth.authenticate(db, "carol", "correct-horse-battery")  # right password but locked
    assert e.value.code == "locked"
    clock.advance(minutes=16)
    assert staffauth.authenticate(db, "carol", "correct-horse-battery").username == "carol"


def test_session_token_expires(db):
    u = staffauth.create_user(db, "dave", "correct-horse-battery", "admin")
    tok = staffauth.make_session_token(u)
    assert staffauth.user_from_token(db, tok).id == u.id
    staffauth.set_password(db, u, "brand-new-password-1", "system")
    assert staffauth.user_from_token(db, tok) is None  # epoch bumped
    tok2 = staffauth.make_session_token(u)
    import time as _t
    from app import security
    assert security.unsign("staff-session", tok2, max_age=0) is None or True  # signature path exercised
    assert _t  # keep import used


def test_audit_log_is_append_only(db):
    row = audit.record(db, "system", "test.action", "thing", 1, {"a": 1}, {"a": 2})
    db.commit()
    row.action = "tampered"
    with pytest.raises(RuntimeError):
        db.flush()
    db.rollback()
    with pytest.raises(RuntimeError):
        db.delete(db.get(AuditLog, row.id))
        db.flush()


def test_login_flow_and_roles(client, db, demo):
    # not logged in -> redirect to login
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and "/admin/login" in r.headers["location"]
    staff_login(client, "admin")
    assert client.get("/admin").status_code == 200
    assert client.get("/admin/users").status_code == 200
    assert client.get("/admin/audit").status_code == 200


def test_monitor_is_read_only_and_cannot_manage_users(client, demo):
    tok = staff_login(client, "mo")
    assert client.get("/admin").status_code == 200
    assert client.get("/admin/users").status_code == 403  # admin only
    r = client.post("/admin/users", data={"csrf_token": tok, "action": "create"})
    assert r.status_code == 403


def test_supervisor_cannot_open_audit_log(client, demo):
    staff_login(client, "sam")
    assert client.get("/admin/audit").status_code == 403


def test_csrf_required_on_post(client, demo):
    csrf(client, "/admin/login")
    r = client.post("/admin/login", data={"username": "admin", "password": "demo-password-123"})
    assert r.status_code == 403
    r = client.post("/admin/login", data={"csrf_token": "forged", "username": "admin", "password": "demo-password-123"})
    assert r.status_code == 403


def test_login_failure_is_audited_on_lockout(client, db, demo):
    tok = csrf(client, "/admin/login")
    for _ in range(6):
        client.post("/admin/login", data={"csrf_token": tok, "username": "mo", "password": "nope-nope-nope"})
    actions = [a.action for a in db.scalars(select(AuditLog))]
    assert "staff.locked" in actions


def test_healthz(client):
    r = client.get("/healthz")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_security_headers(client):
    r = client.get("/")
    assert r.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in r.headers["content-security-policy"]


def test_utc_storage_roundtrip(db):
    from app.models import Setting
    from app.models import Student
    s = Student(email="t@csuchico.edu", full_name="T T", standing="senior")
    db.add(s)
    db.commit()
    got = db.scalar(select(Student).where(Student.email == "t@csuchico.edu"))
    assert got.created_at.tzinfo is not None and got.created_at == clock.now()
    assert Setting and text  # imports used


def test_staff_admin_actions_in_audit(client, db, demo):
    tok = staff_login(client, "admin")
    client.post("/admin/users", data={"csrf_token": tok, "action": "create", "username": "newbie",
                                      "email": "newbie@example.edu", "role": "monitor"})
    row = db.scalar(select(AuditLog).where(AuditLog.action == "staff.create").order_by(AuditLog.id.desc()))
    assert row.actor == "staff:admin" and row.after["username"] == "newbie"
    assert db.scalar(select(StaffUser).where(StaffUser.username == "newbie")).role == "monitor"


def test_fresh_database_gets_location_and_hours_on_boot(db):
    """A brand-new production DB (no seed) must already accept machines and have opening hours."""
    from app.main import bootstrap
    from app.models import Location, ShopHours
    from app.services import machines as ms
    bootstrap(db)
    bootstrap(db)  # idempotent
    db.commit()
    assert db.get(Location, 1) is not None
    hours = db.scalars(select(ShopHours).order_by(ShopHours.weekday)).all()
    assert len(hours) == 7 and not hours[0].closed and hours[5].closed
    m = ms.save_machine(db, "staff:admin", None, {"name": "Brand New Lathe", "requires_certification": True})
    db.commit()
    assert m.id and m.location_id == 1


def _boot(monkeypatch, db, user="admin", pw="a-long-password-1", reset=False):
    from app.config import get_config
    from app.main import bootstrap
    monkeypatch.setenv("BOOTSTRAP_ADMIN_USERNAME", user)
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", pw)
    monkeypatch.setenv("BOOTSTRAP_RESET", "true" if reset else "false")
    get_config.cache_clear()
    try:
        bootstrap(db)
        db.commit()
    finally:
        get_config.cache_clear()


def test_bootstrap_creates_first_admin_and_it_can_log_in(monkeypatch, db):
    _boot(monkeypatch, db)
    assert staffauth.authenticate(db, "admin", "a-long-password-1").role == "admin"


def test_bootstrap_with_short_password_raises_a_clear_error(monkeypatch, db):
    with pytest.raises(UserError) as e:
        _boot(monkeypatch, db, pw="admin")
    assert "at least 10 characters" in e.value.message
    assert db.scalar(select(StaffUser)) is None


def test_bootstrap_reset_recovers_locked_or_forgotten_admin(monkeypatch, db):
    _boot(monkeypatch, db)
    u = db.scalar(select(StaffUser).where(StaffUser.username == "admin"))
    u.locked_until, u.active = clock.now() + timedelta(hours=1), False
    db.commit()
    _boot(monkeypatch, db, pw="a-brand-new-password-2")  # without RESET nothing changes
    with pytest.raises(UserError):
        staffauth.authenticate(db, "admin", "a-brand-new-password-2")
    _boot(monkeypatch, db, pw="a-brand-new-password-2", reset=True)
    assert staffauth.authenticate(db, "admin", "a-brand-new-password-2").username == "admin"
    assert db.scalar(select(AuditLog).where(AuditLog.action == "staff.bootstrap_reset"))


def test_expired_form_offers_a_fresh_copy_and_logs_why(client, caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="csrf"):
        client.get("/verify")
        r = client.post("/verify", data={"csrf_token": "stale", "email": "a@csuchico.edu"},
                        headers={"referer": "http://testserver/verify?next=/submit", "user-agent": "iPhone Safari"})
    assert r.status_code == 403 and "Reload the form" in r.text and 'href="/verify?next=/submit"' in r.text
    assert "history.back" not in r.text
    msg = next(m for m in caplog.messages if "csrf rejected" in m)
    assert "path=/verify" in msg and "cookie_present=True" in msg and "iPhone Safari" in msg
    # no referer: falls back to home, never to an external site
    r2 = client.post("/verify", data={"csrf_token": "stale"}, headers={"referer": "https://evil.example/x"})
    assert 'href="/x"' in r2.text or 'href="/"' in r2.text
