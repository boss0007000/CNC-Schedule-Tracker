"""Regression: every form must carry the REAL security token in the page itself (not just in the cookie).
An earlier version rendered empty tokens inside macros, so every student form failed with 403 in a browser."""
import re

import pytest
from sqlalchemy import select

from app import clock, security
from app.models import Approval
from tests.factories import at, book, machine, student
from tests.helpers import verify_student


def page_tokens(html: str) -> list[str]:
    return re.findall(r'name="csrf_token" value="([^"]*)"', html)


def browser_post(client, path_get, path_post, fields, **kw):
    """Behave like a browser: read the token out of the rendered form, not from the cookie jar."""
    html = client.get(path_get).text
    toks = page_tokens(html)
    assert toks and all(t for t in toks), f"{path_get}: empty csrf token in form {toks}"
    assert toks[0] == client.cookies.get("csrf")
    form_tokens = re.findall(r'name="form_token" value="([^"]*)"', html)
    data = {"csrf_token": toks[0], **({"form_token": form_tokens[0]} if form_tokens else {}), **fields}
    return client.post(path_post, data=data, **kw)


def test_login_works_exactly_like_a_browser(client, db, demo):
    from app import emailer
    r = browser_post(client, "/verify", "/verify", {"email": "new.student@csuchico.edu", "full_name": "New Student",
                                                    "standing": "junior", "next": "/"})
    assert r.status_code == 200 and "Enter your code" in r.text
    code = re.search(r"code is (\d{6})", emailer.SENT[-1]["text"]).group(1)
    r = browser_post(client, "/verify", "/verify/code", {"email": "new.student@csuchico.edu", "code": code, "next": "/"},
                     follow_redirects=False)
    assert r.status_code == 303  # that is the page the student sees after the code


def test_every_student_form_page_carries_a_real_token(client, db, demo):
    verify_student(client, "alex.rivera@csuchico.edu", "Alex Rivera")
    alex = student(db, "alex")
    a = db.scalar(select(Approval).where(Approval.student_id == alex.id))
    r = book(db, alex, a, "Haas Mini Mill 1", at(1, 10))
    db.commit()
    day = clock.to_local(at(1, 10)).date()
    pages = ["/submit", "/checkin", "/checkout", "/my", f"/reservation/{r.ref}",
             f"/reserve?approval={a.number}&machine={machine(db, 'Haas Mini Mill 1').id}&date={day}"]
    for p in pages:
        html = client.get(p).text
        if "<form" in html and 'method="post"' in html:
            toks = page_tokens(html)
            assert toks and all(t == client.cookies.get("csrf") for t in toks), f"{p}: bad/empty token {toks}"
            ft = re.findall(r'name="form_token" value="([^"]*)"', html)
            assert all(ft), f"{p}: empty form_token"


def test_reserve_works_exactly_like_a_browser(client, db, demo):
    verify_student(client, "alex.rivera@csuchico.edu", "Alex Rivera")
    alex = student(db, "alex")
    a = db.scalar(select(Approval).where(Approval.student_id == alex.id))
    m = machine(db, "Haas Mini Mill 1")
    day = clock.to_local(at(1, 10)).date()
    r = browser_post(client, f"/reserve?approval={a.number}&machine={m.id}&date={day}", "/reserve",
                     {"approval": a.number, "machine_id": str(m.id), "start": at(1, 10).isoformat(), "nslots": "1",
                      "action": "book"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/reservation/")


def test_admin_login_works_exactly_like_a_browser(client, db, demo):
    r = browser_post(client, "/admin/login", "/admin/login", {"username": "admin", "password": "demo-password-123"},
                     follow_redirects=False)
    assert r.status_code == 303
