from html import unescape

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import clock, emailer, security
from app.models import Approval, Reservation, SafetyWaiver
from tests.factories import at, machine, student
from tests.helpers import verify_student


@pytest.fixture
def ctx(client, db, demo):
    tok = verify_student(client, "alex.rivera@csuchico.edu", "Alex Rivera")
    a = db.scalar(select(Approval).where(Approval.number == demo["approvals"][0]))
    return type("C", (), dict(tok=tok, approval=a.number, mm1=machine(db, "Haas Mini Mill 1").id,
                              st10=machine(db, "Haas ST-10").id))


def form(ctx, hour=10, day=1, **over):
    d = {"csrf_token": ctx.tok, "form_token": security.new_form_token(), "approval": ctx.approval,
         "machine_id": str(ctx.mm1), "start": at(day, hour).isoformat(), "nslots": "1", "action": "book"}
    d.update(over)
    return d


def test_reserve_page_lists_available_slots(client, ctx):
    day = clock.to_local(at(1, 10)).date().isoformat()
    r = client.get(f"/reserve?approval={ctx.approval}&machine={ctx.mm1}&date={day}")
    assert r.status_code == 200 and r.text.count('name="start"') == 14


def test_reserve_page_explains_uncovered_machine(client, ctx):
    day = clock.to_local(at(1, 10)).date().isoformat()
    r = client.get(f"/reserve?approval={ctx.approval}&machine={ctx.st10}&date={day}")
    assert "doesn't cover the Haas ST-10" in unescape(r.text) and 'name="start"' not in r.text


def test_book_via_http_and_double_tap_idempotent(client, db, ctx):
    data = form(ctx)
    r1 = client.post("/reserve", data=data, follow_redirects=False)
    r2 = client.post("/reserve", data=data, follow_redirects=False)
    assert r1.status_code == 303 and r1.headers["location"] == r2.headers["location"]
    assert len(db.scalars(select(Reservation)).all()) == 1
    ref = r1.headers["location"].split("/")[2].split("?")[0]
    page = client.get(f"/reservation/{ref}")
    assert page.status_code == 200 and ref in page.text


def test_validation_error_shown_to_user(client, ctx):
    r = client.post("/reserve", data=form(ctx, machine_id=str(ctx.st10)))
    assert r.status_code == 400 and "doesn&#39;t cover the Haas ST-10" in r.text.replace("&#39;", "&#39;")


def test_waiver_prompt_then_inline_accept(client, db, ctx):
    db.query(SafetyWaiver).delete()
    db.commit()
    r = client.post("/reserve", data=form(ctx))
    assert r.status_code == 400 and "safety waiver" in r.text and 'name="accept_waiver"' in r.text
    r = client.post("/reserve", data=form(ctx, accept_waiver="on"), follow_redirects=False)
    assert r.status_code == 303
    assert db.scalar(select(SafetyWaiver)) is not None


def test_cancel_via_emailed_link_without_login(client, db, ctx):
    client.post("/reserve", data=form(ctx, hour=16, day=2))
    emailer.flush_in_new_session()
    mail = [m for m in emailer.SENT if "Reservation confirmed" in m["subject"]][-1]
    link = next(w for w in mail["text"].split() if "/reservation/" in w)
    path = link.replace("http://localhost:8000", "")
    with TestClient(client.app, base_url="http://testserver") as anon:
        page = anon.get(path)
        assert page.status_code == 200 and "Cancel reservation" in page.text
        anon.get("/verify"); tok = anon.cookies.get("csrf")
        ref = path.split("/")[2].split("?")[0]
        t = path.split("t=")[1]
        assert anon.post(f"/reservation/{ref}/cancel", data={"csrf_token": tok, "t": "forged"}).status_code == 404
        r = anon.post(f"/reservation/{ref}/cancel", data={"csrf_token": tok, "t": t}, follow_redirects=False)
        assert r.status_code == 303
    db.expire_all()
    assert db.scalar(select(Reservation).where(Reservation.ref == ref)).status == "cancelled"


def test_other_student_cannot_view_reservation(client, db, ctx):
    r = client.post("/reserve", data=form(ctx), follow_redirects=False)
    path = r.headers["location"].split("?")[0]
    with TestClient(client.app, base_url="http://testserver") as other:
        verify_student(other, "jordan.lee@csuchico.edu", "Jordan Lee", "senior")
        assert other.get(path).status_code == 404


def test_extend_via_http(client, db, ctx):
    r = client.post("/reserve", data=form(ctx), follow_redirects=False)
    path = r.headers["location"].split("?")[0]
    ext = client.post(path + "/extend", data={"csrf_token": ctx.tok}, follow_redirects=False)
    assert ext.status_code == 303
    res = db.scalar(select(Reservation))
    db.refresh(res)
    assert res.hours == 2


def test_my_page_shows_reservations_and_approvals(client, ctx):
    client.post("/reserve", data=form(ctx))
    page = client.get("/my").text
    assert ctx.approval in page and "Haas Mini Mill 1" in page
