"""Reports, CSV exports, QR codes, backups, settings (M8)."""
from datetime import date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.db import get_db
from app.errors import UserError
from app.models import ShiftSchedule, ShopHours, StaffUser
from app.services import audit, backup, pdfs, qr, reports, retention, shopconfig, staffauth
from app.services import settings as shop_settings
from app.web import get_form, render, staff_actor, staff_dep

router = APIRouter(prefix="/admin")
monitor = staff_dep("monitor")
admin_only = staff_dep("admin")


def _range(frm: str, to: str) -> tuple[date, date]:
    today = clock.to_local(clock.now()).date()
    try:
        end = datetime.strptime(to, "%Y-%m-%d").date() if to else today
        start = datetime.strptime(frm, "%Y-%m-%d").date() if frm else end - timedelta(days=29)
    except ValueError:
        end, start = today, today - timedelta(days=29)
    if start > end:
        start, end = end, start
    return start, min(end, today + timedelta(days=366))


# ---------------------------------------------------------------- reports
@router.get("/reports")
def reports_page(request: Request, frm: str = "", to: str = "", email: str = "", user: StaffUser = Depends(monitor),
                 db: Session = Depends(get_db)):
    start, end = _range(frm, to)
    util = reports.utilization(db, start, end)
    return render(request, "admin/reports.html", {
        "start": start, "end": end, "util": util, "peaks": reports.peak_hours(db, start, end),
        "demand": reports.demand(db, start, end), "noshow": reports.no_show_rates(db, start, end),
        "issues": reports.maintenance_issues(db, start, end), "email": email,
        "hist": reports.student_history(db, email, start, end) if email else None,
        "max_peak": max([p["count"] for p in reports.peak_hours(db, start, end)["hours"]] + [1]),
        "tables": reports.exportable_tables(), "admin_only": reports.ADMIN_ONLY})


@router.get("/export/{table}.csv")
def export_csv(table: str, user: StaffUser = Depends(monitor), db: Session = Depends(get_db)):
    if table in reports.ADMIN_ONLY and not staffauth.can(user, "admin"):
        raise HTTPException(403, "Only admins can export this table.")
    if table not in reports.exportable_tables():
        raise HTTPException(404, "Unknown table.")
    audit.record(db, staff_actor(user), "export.csv", "table", table)
    db.commit()
    return StreamingResponse(reports.csv_rows(db, table), media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{table}-{clock.now():%Y%m%d}.csv"',
                                      "Cache-Control": "no-store"})


# ---------------------------------------------------------------- QR codes
@router.get("/qr")
def qr_page(request: Request, user: StaffUser = Depends(monitor), db: Session = Depends(get_db)):
    return render(request, "admin/qr.html", {"flows": [(k, lbl, hint, qr.url_for(path)) for k, lbl, hint, path in qr.FLOWS]})


@router.get("/qr/print.pdf")
def qr_sheet(user: StaffUser = Depends(monitor), db: Session = Depends(get_db)):
    pdf = pdfs.qr_sheet_pdf(shop_settings.load(db).shop_name)
    return Response(pdf, media_type="application/pdf", headers={"Content-Disposition": 'inline; filename="shop-qr-codes.pdf"'})


@router.get("/qr/{name}.png")
def qr_download(name: str, user: StaffUser = Depends(monitor)):
    try:
        data = qr.png(name, 20)
    except KeyError:
        raise HTTPException(404, "Unknown code.") from None
    return Response(data, media_type="image/png", headers={"Content-Disposition": f'attachment; filename="qr-{name}.png"'})


