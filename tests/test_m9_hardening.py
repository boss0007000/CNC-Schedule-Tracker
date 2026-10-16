"""Security + accessibility pass: route-enumerating checks so new endpoints can't silently skip protections."""
import re
from html.parser import HTMLParser

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import clock, security
from app.config import Config, get_config
from app.main import create_app
from app.models import Approval, Student
from tests.factories import at, book, machine, student
from tests.helpers import csrf, staff_login, verify_student


def concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "1", path).replace("/1.csv", "/students.csv").replace("/1.png", "/submit.png").replace("/1.pdf", "/submit.pdf")


def routes(app):
    """All endpoints, including those from included routers (FastAPI nests them lazily)."""
    out = []
    for r in app.routes:
        if isinstance(r, APIRoute):
            out.append(r)
        elif hasattr(r, "original_router"):
            out += [x for x in r.original_router.routes if isinstance(x, APIRoute)]
    return out


# ------------------------------------------------------------------ CSRF everywhere
def test_every_post_endpoint_rejects_missing_or_forged_csrf(client):
    csrf(client, "/")
    posts = [r for r in routes(client.app) if "POST" in r.methods]
    assert len(posts) > 30
    for r in posts:
        path = concrete(r.path)
        for data in ({}, {"csrf_token": "forged"}):
            resp = client.post(path, data=data, follow_redirects=False)
            assert resp.status_code == 403, f"POST {r.path} accepted a request without a valid CSRF token ({resp.status_code})"


# ------------------------------------------------------------------ authentication everywhere
def test_every_admin_route_requires_staff_login(client):
    tok = csrf(client, "/admin/login")
    admin = [r for r in routes(client.app) if r.path.startswith("/admin") and r.path not in ("/admin/login", "/admin/logout")]
    assert len(admin) > 40
    for r in admin:
        for m in r.methods - {"HEAD", "OPTIONS"}:
            resp = client.request(m, concrete(r.path), data={"csrf_token": tok} if m == "POST" else None, follow_redirects=False)
            assert resp.status_code == 303 and "/admin/login" in resp.headers["location"], f"{m} {r.path} -> {resp.status_code}"


