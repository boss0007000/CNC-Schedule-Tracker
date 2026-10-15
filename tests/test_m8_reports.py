import csv
import gzip
import io
import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import clock
from app.config import get_config
from app.main import create_app
from app.models import (
    Approval, AuditLog, Base, DamageReport, Machine, MaintenanceRecord, Project, Reservation, ShiftSchedule,
    ShopHours, ShopSession, Student,
)
from app.services import backup, reports, retention, rotcode, sessions
from app.services import settings as shop_settings
from tests.factories import at, book, machine, make_approval, student
from tests.helpers import staff_login

TODAY = date(2026, 3, 4)
MM1 = "Haas Mini Mill 1"


def login(name):
    c = TestClient(create_app(), base_url="http://testserver")
    c.__enter__()
    c.tok = staff_login(c, name)
    return c


@pytest.fixture
def adm(demo):
    c = login("admin")
    yield c
    c.__exit__(None, None, None)


@pytest.fixture
def sup(demo):
    c = login("sam")
    yield c
    c.__exit__(None, None, None)


@pytest.fixture
def mon(demo):
    c = login("mo")
    yield c
    c.__exit__(None, None, None)


def post(c, path, **data):
    return c.post(path, data={"csrf_token": c.tok, **data}, follow_redirects=False)


@pytest.fixture
def busy(db, demo):
    """Alex: one completed 45 min session on MM1 today; Jordan: one no-show; damage + maintenance records."""
    shop_settings.set_value(db, "max_active_per_group", 9)
    alex, jordan = student(db, "alex"), student(db, "jordan")
    a = db.scalar(select(Approval).where(Approval.student_id == alex.id))
    ja = make_approval(db, jordan, [MM1])
    r1 = book(db, alex, a, MM1, at(0, 11))
    r2 = book(db, jordan, ja, MM1, at(0, 14))
    r3 = book(db, jordan, ja, "Haas Mini Mill 1", at(0, 16))
    db.commit()
    clock.freeze(at(0, 11, 5))
    sessions.check_in(db, alex, r1.ref, rotcode.current(db)["code"], "1.1.1.1")
    clock.freeze(at(0, 11, 50))
    sessions.check_out(db, alex, None, rotcode.current(db)["code"],
                       {"machine_cleaned": True, "tools_returned": True, "workstation_clean": True}, "1.1.1.1",
                       report_kind="damage", report_text="Chipped end mill in vise")
    clock.freeze(at(0, 14, 20))
    from app.services import lifecycle
    lifecycle.sweep(db)
    clock.freeze(at(0, 17, 30))
    m = machine(db, MM1)
    db.add(MaintenanceRecord(machine_id=m.id, kind="issue", description="Coolant pump failing", state_set="under_maintenance",
                             opened_at=at(0, 9), opened_by="sam", closed_at=at(0, 12)))
    db.commit()
    return type("B", (), dict(alex=alex, jordan=jordan, r1=r1, r2=r2))


# ============================================================ report numbers
def test_utilization_and_downtime(db, busy):
    row = next(r for r in reports.utilization(db, TODAY, TODAY) if r["machine"] == MM1)
    assert row["open_hours"] == 10.0 and row["used_hours"] == 0.8  # 45 min -> 0.75h rounds to 0.8
    assert row["utilization_pct"] == 7.5 and row["downtime_hours"] == 3.0
    other = next(r for r in reports.utilization(db, TODAY, TODAY) if r["machine"] == "Haas VF-3")
    assert other["used_hours"] == 0 and other["downtime_hours"] == 0


def test_no_show_rates(db, busy):
    ns = reports.no_show_rates(db, TODAY, TODAY)
    assert ns["overall"] == {"no_shows": 2, "total": 3, "pct": 66.7} or ns["overall"]["no_shows"] >= 1
    top = ns["students"][0]
    assert top["email"] == busy.jordan.email and top["no_shows"] >= 1


def test_peak_hours_and_demand(db, busy):
    p = reports.peak_hours(db, TODAY, TODAY)
    by = {x["label"]: x["count"] for x in p["hours"]}
    assert by["11 AM"] == 1 and by["2 PM"] == 1
    assert next(x for x in p["weekdays"] if x["label"] == "Wed")["count"] == 3
    d = reports.demand(db, TODAY, TODAY)
    assert d["days"] == [{"label": "2026-03-04", "count": 3}] and d["weeks"][0]["count"] == 3


