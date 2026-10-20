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

STEP_BYTES = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF"


def machine_ids(db):
    return [m.id for m in db.scalars(select(Machine).order_by(Machine.sort_order))]


def payload(tok, db, **over):
    d = {"csrf_token": tok, "form_token": security.new_form_token(), "title": "Base plate",
         "description": "Mill a base plate for my robot arm.", "machine_ids": [str(machine_ids(db)[0])],
         "estimated_hours": "4", "materials": "6061 aluminum", "course": "ME 250", "instructor": "Dr. X"}
    d.update(over)
    return d


def post_submit(client, tok, db, uploads=None, cad=True, **over):
    """cad=True adds a valid STEP file (a CAD file is now required on every submission)."""
    files_ = list(uploads or [])
    if cad:
        files_.append(("files", ("part.step", STEP_BYTES, "application/octet-stream")))
    return client.post("/submit", data=payload(tok, db, **over), files=files_, follow_redirects=False)


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
    assert p.status == "submitted" and p.revision_no == 1 and len(p.files) == 2  # PDF + the required STEP
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
    r = post_submit(client, tok, db, [pdf(f"f{i}.pdf") for i in range(4)])  # 4 + the default STEP = 5
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
    step = [("files", ("p.step", STEP_BYTES, "application/octet-stream"))]
    r1 = client.post("/submit", data=data, files=step, follow_redirects=False)
    r2 = client.post("/submit", data=data, files=[("files", ("p.step", STEP_BYTES, "application/octet-stream"))], follow_redirects=False)
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
    assert p.status == "submitted" and p.revision_no == 2 and len(p.files) == 3
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


STEP = b"ISO-10303-21;\nHEADER;\nFILE_DESCRIPTION(('x'),'2;1');\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"


@pytest.mark.parametrize("name,mime", [("part.step", "application/octet-stream"), ("part.STEP", ""), ("part.stp", "model/step"),
                                       ("part.step", "application/x-step"), ("part.step", "application/STEP"),
                                       ("part.step", "text/plain"), ("part.stp", "application/vnd.step")])
def test_step_files_accepted_whatever_mime_the_browser_reports(client, db, demo, name, mime):
    tok = verify_student(client)
    r = post_submit(client, tok, db, [("files", (name, STEP, mime))])
    assert r.status_code == 303, r.text[:300]


def test_step_with_bom_and_binary_stl_accepted_and_fakes_rejected(client, db, demo):
    tok = verify_student(client)
    import struct
    binary_stl = b"\0" * 80 + struct.pack("<I", 2) + b"\0" * 100
    ok = post_submit(client, tok, db, [("files", ("a.step", b"\xef\xbb\xbf" + STEP, "")), ("files", ("b.stl", binary_stl, ""))])
    assert ok.status_code == 303
    for name, data in (("fake.step", b"just some text"), ("fake.stl", b"hello"), ("fake.dxf", b"not a drawing")):
        r = post_submit(client, tok, db, [("files", (name, data, "application/octet-stream"))])
        assert r.status_code == 400 and "doesn't look like a valid" in unescape(r.text), name


def test_upload_limit_is_configurable_and_message_names_it(client, db, demo, monkeypatch):
    from app.config import get_config
    monkeypatch.setenv("MAX_UPLOAD_MB", "1")
    get_config.cache_clear()
    try:
        tok = verify_student(client)
        big = STEP + b"0" * (1024 * 1024)
        r = post_submit(client, tok, db, [("files", ("big.step", big, "application/octet-stream"))])
        assert r.status_code == 400 and "larger than 1 MB" in unescape(r.text)
        assert "1 MB each" in client.get("/submit").text
    finally:
        get_config.cache_clear()


def test_file_input_has_no_extension_filter_that_hides_step_files_on_phones(client, db, demo):
    verify_student(client)
    html = client.get("/submit").text
    assert 'type="file"' in html and "accept=" not in html[html.index('type="file"') - 80: html.index('type="file"') + 200]


def test_at_least_one_cad_file_is_required(client, db, demo):
    tok = verify_student(client)
    for ups in ([], [pdf()], [("files", ("pic.png", b"\x89PNG\r\n\x1a\n" + b"0" * 20, "image/png"))]):
        r = post_submit(client, tok, db, ups, cad=False)
        assert r.status_code == 400 and "at least one CAD file" in unescape(r.text), ups
    assert db.scalar(select(Project).where(Project.title == "Base plate")) is None
    for name, data in (("a.step", STEP_BYTES), ("a.stl", b"solid x\nendsolid"), ("a.dxf", b"0\nSECTION\n")):
        t2 = payload(tok, db)
        r = client.post("/submit", data=t2, files=[("files", (name, data, "application/octet-stream"))], follow_redirects=False)
        assert r.status_code == 303, name


def test_resubmission_keeps_earlier_cad_but_a_new_project_form_requires_it(client, db, demo):
    tok = verify_student(client, "cad.student@csuchico.edu", "Cad Student")
    assert post_submit(client, tok, db, cad=True).status_code == 303
    p = db.scalar(select(Project).where(Project.title == "Base plate"))
    with new_staff_client() as sc:
        staff_login(sc, "sam")
        decide(sc, sc.cookies.get("csrf"), p.id, action="changes", comments="Add tolerances")
    r = client.post(f"/submit/{p.id}/revise", data=payload(tok, db, description="Updated with tolerances +/-0.005 in."),
                    files=[pdf("v2.pdf")], follow_redirects=False)
    assert r.status_code == 303  # the STEP from revision 1 still counts
    assert 'required' in client.get("/submit").text.split('id="f_files"')[1][:160]


def test_approval_email_explains_how_to_request_a_missing_machine(db, demo, monkeypatch):
    from app import emailer
    from app.models import Approval
    from app.services import notify
    monkeypatch.setenv("REPLY_TO_EMAIL", "lab@example.edu")
    from app.config import get_config
    get_config.cache_clear() if hasattr(get_config, "cache_clear") else None
    a = db.scalar(select(Approval))
    emailer.SENT.clear()
    notify.project_approved(db, a)
    emailer.flush_outbox(db)
    mail = emailer.SENT[-1]
    assert "reply to this email" in mail["text"] and a.number in mail["text"] and "MISSING A MACHINE" in mail["text"]
    assert mail["reply_to"] == "lab@example.edu"
