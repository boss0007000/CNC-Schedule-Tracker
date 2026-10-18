"""Permission levels: each staff role may do only what it is for; enforced on every admin write route."""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import AuditLog, StaffUser
from app.services import staffauth
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
            and r.path not in ("/admin/login", "/admin/logout")]


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
                                          "password": "very-long-password", "roles": ["trainer", "part_reviewer"]},
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