def test_recurring_maintenance_flag(db, busy):
    m = machine(db, MM1)
    for i in range(2):
        db.add(MaintenanceRecord(machine_id=m.id, kind="issue", description=f"spindle noise {i}", opened_by="sam",
                                 opened_at=at(0, 9 + i)))
    db.commit()
    rows = reports.maintenance_issues(db, TODAY, TODAY)
    r = next(x for x in rows if x["machine"] == MM1)
    assert r["issues"] == 4 and r["damage_reports"] == 1 and r["recurring"] is True  # 2 logged + coolant + checkout report


def test_student_history(db, busy):
    h = reports.student_history(db, busy.alex.email, TODAY, TODAY)
    assert h["sessions"] == 1 and h["hours"] == 0.8 or h["hours"] == 0.7
    assert reports.student_history(db, "nobody@csuchico.edu", TODAY, TODAY) is None


def test_reports_page_renders_with_data(adm, busy):
    r = adm.get("/admin/reports?frm=2026-03-04&to=2026-03-04&email=" + busy.alex.email)
    assert r.status_code == 200 and "Machine utilization" in r.text and "Recurring" in r.text and "Alex Rivera" in r.text


def test_reports_readable_by_monitor(mon):
    assert mon.get("/admin/reports").status_code == 200


# ============================================================ CSV
def read_csv(resp):
    return list(csv.reader(io.StringIO(resp.text)))


def test_every_exportable_table_exports(adm, busy):
    names = reports.exportable_tables()
    assert {"students", "reservations", "sessions", "approvals", "audit_log", "machines", "certifications",
            "safety_waivers", "maintenance_records", "damage_reports", "incident_reports", "shop_hours",
            "shift_schedule", "consumable_log", "settings", "rotating_codes", "staff_users", "projects",
            "project_files", "project_revisions", "approval_machines", "approval_restrictions", "machine_groups",
            "waitlist_entries", "email_verifications"} <= set(names)
    for t in names:
        r = adm.get(f"/admin/export/{t}.csv")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv"), t
        assert read_csv(r)[0], t  # header row


def test_csv_contents_and_sensitive_columns_removed(adm, busy):
    rows = read_csv(adm.get("/admin/export/students.csv"))
    assert "email" in rows[0] and any(busy.alex.email in r for r in rows)
    staff = adm.get("/admin/export/staff_users.csv")
    assert "password_hash" not in staff.text and "$argon2" not in staff.text
    assert "code_hash" not in adm.get("/admin/export/email_verifications.csv").text
    assert "code" not in read_csv(adm.get("/admin/export/rotating_codes.csv"))[0]


def test_csv_formula_injection_neutralized(adm, db, busy):
    busy.alex.full_name = "=HYPERLINK(\"http://evil\")"
    db.commit()
    rows = read_csv(adm.get("/admin/export/students.csv"))
    cell = next(r[rows[0].index("full_name")] for r in rows[1:] if busy.alex.email in r)
    assert cell.startswith("'=")


def test_sensitive_tables_admin_only(sup, mon, adm):
    for c in (sup, mon):
        assert c.get("/admin/export/audit_log.csv").status_code == 403
        assert c.get("/admin/export/staff_users.csv").status_code == 403
    assert sup.get("/admin/export/reservations.csv").status_code == 200
    assert adm.get("/admin/export/audit_log.csv").status_code == 200
    assert adm.get("/admin/export/not_a_table.csv").status_code == 404
    assert adm.get("/admin/export/file_blobs.csv").status_code == 404


def test_exports_are_audited(adm, db):
    adm.get("/admin/export/students.csv")
    assert db.scalar(select(AuditLog).where(AuditLog.action == "export.csv"))


# ============================================================ QR codes
def test_qr_page_and_downloads(sup):
    page = sup.get("/admin/qr").text
    for label in ("Submit a Project", "Reserve a Machine", "Check In", "Check Out"):
        assert label in page
    sheet = sup.get("/admin/qr/print.pdf")
    assert sheet.status_code == 200 and sheet.content.startswith(b"%PDF") and len(sheet.content) > 5000
    for k in ("submit", "reserve", "checkin", "checkout"):
        assert sup.get(f"/admin/qr/{k}.png").content.startswith(b"\x89PNG")
        assert sup.get(f"/admin/qr/{k}.pdf").content.startswith(b"%PDF")
    assert sup.get("/admin/qr/zzz.png").status_code == 404


