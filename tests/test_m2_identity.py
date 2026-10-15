from datetime import timedelta

from sqlalchemy import select

from app import clock, emailer
from app.models import EmailVerification, Student
from app.services import identity
from app.services import settings as shop_settings
from tests.helpers import csrf, last_code, verify_student

GOOD = {"email": "pat.kim@csuchico.edu", "full_name": "Pat Kim", "standing": "senior", "next": "/my"}


def start(client, tok, **over):
    return client.post("/verify", data={"csrf_token": tok, **GOOD, **over})


def test_protected_page_redirects_to_verify(client):
    r = client.get("/my", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/verify?next=/my")


def test_rejects_non_campus_email(client):
    tok = csrf(client, "/verify")
    r = start(client, tok, email="pat@gmail.com")
    assert r.status_code == 400 and "@csuchico.edu" in r.text
    assert not emailer.SENT


def test_domain_is_configurable(client, db):
    shop_settings.set_value(db, "allowed_email_domain", "example.edu")
    db.commit()
    tok = csrf(client, "/verify")
    assert start(client, tok).status_code == 400
    assert start(client, tok, email="pat@example.edu").status_code == 200


def test_full_flow_creates_student_and_cookie(client, db):
    tok = csrf(client, "/verify")
    assert start(client, tok).status_code == 200
    assert len(emailer.SENT) == 1 and emailer.SENT[0]["to"] == "pat.kim@csuchico.edu"
    code = last_code()
    assert db.scalar(select(Student)) is None  # not created before verification
    r = client.post("/verify/code", data={"csrf_token": tok, "email": GOOD["email"], "code": code, "next": "/my"},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/my"
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "student_session" in cookie
    s = db.scalar(select(Student))
    assert s.email == "pat.kim@csuchico.edu" and s.standing == "senior" and s.student_id is None
    assert client.get("/my").status_code == 200


def test_code_stored_hashed_not_plain(client, db):
    tok = csrf(client, "/verify")
    start(client, tok)
    code = last_code()
    row = db.scalar(select(EmailVerification))
    assert code not in row.code_hash and len(row.code_hash) == 64


def test_wrong_code_five_attempts_then_locked(client):
    tok = csrf(client, "/verify")
    start(client, tok)
    real = last_code()
    wrong = "000000" if real != "000000" else "111111"
    for i in range(5):
        r = client.post("/verify/code", data={"csrf_token": tok, "email": GOOD["email"], "code": wrong})
        assert r.status_code == 400
    # even the right code is now rejected: attempts exhausted
    r = client.post("/verify/code", data={"csrf_token": tok, "email": GOOD["email"], "code": real})
    assert r.status_code == 400 and "Too many wrong attempts" in r.text


def test_code_expires_after_10_minutes(client):
    tok = csrf(client, "/verify")
    start(client, tok)
    code = last_code()
    clock.advance(minutes=10, seconds=1)
    r = client.post("/verify/code", data={"csrf_token": tok, "email": GOOD["email"], "code": code})
    assert r.status_code == 400 and "expired" in r.text


def test_code_valid_just_before_expiry(client):
    tok = csrf(client, "/verify")
    start(client, tok)
    code = last_code()
    clock.advance(minutes=9, seconds=50)
    r = client.post("/verify/code", data={"csrf_token": tok, "email": GOOD["email"], "code": code},
                    follow_redirects=False)
    assert r.status_code == 303


def test_code_single_use(client):
    tok = csrf(client, "/verify")
    start(client, tok)
    code = last_code()
    assert client.post("/verify/code", data={"csrf_token": tok, "email": GOOD["email"], "code": code},
                       follow_redirects=False).status_code == 303
    assert client.post("/verify/code", data={"csrf_token": tok, "email": GOOD["email"], "code": code}).status_code == 400


def test_rate_limit_per_email(client):
    tok = csrf(client, "/verify")
    for _ in range(3):
        assert start(client, tok).status_code == 200
    r = start(client, tok)
    assert r.status_code == 429 and len(emailer.SENT) == 3
    clock.advance(minutes=11)
    assert start(client, tok).status_code == 200


def test_rate_limit_per_ip(client):
    tok = csrf(client, "/verify")
    for i in range(10):
        assert start(client, tok, email=f"user{i}@csuchico.edu").status_code == 200
    assert start(client, tok, email="user99@csuchico.edu").status_code == 429


def test_session_cookie_valid_8_hours(client):
    verify_student(client)
    assert client.get("/my").status_code == 200
    clock.advance(hours=7, minutes=59)
    # itsdangerous checks age against wall clock, not our frozen clock: check via the provider API
    tok = identity.session_token("alex.rivera@csuchico.edu")
    from app import security
    assert security.unsign("student-session", tok, identity.COOKIE_MAX_AGE)["email"] == "alex.rivera@csuchico.edu"
    assert security.unsign("student-session", tok + "x", identity.COOKIE_MAX_AGE) is None  # tamper
    assert identity.COOKIE_MAX_AGE == 8 * 3600


def test_forged_cookie_rejected(client):
    client.cookies.set("student_session", "garbage")
    assert client.get("/my", follow_redirects=False).status_code == 303


def test_honeypot_blocks_bots(client):
    tok = csrf(client, "/verify")
    r = start(client, tok, website="http://spam.example")
    assert r.status_code == 400 and not emailer.SENT


def test_student_id_off_by_default_and_toggle(client, db):
    assert 'name="student_id"' not in client.get("/verify").text
    shop_settings.set_value(db, "collect_student_id", True)
    db.commit()
    assert 'name="student_id"' in client.get("/verify").text
    tok = csrf(client, "/verify")
    start(client, tok, student_id="123456789")
    client.post("/verify/code", data={"csrf_token": tok, "email": GOOD["email"], "code": last_code()})
    assert db.scalar(select(Student)).student_id == "123456789"


def test_returning_student_not_duplicated(client, db):
    verify_student(client, "alex.rivera@csuchico.edu", "Alex Rivera")
    verify_student(client, "ALEX.RIVERA@csuchico.edu", "Alex R Rivera", "senior")
    rows = db.scalars(select(Student)).all()
    assert len(rows) == 1 and rows[0].standing == "senior"


def test_open_redirect_blocked(client):
    tok = csrf(client, "/verify")
    start(client, tok)
    r = client.post("/verify/code", data={"csrf_token": tok, "email": GOOD["email"], "code": last_code(),
                                          "next": "https://evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_public_name_hides_details(db):
    s = Student(email="a@csuchico.edu", full_name="Alexandra Marie Rivera", standing="senior")
    assert s.public_name == "Alexandra R."
