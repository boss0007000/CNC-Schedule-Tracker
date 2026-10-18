"""Per-machine QR codes: scan on the machine, tap Check in."""
import re

from sqlalchemy import select

from app import clock
from app.models import Approval, ShopSession
from app.services import qr, rotcode
from app.services import settings as shop_settings
from tests.factories import at, book, machine, student
from tests.helpers import staff_login, verify_student
from tests.test_m13_form_tokens import page_tokens


def _setup(client, db):
    verify_student(client, "alex.rivera@csuchico.edu", "Alex Rivera")
    alex = student(db, "alex")
    a = db.scalar(select(Approval).where(Approval.student_id == alex.id))
    m1, m2 = machine(db, "Haas Mini Mill 1"), machine(db, "Haas Mini Mill 2")
    r = book(db, alex, a, "Haas Mini Mill 1", at(0, 11))
    tok1, tok2 = qr.machine_token(db, m1), qr.machine_token(db, m2)
    db.commit()
    clock.freeze(at(0, 11, 5))
    return alex, r, m1, m2, tok1, tok2


def _post(client, token, ref, code=True):
    html = client.get(f"/m/{token}").text
    ft = re.findall(r'name="form_token" value="([^"]*)"', html)
    return client.post(f"/m/{token}/checkin", data={"csrf_token": client.cookies.get("csrf"), "ref": ref,
                                                    **({"shop_code": rotcode.current(client._db)["code"]} if code is True else
                                                       ({"shop_code": code} if code else {})),
                                                    **({"form_token": ft[0]} if ft else {})}, follow_redirects=False)


def test_scan_preselects_machine_and_checks_in_with_the_tv_code(client, db, demo):
    alex, r, m1, m2, tok1, tok2 = _setup(client, db)
    client._db = db
    html = client.get(f"/m/{tok1}").text
    assert "Check in to Haas Mini Mill 1" in html and r.ref in html and 'name="shop_code"' in html
    assert all(t == client.cookies.get("csrf") for t in page_tokens(html))
    resp = _post(client, tok1, r.ref)
    assert resp.status_code == 303 and resp.headers["location"].startswith("/checkin/done/")
    db.expire_all()
    s = db.scalar(select(ShopSession).where(ShopSession.student_id == alex.id))
    assert s.status == "active" and s.machine_id == m1.id
    assert "You're checked in" in client.get(f"/m/{tok1}").text


def test_wrong_machine_scan_cannot_check_in_the_reservation(client, db, demo):
    alex, r, m1, m2, tok1, tok2 = _setup(client, db)
    client._db = db
    assert "couldn't find a reservation" in client.get(f"/m/{tok2}").text
    resp = _post(client, tok2, r.ref)
    assert resp.status_code == 400 and "not this machine" in resp.text
    db.expire_all()
    assert db.scalar(select(ShopSession).where(ShopSession.student_id == alex.id)) is None


def test_unknown_token_404_and_requires_verification(client, db, demo):
    alex, r, m1, m2, tok1, tok2 = _setup(client, db)
    assert client.get("/m/not-a-real-token").status_code == 404
    from fastapi.testclient import TestClient

    from app.main import create_app
    with TestClient(create_app(), base_url="http://testserver") as anon:
        resp = anon.get(f"/m/{tok1}", follow_redirects=False)
        assert resp.status_code == 303 and resp.headers["location"].startswith("/verify")


def test_setting_off_disables_scan_checkin(client, db, demo):
    alex, r, m1, m2, tok1, tok2 = _setup(client, db)
    client._db = db
    shop_settings.set_value(db, "machine_qr_checkin", False)
    db.commit()
    assert _post(client, tok1, r.ref).status_code == 400


def test_admin_qr_page_lists_machines_pdf_and_reissue(client, db, demo):
    m1 = machine(db, "Haas Mini Mill 1")
    old = qr.machine_token(db, m1)
    db.commit()
    tok = staff_login(client, "admin")
    assert "Haas Mini Mill 1" in client.get("/admin/qr").text
    assert client.get(f"/admin/qr/machine/{m1.id}.png").headers["content-type"] == "image/png"
    pdf = client.get(f"/admin/qr/machine/{m1.id}.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert client.post(f"/admin/qr-reset/{m1.id}", data={"csrf_token": tok}, follow_redirects=False).status_code == 303
    db.expire_all()
    assert db.get(type(m1), m1.id).qr_token != old
    assert client.get(f"/m/{old}", follow_redirects=False).status_code in (303, 404)


def test_saved_qr_without_the_tv_code_cannot_check_in(client, db, demo):
    alex, r, m1, m2, tok1, tok2 = _setup(client, db)
    client._db = db
    for bad in (False, "0000" if rotcode.current(db)["code"] != "0000" else "1111"):
        resp = _post(client, tok1, r.ref, code=bad)
        assert resp.status_code == 400, bad
    db.expire_all()
    assert db.scalar(select(ShopSession).where(ShopSession.student_id == alex.id)) is None
