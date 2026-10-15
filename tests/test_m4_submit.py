import re
from pathlib import Path

import pytest
from html import unescape

from sqlalchemy import select

from app import emailer, security
from app.config import get_config
from app.models import Approval, AuditLog, Machine, Project, ProjectFile, ProjectRevision
from app.services import files
from tests.helpers import csrf, staff_login, verify_student

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


def machine_ids(db):
    return [m.id for m in db.scalars(select(Machine).order_by(Machine.sort_order))]


def payload(tok, db, **over):
    d = {"csrf_token": tok, "form_token": security.new_form_token(), "title": "Base plate",
         "description": "Mill a base plate for my robot arm.", "machine_ids": [str(machine_ids(db)[0])],
         "estimated_hours": "4", "materials": "6061 aluminum", "course": "ME 250", "instructor": "Dr. X"}
    d.update(over)
    return d


def post_submit(client, tok, db, uploads=None, **over):
    return client.post("/submit", data=payload(tok, db, **over), files=uploads or [], follow_redirects=False)


def pdf(name="plan.pdf"):
    return ("files", (name, PDF, "application/pdf"))


# ----------------------------------------------------------------- student side
def test_submit_requires_verification(client):
    r = client.get("/submit", follow_redirects=False)
    assert r.status_code == 303 and "/verify" in r.headers["location"]


def test_successful_submission_with_file_and_emails(client, db, demo):
    tok = verify_student(client, "newbie@csuchico.edu", "New Bie")
    emailer.SENT.clear()
    r = post_submit(client, tok, db, [pdf()])
    assert r.status_code == 303 and r.headers["location"].startswith("/submit/")
    p = db.scalar(select(Project).where(Project.title == "Base plate"))
    assert p.status == "submitted" and p.revision_no == 1 and len(p.files) == 1
    assert p.files[0].sha256 and p.files[0].scan_status == "skipped"
    to = {m["to"]: m["subject"] for m in emailer.SENT}
    assert "newbie@csuchico.edu" in to and "shop@csuchico.edu" in to  # student + staff
    assert "New project submission" in to["shop@csuchico.edu"]
    assert client.get(r.headers["location"]).status_code == 200


def test_files_stored_outside_web_root_with_random_names(client, db, demo):
    tok = verify_student(client)
    post_submit(client, tok, db, [pdf("../../etc/passwd.pdf")])
    f = db.scalar(select(ProjectFile).where(ProjectFile.original_name.like("%passwd%")))
    assert "/" not in f.original_name and ".." not in f.original_name
    stored = Path(get_config().upload_dir) / f.storage_key
    assert stored.exists() and stored.read_bytes() == PDF
    assert "static" not in str(stored) and f.storage_key != f.original_name


@pytest.mark.parametrize("upload,msg", [
    (("files", ("virus.exe", b"MZ\x90\x00", "application/octet-stream")), "isn't an accepted file type"),
    (("files", ("a.pdf", PDF, "text/html")), "doesn't look like"),
    (("files", ("fake.pdf", b"<html>not a pdf</html>", "application/pdf")), "isn't a valid PDF"),
    (("files", ("evil.stl", b"#!/bin/sh\nrm -rf /", "application/octet-stream")), "isn't allowed"),
    (("files", ("empty.pdf", b"", "application/pdf")), "is empty"),
    (("files", ("big.pdf", PDF + b"0" * (10 * 1024 * 1024), "application/pdf")), "larger than 10 MB"),
])
def test_bad_uploads_rejected(client, db, demo, upload, msg):
    tok = verify_student(client)
    r = post_submit(client, tok, db, [upload])
    assert r.status_code == 400 and msg in unescape(r.text)
    assert db.scalar(select(Project).where(Project.title == "Base plate")) is None  # nothing half-saved


def test_max_five_files(client, db, demo):
    tok = verify_student(client)
    r = post_submit(client, tok, db, [pdf(f"f{i}.pdf") for i in range(6)])
    assert r.status_code == 400 and "at most 5 files" in unescape(r.text)
    r = post_submit(client, tok, db, [pdf(f"f{i}.pdf") for i in range(5)] + [])
    assert r.status_code == 303


def test_cad_formats_accepted(client, db, demo):
    tok = verify_student(client)
    ups = [("files", ("p.dxf", b"0\nSECTION\n", "application/octet-stream")),
           ("files", ("p.step", b"ISO-10303-21;", "application/octet-stream")),
           ("files", ("p.stl", b"solid x\nendsolid", "model/stl"))]
    assert post_submit(client, tok, db, ups).status_code == 303


