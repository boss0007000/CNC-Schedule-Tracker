from datetime import timedelta
from html import unescape

import pytest
from sqlalchemy import select

from app import clock, security
from app.models import Approval, DamageReport, ShopSession
from app.services import rotcode
from tests.factories import at, book, machine, student
from tests.helpers import verify_student

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


@pytest.fixture
def ctx(client, db, demo):
    tok = verify_student(client, "alex.rivera@csuchico.edu", "Alex Rivera")
    alex = student(db, "alex")
    a = db.scalar(select(Approval).where(Approval.student_id == alex.id))
    r = book(db, alex, a, "Haas Mini Mill 1", at(0, 11))
    db.commit()
    clock.freeze(at(0, 10, 55))
    return type("C", (), dict(tok=tok, ref=r.ref))


def code(db):
    c = rotcode.current(db)["code"]
    db.commit()  # don't hold row locks while the app (another connection) runs
    return c


def test_check_in_flow_and_double_scan(client, db, ctx):
    page = client.get("/checkin")
    assert page.status_code == 200 and ctx.ref in page.text  # offered in the datalist
    data = {"csrf_token": ctx.tok, "form_token": security.new_form_token(), "ref": ctx.ref, "shop_code": code(db)}
    r1 = client.post("/checkin", data=data, follow_redirects=False)
    r2 = client.post("/checkin", data=data, follow_redirects=False)
    assert r1.status_code == 303 and r1.headers["location"] == r2.headers["location"]
    assert len(db.scalars(select(ShopSession)).all()) == 1
    assert "checked in" in client.get(r1.headers["location"]).text


def test_wrong_code_message_and_persisted_failures(client, db, ctx):
    good = code(db)
    bad = "0000" if good != "0000" else "1111"
    for _ in range(5):
        r = client.post("/checkin", data={"csrf_token": ctx.tok, "form_token": security.new_form_token(),
                                          "ref": ctx.ref, "shop_code": bad})
        assert r.status_code == 400 and "shop code is wrong" in unescape(r.text)
    r = client.post("/checkin", data={"csrf_token": ctx.tok, "form_token": security.new_form_token(),
                                      "ref": ctx.ref, "shop_code": good})
    assert r.status_code == 429  # failures were persisted, so the limit holds across requests


def test_late_checkin_over_http_marks_no_show_and_shows_reason(client, db, ctx):
    clock.freeze(at(0, 11, 20))
    r = client.post("/checkin", data={"csrf_token": ctx.tok, "form_token": security.new_form_token(),
                                      "ref": ctx.ref, "shop_code": code(db)})
    assert r.status_code == 400 and "no-show" in unescape(r.text)
    db.expire_all()
    from app.models import Reservation
    assert db.scalar(select(Reservation).where(Reservation.ref == ctx.ref)).status == "no_show"


def test_full_check_out_with_photo_report(client, db, ctx):
    client.post("/checkin", data={"csrf_token": ctx.tok, "form_token": security.new_form_token(), "ref": ctx.ref,
                                  "shop_code": code(db)})
    clock.freeze(at(0, 11, 40))
    page = client.get("/checkout")
    assert "Cleanup checklist" in page.text
    base = {"csrf_token": ctx.tok, "form_token": security.new_form_token(), "shop_code": code(db), "leave_state": "clear",
            "report_kind": "damage", "report_text": "Chipped the end mill."}
    r = client.post("/checkout", data={**base, "machine_cleaned": "on"}, follow_redirects=False)
    assert r.status_code == 400 and "Tools returned" in r.text
    base["form_token"] = security.new_form_token()
    full = {**base, "machine_cleaned": "on", "tools_returned": "on", "workstation_clean": "on"}
    r = client.post("/checkout", data=full, files=[("photo", ("p.png", PNG, "image/png"))], follow_redirects=False)
    assert r.status_code == 303
    assert "Checked out" in client.get(r.headers["location"]).text
    db.expire_all()
    rep = db.scalar(select(DamageReport))
    assert rep.photo_key and rep.severity == "damage"
    assert machine(db, "Haas Mini Mill 1").maintenance_state == "awaiting_inspection"


def test_photo_must_be_an_image(client, db, ctx):
    client.post("/checkin", data={"csrf_token": ctx.tok, "form_token": security.new_form_token(), "ref": ctx.ref,
                                  "shop_code": code(db)})
    full = {"csrf_token": ctx.tok, "form_token": security.new_form_token(), "shop_code": code(db), "leave_state": "clear",
            "report_kind": "damage", "report_text": "Chipped the end mill.", "machine_cleaned": "on",
            "tools_returned": "on", "workstation_clean": "on"}
    r = client.post("/checkout", data=full, files=[("photo", ("p.pdf", b"%PDF-1.4 x", "application/pdf"))])
    assert r.status_code == 400 and "accepted file type" in unescape(r.text)
    assert db.scalar(select(ShopSession).where(ShopSession.status == "active")) is not None  # still checked in