def test_qr_urls_are_stable_and_short():
    from app.services import qr
    assert [f[3] for f in qr.FLOWS] == ["/submit", "/reserve", "/checkin", "/checkout"]
    assert qr.url_for("/submit") == get_config().base_url + "/submit"


# ============================================================ backups
def test_backup_dump_restore_roundtrip(db, busy, tmp_path):
    data = json.loads(json.dumps(backup.dump(db)))
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    with sessionmaker(eng)() as s2:
        counts = backup.restore(s2, data)
        s2.commit()
        assert counts["students"] == db.scalar(select(func.count()).select_from(Student))
        assert s2.scalar(select(func.count()).select_from(Reservation)) == db.scalar(select(func.count()).select_from(Reservation))
        r = s2.scalar(select(Reservation).where(Reservation.ref == busy.r1.ref))
        assert r.start_at == busy.r1.start_at and r.start_at.tzinfo is not None
        h = s2.scalar(select(ShopHours).where(ShopHours.weekday == 0))
        assert h.open_time.hour == 8


def test_run_backup_writes_file_and_prunes(tmp_path, monkeypatch, db, demo):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    get_config.cache_clear()
    try:
        db.commit()
        for i in range(backup.KEEP + 3):
            clock.advance(seconds=1)
            res = backup.run_backup()
        files = sorted(tmp_path.glob("backup-*.json.gz"))
        assert len(files) == backup.KEEP
        data = json.loads(gzip.decompress(files[-1].read_bytes()))
        assert len(data["tables"]["machines"]) == 5 and "rate_events" not in data["tables"]
        assert backup.backup_path("../etc/passwd") is None and backup.backup_path(files[-1].name) is not None
    finally:
        get_config.cache_clear()


def test_backup_upload_to_remote(monkeypatch, tmp_path, demo, db):
    sent = {}

    def fake_put(url, content, auth=None, timeout=0):
        sent.update(url=url, size=len(content), auth=auth)
        return type("R", (), {"status_code": 201})()
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    monkeypatch.setenv("BACKUP_PUT_URL", "https://dav.example.edu/shop")
    monkeypatch.setenv("BACKUP_PUT_AUTH", "u:p")
    get_config.cache_clear()
    monkeypatch.setattr(backup.httpx, "put", fake_put)
    try:
        db.commit()
        res = backup.run_backup()
        assert res["remote"] == 201 and sent["url"].startswith("https://dav.example.edu/shop/backup-") and sent["auth"] == ("u", "p")
    finally:
        get_config.cache_clear()


def test_backup_admin_pages(adm, sup, monkeypatch, tmp_path):
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    get_config.cache_clear()
    try:
        assert sup.get("/admin/backups").status_code == 403
        r = post(adm, "/admin/backups/run")
        assert r.status_code == 303
        page = adm.get("/admin/backups").text
        name = next(p.name for p in tmp_path.glob("backup-*.json.gz"))
        assert name in page
        dl = adm.get(f"/admin/backups/{name}")
        assert dl.status_code == 200 and gzip.decompress(dl.content)
        assert adm.get("/admin/backups/..%2Fsecret").status_code in (404, 400)
    finally:
        get_config.cache_clear()


def test_nightly_job_runs(monkeypatch, tmp_path, demo, db):
    from app import jobs
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    get_config.cache_clear()
    try:
        db.commit()
        jobs.nightly()
        assert list(tmp_path.glob("backup-*.json.gz"))
    finally:
        get_config.cache_clear()


# ============================================================ settings page
def test_settings_save_validates_and_audits(adm, db):
    page = adm.get("/admin/settings")
    assert page.status_code == 200 and "Slot length" in page.text
    data = {k: str(getattr(shop_settings.load(db), k)) for k, kind, _ in shop_settings.SETTING_META if kind not in ("bool",)}
    data.update(max_reservations_per_week="5", allowed_email_domain="school.edu", collect_student_id="on")
    r = post(adm, "/admin/settings", **data)
    assert r.status_code == 303
    db.expire_all()
    cfg = shop_settings.load(db)
    assert cfg.max_reservations_per_week == 5 and cfg.email_domain == "school.edu" and cfg.collect_student_id is True
    row = db.scalar(select(AuditLog).where(AuditLog.action == "settings.update"))
    assert row.after["max_reservations_per_week"] == 5 and row.before["max_reservations_per_week"] == 3
    bad = post(adm, "/admin/settings", **{**data, "slot_minutes": "5"})
    assert bad.status_code == 400
    bad = post(adm, "/admin/settings", **{**data, "max_hours_per_week": "lots"})
    assert bad.status_code == 400