def test_validation_messages(client, db, demo):
    tok = verify_student(client)
    assert "title" in post_submit(client, tok, db, title="x").text
    assert "Choose at least one machine" in unescape(post_submit(client, tok, db, machine_ids=[]).text)
    assert "between 0.5 and 500" in post_submit(client, tok, db, estimated_hours="0").text
    assert "must be a number" in post_submit(client, tok, db, estimated_hours="abc").text


def test_double_tap_is_idempotent(client, db, demo):
    tok = verify_student(client)
    data = payload(tok, db)
    r1 = client.post("/submit", data=data, follow_redirects=False)
    r2 = client.post("/submit", data=data, follow_redirects=False)
    assert r1.headers["location"] == r2.headers["location"]
    assert len(db.scalars(select(Project).where(Project.title == "Base plate")).all()) == 1


def test_honeypot_rejected(client, db, demo):
    tok = verify_student(client)
    assert post_submit(client, tok, db, website="spam").status_code == 400


def test_student_cannot_see_other_students_project(client, db, demo):
    p = db.scalar(select(Project).where(Project.title == "Spindle shaft"))
    verify_student(client)  # alex
    assert client.get(f"/submit/{p.id}").status_code == 404


# ----------------------------------------------------------------- staff review
def submit_as(client, db, email="newbie@csuchico.edu"):
    tok = verify_student(client, email, "New Bie")
    post_submit(client, tok, db, [pdf()])
    return db.scalar(select(Project).where(Project.title == "Base plate"))


def staff_client(app_client_factory):
    pass


@pytest.fixture
def staff(client):
    return client


def decide(client, tok, pid, **data):
    return client.post(f"/admin/approvals/{pid}/decide", data={"csrf_token": tok, **data}, follow_redirects=False)


def new_staff_client():
    from fastapi.testclient import TestClient
    from app.main import create_app
    return TestClient(create_app(), base_url="http://testserver")


def test_approve_issues_number_email_and_record(client, db, demo):
    p = submit_as(client, db)
    emailer.SENT.clear()
    with new_staff_client() as sc:
        tok = staff_login(sc, "sam")
        ids = machine_ids(db)[:2]
        r = decide(sc, tok, p.id, action="approve", machine_ids=[str(i) for i in ids], max_total_hours="6",
                   max_sessions="3", expires_on="2026-12-31", restrictions="Aluminum only\nNo steel",
                   conditions="Wear glasses", comments="Nice design")
        assert r.status_code == 303
    db.expire_all()
    a = db.scalar(select(Approval).where(Approval.project_id == p.id))
    assert re.fullmatch(r"APR-2026-\d{4}", a.number) and a.number != demo["approvals"][0]
    assert sorted(a.machine_ids) == sorted(ids) and a.max_total_hours == 6 and a.max_sessions == 3
    assert [r.value for r in a.restrictions] == ["Aluminum only", "No steel"]
    assert db.get(Project, p.id).status == "approved"
    msg = next(m for m in emailer.SENT if m["to"] == "newbie@csuchico.edu")
    assert a.number in msg["subject"] and "Aluminum only" in msg["text"] and "Wear glasses" in msg["text"] and "Nice design" in msg["text"]
    assert "/approval/" in msg["text"]
    assert db.scalar(select(AuditLog).where(AuditLog.action == "project.approve")).actor == "staff:sam"


def test_signed_approval_link_and_pdf(client, db, demo):
    a = db.scalar(select(Approval))
    tok = security.sign("approval-view", {"n": a.number})
    r = client.get(f"/approval/{a.number}?t={tok}")
    assert r.status_code == 200 and a.number in r.text
    pdf_resp = client.get(f"/approval/{a.number}/pdf?t={tok}")
    assert pdf_resp.content.startswith(b"%PDF")
    assert client.get(f"/approval/{a.number}").status_code == 404  # no token, not logged in
    assert client.get(f"/approval/{a.number}?t=forged").status_code == 404


def test_reject_requires_reason_and_emails(client, db, demo):
    p = submit_as(client, db)
    emailer.SENT.clear()
    with new_staff_client() as sc:
        tok = staff_login(sc, "sam")
        assert decide(sc, tok, p.id, action="reject", reason="").status_code == 400
        assert decide(sc, tok, p.id, action="reject", reason="Material is unsafe").status_code == 303
    db.expire_all()
    assert db.get(Project, p.id).status == "rejected"
    assert "Material is unsafe" in next(m for m in emailer.SENT if m["to"] == "newbie@csuchico.edu")["text"]


