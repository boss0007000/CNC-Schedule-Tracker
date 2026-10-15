import re

from app import emailer


def csrf(client, path="/"):
    """Prime the CSRF cookie and return the token."""
    client.get(path)
    return client.cookies.get("csrf")


def last_code():
    for m in reversed(emailer.SENT):
        r = re.search(r"code is (\d{6})", m["text"])
        if r:
            return r.group(1)
    raise AssertionError("no code email sent")


def verify_student(client, email="alex.rivera@csuchico.edu", name="Alex Rivera", standing="junior"):
    tok = csrf(client, "/verify")
    r = client.post("/verify", data={"csrf_token": tok, "email": email, "full_name": name,
                                     "standing": standing, "next": "/"})
    assert r.status_code == 200, r.text
    r = client.post("/verify/code", data={"csrf_token": tok, "email": email, "code": last_code(), "next": "/"},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    return tok


def staff_login(client, username="admin", password="demo-password-123"):
    tok = csrf(client, "/admin/login")
    r = client.post("/admin/login", data={"csrf_token": tok, "username": username, "password": password},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    return tok