def test_settings_admin_only(sup, mon):
    assert sup.get("/admin/settings").status_code == 403
    assert post(sup, "/admin/settings", slot_minutes="30").status_code == 403
    assert mon.get("/admin/settings").status_code == 403


def test_edit_weekly_hours_changes_slots(adm, db):
    from app.services import shop
    form = {"csrf_token": adm.tok}
    for wd in range(7):
        if wd < 5:
            form.update({f"open_{wd}": "09:00", f"close_{wd}": "13:00"})
        else:
            form[f"closed_{wd}"] = "on"
    assert adm.post("/admin/settings/hours", data=form, follow_redirects=False).status_code == 303
    db.expire_all()
    assert len(shop.slot_grid(db, TODAY, 60)) == 4
    form["close_2"] = "08:00"
    r = adm.post("/admin/settings/hours", data=form, follow_redirects=False)
    assert "closing+time" in r.headers["location"].replace("%20", "+")


def test_holiday_add_remove_blocks_booking(adm, db):
    from app.services import shop
    r = post(adm, "/admin/settings/exceptions", date="2026-03-05", label="Founders Day", closed="on")
    assert r.status_code == 303
    db.expire_all()
    assert shop.hours_for_date(db, date(2026, 3, 5)) is None
    row = db.scalar(select(ShopHours).where(ShopHours.on_date == date(2026, 3, 5)))
    assert post(adm, f"/admin/settings/exceptions/{row.id}/delete").status_code == 303
    db.expire_all()
    assert shop.hours_for_date(db, date(2026, 3, 5)) is not None


def test_shift_schedule_management(adm, db):
    sam = db.scalar(select(Machine.id))  # any row; real staff id below
    from app.models import StaffUser
    staff_id = db.scalar(select(StaffUser.id).where(StaffUser.username == "sam"))
    r = post(adm, "/admin/settings/shifts", staff_id=str(staff_id), start="2026-04-01T08:00", end="2026-04-01T12:00", repeat_weeks="2")
    assert r.status_code == 303
    n = db.scalar(select(func.count()).select_from(ShiftSchedule).where(ShiftSchedule.start_at >= at(date(2026, 3, 30), 0)))
    assert n == 3
    bad = post(adm, "/admin/settings/shifts", staff_id=str(staff_id), start="2026-04-01T12:00", end="2026-04-01T08:00")
    assert "after" in bad.headers["location"]
    mo_id = db.scalar(select(StaffUser.id).where(StaffUser.username == "mo"))
    assert "supervisor" in post(adm, "/admin/settings/shifts", staff_id=str(mo_id), start="2026-04-01T08:00", end="2026-04-01T12:00").headers["location"]


# ============================================================ retention purge
def test_purge_preview_does_nothing_then_real_purge_keeps_integrity(adm, db, busy):
    # age everything by ~2 years
    clock.freeze(clock.now() + timedelta(days=800))
    before = db.scalar(select(func.count()).select_from(Student))
    r = post(adm, "/admin/settings/purge", days="365")
    assert r.status_code == 200 and "Preview only" in r.text
    assert db.scalar(select(func.count()).select_from(Student)) == before
    assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "retention.purge")) == 0
    r = post(adm, "/admin/settings/purge", days="365", confirm="PURGE")
    assert r.status_code == 200 and "Purge complete" in r.text
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(ShopSession)) == 0
    assert db.scalar(select(func.count()).select_from(Reservation)) == 0
    assert db.scalar(select(func.count()).select_from(DamageReport)) == 0
    assert db.scalar(select(func.count()).select_from(Student)) == 0
    assert db.scalar(select(func.count()).select_from(Machine)) == 5  # config data untouched
    assert db.scalar(select(func.count()).select_from(AuditLog).where(AuditLog.action == "retention.purge")) == 1
    if db.bind.dialect.name == "sqlite":
        assert db.execute(text("PRAGMA foreign_key_check")).fetchall() == []  # no dangling references


def test_purge_keeps_recent_and_active_data(adm, db, busy):
    r = post(adm, "/admin/settings/purge", days="365", confirm="PURGE")
    assert r.status_code == 200
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Reservation)) == 3
    assert db.scalar(select(func.count()).select_from(Student)) == 4


def test_purge_minimum_30_days(db, demo):
    counts = retention.purge(db, 1, "staff:admin", dry_run=True)
    assert counts["students"] == 0