def test_request_changes_then_resubmit_keeps_history(client, db, demo):
    p = submit_as(client, db)
    with new_staff_client() as sc:
        tok = staff_login(sc, "sam")
        assert decide(sc, tok, p.id, action="changes", comments="").status_code == 400
        assert decide(sc, tok, p.id, action="changes", comments="Add tolerances").status_code == 303
    db.expire_all()
    assert db.get(Project, p.id).status == "modifications_requested"
    assert any("Add tolerances" in m["text"] for m in emailer.SENT if m["to"] == "newbie@csuchico.edu")
    tok = client.cookies.get("csrf")
    assert client.get(f"/submit/{p.id}/revise").status_code == 200
    r = client.post(f"/submit/{p.id}/revise", data=payload(tok, db, description="Now with tolerances +/-0.005 in."),
                    files=[pdf("v2.pdf")], follow_redirects=False)
    assert r.status_code == 303
    db.expire_all()
    p = db.get(Project, p.id)
    assert p.status == "submitted" and p.revision_no == 2 and len(p.files) == 2
    revs = db.scalars(select(ProjectRevision).where(ProjectRevision.project_id == p.id).order_by(ProjectRevision.revision_no)).all()
    assert [r.revision_no for r in revs] == [1, 2] and revs[0].staff_comments == "Add tolerances"
    assert "tolerances +/-0.005" in revs[1].description and "tolerances" not in revs[0].description.replace("Mill a base", "")
    # a second resubmit attempt is refused: project is no longer waiting for changes
    r = client.post(f"/submit/{p.id}/revise", data=payload(tok, db), follow_redirects=False)
    assert r.status_code == 400


def test_cannot_decide_twice(client, db, demo):
    p = submit_as(client, db)
    with new_staff_client() as sc:
        tok = staff_login(sc, "sam")
        assert decide(sc, tok, p.id, action="reject", reason="Not feasible").status_code == 303
        r = decide(sc, tok, p.id, action="approve", machine_ids=[str(machine_ids(db)[0])])
        assert r.status_code == 400 and "already been decided" in r.text


def test_approval_defaults_expiry_and_requires_machines(client, db, demo):
    p = submit_as(client, db)
    with new_staff_client() as sc:
        tok = staff_login(sc, "sam")
        assert "at least one machine" in decide(sc, tok, p.id, action="approve").text
        assert decide(sc, tok, p.id, action="approve", machine_ids=[str(machine_ids(db)[0])]).status_code == 303
    a = db.scalar(select(Approval).where(Approval.project_id == p.id))
    assert (a.expires_at - a.approved_at).days in (119, 120)


def test_monitor_can_view_but_not_decide(client, db, demo):
    p = submit_as(client, db)
    with new_staff_client() as sc:
        tok = staff_login(sc, "mo")
        assert sc.get(f"/admin/approvals/{p.id}").status_code == 200
        assert decide(sc, tok, p.id, action="reject", reason="nope").status_code == 403
    assert db.get(Project, p.id).status == "submitted"


def test_queue_page_lists_pending(client, db, demo):
    submit_as(client, db)
    with new_staff_client() as sc:
        staff_login(sc, "sam")
        assert "Base plate" in sc.get("/admin/approvals").text
        assert "Spindle shaft" not in sc.get("/admin/approvals").text
        assert "Spindle shaft" in sc.get("/admin/approvals?status=all").text


def test_file_download_needs_staff_and_valid_signed_link(client, db, demo):
    p = submit_as(client, db)
    fid = db.scalar(select(ProjectFile.id).where(ProjectFile.project_id == p.id))
    link = files.signed_link("project", fid)
    anon = client.get(link, follow_redirects=False)
    assert anon.status_code == 303  # student cookie is not staff
    with new_staff_client() as sc:
        staff_login(sc, "mo")
        ok = sc.get(link)
        assert ok.status_code == 200 and ok.content == PDF and ok.headers["x-content-type-options"] == "nosniff"
        assert sc.get(f"/admin/files/project/{fid}").status_code == 403  # unsigned
        assert sc.get(f"/admin/files/project/{fid}?t=junk").status_code == 403
        other = files.signed_link("project", fid + 1)
        assert sc.get(f"/admin/files/project/{fid}?t=" + other.split("t=")[1]).status_code == 403  # token for another file
    # expiry: tokens older than 15 minutes are refused
    tok = link.split("t=")[1]
    assert security.unsign("file-link", tok, files.LINK_TTL_SECONDS) is not None
    assert security.unsign("file-link", tok, -1) is None


def test_filename_sanitizer():
    assert files.sanitize_filename("../../a b$c.pdf") == "a b_c.pdf"
    assert files.sanitize_filename("C:\\Users\\x\\file.pdf") == "file.pdf"
    assert files.sanitize_filename("") == "file"