@router.get("/qr/{name}.pdf")
def qr_poster(name: str, user: StaffUser = Depends(monitor)):
    if qr.flow(name) is None:
        raise HTTPException(404, "Unknown code.")
    return Response(pdfs.qr_poster_pdf(name), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="qr-{name}.pdf"'})


# ---------------------------------------------------------------- backups
@router.get("/backups")
def backups_page(request: Request, user: StaffUser = Depends(admin_only)):
    from app.config import get_config
    c = get_config()
    return render(request, "admin/backups.html", {"files": backup.list_backups(), "msg": request.query_params.get("msg"),
                                                  "dest": c.backup_dir, "put": bool(c.backup_put_url)})


@router.post("/backups/run")
def backups_run(user: StaffUser = Depends(admin_only), db: Session = Depends(get_db)):
    res = backup.run_backup()
    audit.record(db, staff_actor(user), "backup.run", "system", None, after={"name": res["name"], "remote": res["remote"]})
    db.commit()
    return RedirectResponse(f"/admin/backups?msg=Backup created: {res['name']}", status_code=303)


@router.get("/backups/{name}")
def backups_download(name: str, user: StaffUser = Depends(admin_only), db: Session = Depends(get_db)):
    p = backup.backup_path(name)
    if p is None:
        raise HTTPException(404, "Backup not found.")
    audit.record(db, staff_actor(user), "backup.download", "system", None, after={"name": name})
    db.commit()
    return FileResponse(p, media_type="application/gzip", filename=name)


# ---------------------------------------------------------------- settings
def _settings_ctx(db, errs=None, msg=None):
    cfg = shop_settings.load(db)
    hours = {h.weekday: h for h in db.scalars(select(ShopHours).where(ShopHours.weekday.is_not(None)))}
    return {"meta": shop_settings.SETTING_META, "cfg": cfg, "email_domain": cfg.email_domain, "hours": hours,
            "weekdays": shopconfig.WEEKDAYS, "errs": errs or [], "msg": msg,
            "exceptions": db.scalars(select(ShopHours).where(ShopHours.on_date.is_not(None)).order_by(ShopHours.on_date)).all(),
            "shifts": db.scalars(select(ShiftSchedule).where(ShiftSchedule.end_at > clock.now() - timedelta(days=1))
                                 .order_by(ShiftSchedule.start_at).limit(100)).all(),
            "staff": [u for u in db.scalars(select(StaffUser).where(StaffUser.active.is_(True))) if staffauth.can(u, "checkins.decide")]}


@router.get("/settings")
def settings_page(request: Request, user: StaffUser = Depends(admin_only), db: Session = Depends(get_db)):
    return render(request, "admin/settings.html", _settings_ctx(db, msg=request.query_params.get("msg")))


@router.post("/settings")
def settings_save(request: Request, form=Depends(get_form), user: StaffUser = Depends(admin_only), db: Session = Depends(get_db)):
    before = {k: getattr(shop_settings.load(db), k) for k, _, _ in shop_settings.SETTING_META}
    try:
        for key, kind, label in shop_settings.SETTING_META:
            if kind == "bool":
                val = form.get(key) == "on"
            else:
                raw = form.get(key)
                if raw is None:
                    continue
                if raw.strip() == "" and key == "allowed_email_domain":
                    val = None
                elif raw.strip() == "":
                    raise UserError(f"{label} can't be empty.")
                else:
                    try:
                        val = shop_settings.coerce(kind, raw)
                    except ValueError:
                        raise UserError(f"{label} must be a number.") from None
                    if kind in ("int", "float") and val < 0:
                        raise UserError(f"{label} can't be negative.")
            if key == "slot_minutes" and not 10 <= val <= 480:
                raise UserError("Slot length must be between 10 and 480 minutes.")
            if key == "rotating_code_interval_min" and not 1 <= val <= 120:
                raise UserError("Code rotation interval must be between 1 and 120 minutes.")
            shop_settings.set_value(db, key, val)
        after = {k: getattr(shop_settings.load(db), k) for k, _, _ in shop_settings.SETTING_META}
        changed = {k: v for k, v in after.items() if before[k] != v}
        audit.record(db, staff_actor(user), "settings.update", "settings", None,
                     {k: before[k] for k in changed}, changed)
    except UserError as e:
        db.rollback()
        return render(request, "admin/settings.html", _settings_ctx(db, [e.message]), status=400)
    db.commit()
    return RedirectResponse("/admin/settings?msg=Settings saved", status_code=303)


def _apply(db, fn, redirect="/admin/settings"):
    try:
        fn()
    except (UserError, ValueError) as e:
        db.rollback()
        msg = e.message if isinstance(e, UserError) else "Invalid input."
        return RedirectResponse(f"{redirect}?msg={msg}", status_code=303)
    db.commit()
    return RedirectResponse(f"{redirect}?msg=Saved", status_code=303)


@router.post("/settings/hours")
def hours_save(form=Depends(get_form), user: StaffUser = Depends(admin_only), db: Session = Depends(get_db)):
    return _apply(db, lambda: shopconfig.save_weekly_hours(db, staff_actor(user), form))


@router.post("/settings/exceptions")
def exception_add(form=Depends(get_form), user: StaffUser = Depends(admin_only), db: Session = Depends(get_db)):
    return _apply(db, lambda: shopconfig.add_exception(db, staff_actor(user), form.get("date", ""), form.get("closed") == "on",
                                                       form.get("open_time"), form.get("close_time"), form.get("label", "")))


@router.post("/settings/exceptions/{eid}/delete")
def exception_delete(eid: int, user: StaffUser = Depends(admin_only), db: Session = Depends(get_db)):
    row = db.get(ShopHours, eid)
    if row is None:
        raise HTTPException(404, "Not found.")
    return _apply(db, lambda: shopconfig.delete_exception(db, staff_actor(user), row))


def _local(raw: str) -> datetime:
    try:
        return datetime.strptime(raw, "%Y-%m-%dT%H:%M").replace(tzinfo=clock.tz()).astimezone(clock.UTC)
    except ValueError:
        raise UserError("Enter a valid date and time.") from None


@router.post("/settings/shifts")
def shift_add(form=Depends(get_form), user: StaffUser = Depends(admin_only), db: Session = Depends(get_db)):
    return _apply(db, lambda: shopconfig.add_shift(db, staff_actor(user), int(form.get("staff_id") or 0),
                                                   _local(form.get("start", "")), _local(form.get("end", "")),
                                                   int(form.get("repeat_weeks") or 0)))


@router.post("/settings/shifts/{sid}/delete")
def shift_delete(sid: int, user: StaffUser = Depends(admin_only), db: Session = Depends(get_db)):
    row = db.get(ShiftSchedule, sid)
    if row is None:
        raise HTTPException(404, "Not found.")
    return _apply(db, lambda: shopconfig.delete_shift(db, staff_actor(user), row))


@router.post("/settings/purge")
def purge(request: Request, form=Depends(get_form), user: StaffUser = Depends(admin_only), db: Session = Depends(get_db)):
    days = int(form.get("days") or shop_settings.load(db).retention_days)
    dry = form.get("confirm") != "PURGE"
    counts = retention.purge(db, days, staff_actor(user), dry_run=dry, include_incidents=form.get("incidents") == "on")
    db.commit()
    ctx = _settings_ctx(db, msg=("Preview only — type PURGE to delete these records." if dry else "Purge complete."))
    ctx["purge_counts"], ctx["purge_days"], ctx["purge_dry"] = counts, days, dry
    return render(request, "admin/settings.html", ctx)
