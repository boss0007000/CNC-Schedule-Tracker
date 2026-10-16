"""Reporting queries and CSV export."""
import csv
import io
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.models import (
    Base, DamageReport, Machine, MaintenanceRecord, Reservation, ShopSession, Student,
)
from app.services import shop


REAL_STATUSES = ("active", "closed", "auto_closed", "force_closed")  # pending/denied/expired requests are not machine use


def range_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    """[start, end] inclusive local dates -> UTC [from, to)."""
    return clock.local_dt(start, datetime.min.time()), clock.local_dt(end + timedelta(days=1), datetime.min.time())


def open_hours_between(db: Session, start: date, end: date) -> float:
    total, d = 0.0, start
    while d <= end:
        w = shop.hours_for_date(db, d)
        if w:
            total += (w[1] - w[0]).total_seconds() / 3600
        d += timedelta(days=1)
    return total


def _overlap_h(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> float:
    lo, hi = max(a0, b0), min(a1, b1)
    return max(0.0, (hi - lo).total_seconds() / 3600)


def utilization(db: Session, start: date, end: date) -> list[dict]:
    f, t = range_bounds(start, end)
    avail = open_hours_between(db, start, end)
    now = clock.now()
    rows = []
    for m in db.scalars(select(Machine).where(Machine.active.is_(True)).order_by(Machine.sort_order, Machine.name)):
        used = sum(_overlap_h(s.checked_in_at, s.checked_out_at or now, f, t) for s in db.scalars(
            select(ShopSession).where(ShopSession.machine_id == m.id, ShopSession.checked_in_at < t,
                                      ShopSession.status.in_(REAL_STATUSES),
                                      (ShopSession.checked_out_at.is_(None)) | (ShopSession.checked_out_at > f))))
        down = sum(_overlap_h(r.opened_at, r.closed_at or now, f, t) for r in db.scalars(
            select(MaintenanceRecord).where(MaintenanceRecord.machine_id == m.id, MaintenanceRecord.kind != "scheduled",
                                            MaintenanceRecord.state_set == "under_maintenance",
                                            MaintenanceRecord.opened_at < t)))
        booked = sum(_overlap_h(r.start_at, r.end_at, f, t) for r in db.scalars(
            select(Reservation).where(Reservation.machine_id == m.id, Reservation.status.in_(("confirmed", "checked_in", "completed")),
                                      Reservation.start_at < t, Reservation.end_at > f)))
        rows.append({"machine": m.name, "open_hours": round(avail, 1), "used_hours": round(used, 1),
                     "booked_hours": round(booked, 1), "utilization_pct": round(100 * used / avail, 1) if avail else 0.0,
                     "downtime_hours": round(down, 1)})
    return rows


def peak_hours(db: Session, start: date, end: date) -> dict:
    f, t = range_bounds(start, end)
    by_hour, by_wd = defaultdict(int), defaultdict(int)
    for r in db.scalars(select(Reservation).where(Reservation.start_at >= f, Reservation.start_at < t,
                                                  Reservation.status.in_(("confirmed", "checked_in", "completed", "no_show")))):
        loc = clock.to_local(r.start_at)
        by_hour[loc.hour] += 1
        by_wd[loc.weekday()] += 1
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return {"hours": [{"label": f"{h % 12 or 12} {'AM' if h < 12 else 'PM'}", "count": by_hour.get(h, 0)} for h in range(6, 22)],
            "weekdays": [{"label": names[i], "count": by_wd.get(i, 0)} for i in range(7)]}


def demand(db: Session, start: date, end: date) -> dict:
    f, t = range_bounds(start, end)
    per_day, per_week = defaultdict(int), defaultdict(int)
    for r in db.scalars(select(Reservation).where(Reservation.start_at >= f, Reservation.start_at < t,
                                                  Reservation.status != "cancelled")):
        d = clock.to_local(r.start_at).date()
        per_day[d] += 1
        per_week[d - timedelta(days=d.weekday())] += 1
    return {"days": [{"label": d.isoformat(), "count": c} for d, c in sorted(per_day.items())],
            "weeks": [{"label": f"Week of {d.isoformat()}", "count": c} for d, c in sorted(per_week.items())]}


def no_show_rates(db: Session, start: date, end: date) -> dict:
    f, t = range_bounds(start, end)
    per_machine = defaultdict(lambda: [0, 0])  # [no_shows, outcomes]
    per_student = defaultdict(lambda: [0, 0])
    for r in db.scalars(select(Reservation).where(Reservation.start_at >= f, Reservation.start_at < t,
                                                  Reservation.status.in_(("completed", "checked_in", "no_show")))):
        ns = 1 if r.status == "no_show" else 0
        per_machine[r.machine.name][0] += ns
        per_machine[r.machine.name][1] += 1
        per_student[(r.student.full_name, r.student.email)][0] += ns
        per_student[(r.student.full_name, r.student.email)][1] += 1
    total_ns = sum(v[0] for v in per_machine.values())
    total = sum(v[1] for v in per_machine.values())
    pct = lambda a, b: round(100 * a / b, 1) if b else 0.0  # noqa: E731
    return {"overall": {"no_shows": total_ns, "total": total, "pct": pct(total_ns, total)},
            "machines": sorted([{"name": k, "no_shows": v[0], "total": v[1], "pct": pct(*v)} for k, v in per_machine.items()],
                               key=lambda x: -x["pct"]),
            "students": sorted([{"name": k[0], "email": k[1], "no_shows": v[0], "total": v[1], "pct": pct(*v)}
                                for k, v in per_student.items() if v[0]], key=lambda x: -x["no_shows"])[:20]}


def maintenance_issues(db: Session, start: date, end: date) -> list[dict]:
    f, t = range_bounds(start, end)
    by_machine: dict[str, dict] = {}
    for rec in db.scalars(select(MaintenanceRecord).where(MaintenanceRecord.opened_at >= f, MaintenanceRecord.opened_at < t,
                                                          MaintenanceRecord.kind.in_(("issue", "repair")))):
        d = by_machine.setdefault(rec.machine.name, {"machine": rec.machine.name, "issues": 0, "damage_reports": 0, "last": None, "examples": []})
        d["issues"] += 1
        d["last"] = max(filter(None, [d["last"], rec.opened_at]))
        if len(d["examples"]) < 3:
            d["examples"].append(rec.description[:80])
    for rep in db.scalars(select(DamageReport).where(DamageReport.created_at >= f, DamageReport.created_at < t)):
        d = by_machine.setdefault(rep.machine.name, {"machine": rep.machine.name, "issues": 0, "damage_reports": 0, "last": None, "examples": []})
        d["damage_reports"] += 1
    out = sorted(by_machine.values(), key=lambda x: -(x["issues"] + x["damage_reports"]))
    for d in out:
        d["recurring"] = d["issues"] >= 3  # student reports also create an issue record
    return out


def student_history(db: Session, email: str, start: date, end: date) -> dict | None:
    stu = db.scalar(select(Student).where(Student.email == email.strip().lower()))
    if stu is None:
        return None
    f, t = range_bounds(start, end)
    sess = db.scalars(select(ShopSession).where(ShopSession.student_id == stu.id, ShopSession.checked_in_at >= f,
                                                ShopSession.status.in_(REAL_STATUSES),
                                                ShopSession.checked_in_at < t)).all()
    res = db.scalars(select(Reservation).where(Reservation.student_id == stu.id, Reservation.start_at >= f,
                                               Reservation.start_at < t)).all()
    return {"student": stu, "sessions": len(sess), "hours": round(sum((s.duration_minutes or 0) for s in sess) / 60, 1),
            "reservations": len(res), "no_shows": sum(1 for r in res if r.status == "no_show"),
            "cancelled": sum(1 for r in res if r.status == "cancelled"),
            "auto_closed": sum(1 for s in sess if s.auto_closed)}


# ----------------------------------------------------------------------- CSV export
SENSITIVE = {"password_hash", "code_hash", "claim_token", "pending_profile", "body_html"}
ADMIN_ONLY = {"staff_users", "audit_log", "email_verifications", "rotating_codes", "rate_events", "idempotency_keys",
              "email_outbox"}
SKIP = {"file_blobs"}


def exportable_tables() -> list[str]:
    return sorted(t.name for t in Base.metadata.sorted_tables if t.name not in SKIP)


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, datetime):
        v = clock.to_local(v).isoformat(timespec="seconds") if v.tzinfo else v.isoformat()
    s = str(v)
    if s[:1] in ("=", "+", "-", "@", "\t", "\r"):  # CSV formula injection guard
        s = "'" + s
    return s


def csv_rows(db: Session, table: str):
    t = Base.metadata.tables.get(table)
    if t is None or table in SKIP:
        raise KeyError(table)
    cols = [c for c in t.columns if c.name not in SENSITIVE and not (table == "rotating_codes" and c.name == "code")]
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([c.name for c in cols])
    yield buf.getvalue()
    for row in db.execute(select(*cols).execution_options(yield_per=500)):
        buf.seek(0)
        buf.truncate()
        w.writerow([_cell(v) for v in row])
        yield buf.getvalue()