def test_student_pages_require_verification(client):
    for path in ("/submit", "/reserve", "/checkin", "/checkout", "/my", "/submit/1", "/checkin/done/1", "/checkout/done/1"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and "/verify" in r.headers["location"], path


def test_public_endpoints_are_limited_to_the_expected_set(client):
    public = {"/", "/healthz", "/verify", "/tv", "/tv/data", "/qr/{name}.png", "/approval/{number}", "/approval/{number}/pdf",
              "/reservation/{ref}", "/admin/login"}
    gets = [r.path for r in routes(client.app) if "GET" in r.methods]
    for p in gets:
        if p.startswith("/static"):
            continue
        resp = client.get(concrete(p), follow_redirects=False)
        if resp.status_code in (200,) and p not in public:
            pytest.fail(f"GET {p} is publicly readable but not on the allow-list")


# ------------------------------------------------------------------ roles
def test_role_matrix(demo):
    clients = {}
    for user in ("mo", "sam", "admin"):
        c = TestClient(create_app(), base_url="http://testserver")
        c.__enter__()
        c.tok = staff_login(c, user)
        clients[user] = c

    def req(user, method, path):
        c = clients[user]
        return c.request(method, path, data={"csrf_token": c.tok} if method == "POST" else None, follow_redirects=False).status_code
    try:
        for path in ("/admin/users", "/admin/audit", "/admin/settings", "/admin/backups", "/admin/export/audit_log.csv"):
            assert req("mo", "GET", path) == 403 and req("sam", "GET", path) == 403 and req("admin", "GET", path) == 200, path
        for path in ("/admin/approvals", "/admin/reservations", "/admin/machines", "/admin/reports", "/admin/qr"):
            assert req("mo", "GET", path) == 200 and req("sam", "GET", path) == 200, path
        assert req("mo", "POST", "/admin/reservations/1/cancel") == 403
        assert req("sam", "POST", "/admin/machines/save") == 403  # machine CRUD is admin-only
    finally:
        for c in clients.values():
            c.__exit__(None, None, None)


# ------------------------------------------------------------------ XSS / injection
def test_user_content_is_escaped_in_admin_pages(client, db, demo):
    payload = "<script>alert(1)</script>"
    tok = verify_student(client, "xss@csuchico.edu", "X Y")
    d = {"csrf_token": tok, "form_token": security.new_form_token(), "title": payload + " plate", "description": payload * 2,
         "machine_ids": [str(machine(db, "Haas VF-3").id)], "estimated_hours": "2", "materials": payload}
    assert client.post("/submit", data=d, follow_redirects=False).status_code == 303
    with TestClient(create_app(), base_url="http://testserver") as sc:
        staff_login(sc, "sam")
        for path in ("/admin/approvals", "/admin/approvals/" + str(db.scalar(select(Approval.project_id).limit(1)) and 3)):
            body = sc.get(path).text
            assert payload not in body
    page = client.get("/my").text
    assert payload not in page and "&lt;script&gt;" in page


def test_sql_metacharacters_in_search_are_harmless(demo):
    with TestClient(create_app(), base_url="http://testserver") as sc:
        staff_login(sc, "sam")
        for q in ("' OR 1=1 --", "%", "_", "'; DROP TABLE students; --"):
            assert sc.get("/admin/students", params={"q": q}).status_code == 200
            assert sc.get("/admin/reservations", params={"q": q}).status_code == 200
        assert "Alex Rivera" in sc.get("/admin/students").text


# ------------------------------------------------------------------ IDOR
def test_students_cannot_read_each_others_records(client, db, demo):
    alex = student(db, "alex")
    a = db.scalar(select(Approval).where(Approval.student_id == alex.id))
    r = book(db, alex, a, "Haas Mini Mill 1", at(1, 10))
    db.commit()
    verify_student(client, "jordan.lee@csuchico.edu", "Jordan Lee", "senior")
    assert client.get(f"/reservation/{r.ref}").status_code == 404
    assert client.get(f"/approval/{a.number}").status_code == 404
    assert client.get(f"/approval/{a.number}/pdf").status_code == 404
    from app.models import Project
    pid = db.scalar(select(Project.id).where(Project.student_id == alex.id))
    assert client.get(f"/submit/{pid}").status_code == 404
    assert client.get(f"/submit/{pid}/revise").status_code == 404


# ------------------------------------------------------------------ headers, cookies, config
def test_cookies_are_httponly_and_samesite(client, demo):
    tok = csrf(client, "/admin/login")
    r = client.post("/admin/login", data={"csrf_token": tok, "username": "admin", "password": "demo-password-123"},
                    follow_redirects=False)
    sc = r.headers["set-cookie"].lower()
    assert "httponly" in sc and "samesite=lax" in sc
    assert "httponly" in client.get("/verify").headers.get("set-cookie", "httponly").lower()


def test_pages_are_not_cacheable_and_have_security_headers(client):
    for path in ("/", "/verify", "/tv", "/tv/data", "/admin/login"):
        r = client.get(path)
        assert r.headers["cache-control"] == "no-store", path
        assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
        csp = r.headers["content-security-policy"]
        assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert "max-age" in client.get("/qr/submit.png").headers.get("cache-control", "max-age")


def test_https_redirect_and_hsts(monkeypatch):
    monkeypatch.setenv("FORCE_HTTPS", "true")
    get_config.cache_clear()
    try:
        with TestClient(create_app(), base_url="http://testserver") as c:
            r = c.get("/verify", follow_redirects=False)
            assert r.status_code == 308 and r.headers["location"].startswith("https://")
            ok = c.get("/verify", headers={"x-forwarded-proto": "https"})
            assert ok.status_code == 200 and "strict-transport-security" in ok.headers
            assert c.get("/healthz").status_code == 200  # health checks stay on http
    finally:
        get_config.cache_clear()


def test_prod_refuses_unsafe_configuration():
    bad = Config(env="prod", secret_key="dev-insecure-secret-change-me", base_url="http://x", database_url="sqlite:///x.db",
                 email_provider="console")
    with pytest.raises(RuntimeError) as e:
        bad.validate_for_prod()
    for frag in ("SECRET_KEY", "https://", "Postgres", "EMAIL_PROVIDER"):
        assert frag in str(e.value)
    good = Config(env="prod", secret_key="x" * 40, base_url="https://shop.example.edu",
                  database_url="postgresql://u:p@h/db", email_provider="brevo")
    good.validate_for_prod()
    assert good.secure_cookies and good.sqlalchemy_url.startswith("postgresql+psycopg://")


def test_postgres_url_normalisation():
    assert Config(database_url="postgres://u:p@h/db").sqlalchemy_url == "postgresql+psycopg://u:p@h/db"


def test_oversized_request_rejected(client):
    r = client.post("/verify", content=b"x", headers={"content-length": str(70 * 1024 * 1024), "content-type": "text/plain"})
    assert r.status_code == 413


def test_error_pages_do_not_leak_internals(client, monkeypatch):
    from app.routers import public
    def boom(*a, **k):
        raise RuntimeError("secret database password")
    monkeypatch.setattr(public, "render", boom)
    with TestClient(client.app, base_url="http://testserver", raise_server_exceptions=False) as c:
        r = c.get("/")
        assert r.status_code == 500 and "secret" not in r.text and "Traceback" not in r.text


def test_secret_key_signs_tokens_not_guessable():
    t = security.sign("student-session", {"email": "a@csuchico.edu"})
    assert security.unsign("staff-session", t, 3600) is None  # salts separate token types
    assert security.unsign("student-session", t, 3600) == {"email": "a@csuchico.edu"}


# ------------------------------------------------------------------ accessibility
class A11y(HTMLParser):
    def __init__(self):
        super().__init__()
        self.inputs, self.labels_for, self.h1, self.imgs_noalt, self.inline, self.lang = [], set(), 0, 0, [], None
        self.in_label = 0
        self.titles = 0

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "html":
            self.lang = a.get("lang")
        if tag == "h1":
            self.h1 += 1
        if tag == "title":
            self.titles += 1
        if tag == "label":
            self.in_label += 1
            if a.get("for"):
                self.labels_for.add(a["for"])
        if tag in ("input", "select", "textarea") and a.get("type") not in ("hidden", "submit"):
            self.inputs.append((a, self.in_label > 0))
        if tag == "img" and "alt" not in a:
            self.imgs_noalt += 1
        if "style" in a or any(k.startswith("on") for k in a):
            self.inline.append((tag, [k for k in a if k == "style" or k.startswith("on")]))

    def handle_endtag(self, tag):
        if tag == "label":
            self.in_label -= 1


def audit_html(html: str, name: str):
    p = A11y()
    p.feed(html)
    assert p.lang == "en", f"{name}: missing lang"
    assert p.titles == 1, f"{name}: needs exactly one <title>"
    assert p.h1 >= 1, f"{name}: missing h1"
    assert not p.imgs_noalt, f"{name}: img without alt"
    assert not p.inline, f"{name}: inline style/handler blocked by CSP: {p.inline}"
    for a, wrapped in p.inputs:
        labelled = wrapped or a.get("id") in p.labels_for or a.get("aria-label") or a.get("aria-labelledby")
        assert labelled, f"{name}: unlabelled control {a}"


def test_pages_pass_accessibility_checks(client, db, demo):
    for path in ("/", "/verify", "/tv", "/admin/login"):
        audit_html(client.get(path).text, path)
    tok = verify_student(client, "alex.rivera@csuchico.edu", "Alex Rivera")
    day = clock.to_local(at(1, 10)).date()
    a = db.scalar(select(Approval).where(Approval.number == "APR-2026-0001"))
    for path in ("/submit", "/reserve", f"/reserve?approval={a.number}&machine=1&date={day}", "/checkin", "/checkout", "/my", "/schedule", "/machine/1"):
        audit_html(client.get(path).text, path)
    with TestClient(create_app(), base_url="http://testserver") as sc:
        staff_login(sc, "admin")
        for path in ("/admin", "/admin/approvals", "/admin/approvals/1", "/admin/reservations", "/admin/reservations?view=calendar",
                     "/admin/sessions", "/admin/machines", "/admin/machines/1", "/admin/students", "/admin/students/1",
                     "/admin/incidents", "/admin/reports", "/admin/qr", "/admin/settings", "/admin/users", "/admin/audit",
                     "/admin/backups", "/admin/access", "/admin/requests"):
            audit_html(sc.get(path).text, path)


def test_tv_is_readable_from_ten_feet_and_not_colour_only():
    css = open("app/static/tv.css", encoding="utf-8").read()
    js = open("app/static/tv.js", encoding="utf-8").read()
    assert "--u:min(1vh,1.7778vw)" in css and css.count("var(--u)") > 100  # scales with height OR width, so any window shape works
    assert "m.label" in js and "'badge'" in js  # every card prints the status as text
    assert "animation:drift" in css  # burn-in protection
    assert "Overdue by" in js and "Time left" in js and "timeline" in js
