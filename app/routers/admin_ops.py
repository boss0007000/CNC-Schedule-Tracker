"""Staff operations: approvals queue (M4); reservations, sessions, machines, students, incidents (later)."""
from datetime import datetime, time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock, emailer
from app.db import get_db
from app.errors import UserError
from app.models import Approval, Machine, Project, ProjectFile, StaffUser
from app.services import files, projects
from app.web import get_form, render, staff_actor, staff_dep

router = APIRouter(prefix="/admin")
supervisor = staff_dep("supervisor")
monitor = staff_dep("monitor")


def _flush_later(db: Session):
    db.commit()
    emailer.flush_outbox(db)
    db.commit()


# ---------------------------------------------------------------- approvals queue
@router.get("/approvals")
def approvals(request: Request, status: str = "submitted", user: StaffUser = Depends(monitor),
              db: Session = Depends(get_db)):
    stmt = select(Project).order_by(Project.updated_at.desc())
    if status != "all":
        stmt = stmt.where(Project.status == status)
    counts = dict(db.execute(select(Project.status, func.count()).group_by(Project.status)).all())
    return render(request, "admin/approvals.html", {"projects": db.scalars(stmt.limit(200)).all(), "status": status,
                                                    "counts": counts})


def _detail_ctx(db, p, errs=None):
    machines = projects.active_machines(db)
    approvals_ = db.scalars(select(Approval).where(Approval.project_id == p.id)).all()
    return {"p": p, "machines": machines, "names": {m.id: m.name for m in db.scalars(select(Machine))},
            "approvals": approvals_, "errs": errs or [], "file_link": files.signed_link,
            "default_expiry": clock.to_local(clock.now()).date(), "default_days": 120}


@router.get("/approvals/{pid}")
def approval_detail(pid: int, request: Request, user: StaffUser = Depends(monitor), db: Session = Depends(get_db)):
    p = db.get(Project, pid)
    if p is None:
        raise HTTPException(404, "Project not found.")
    ctx = _detail_ctx(db, p)
    from app.services import settings as shop_settings
    from datetime import timedelta
    ctx["default_expiry"] = (clock.to_local(clock.now()) + timedelta(days=shop_settings.load(db).approval_default_days)).date()
    return render(request, "admin/approval_detail.html", ctx)


def _parse_date_end(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        d = datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise UserError("Expiry date must look like 2026-12-31.") from None
    return clock.local_dt(d, time(23, 59))


@router.post("/approvals/{pid}/decide")
def decide(pid: int, request: Request, form=Depends(get_form), user: StaffUser = Depends(supervisor),
           db: Session = Depends(get_db)):
    p = db.get(Project, pid)
    if p is None:
        raise HTTPException(404, "Project not found.")
    actor = staff_actor(user)
    action = form.get("action")
    try:
        if action == "approve":
            def num(key, cast):
                raw = (form.get(key) or "").strip()
                try:
                    return cast(raw) if raw else None
                except ValueError:
                    raise UserError(f"“{raw}” isn't a valid number.") from None
            projects.approve(db, p, actor, [int(i) for i in form.getlist("machine_ids") if str(i).isdigit()],
                             (form.get("restrictions") or "").splitlines(), num("max_total_hours", float),
                             num("max_sessions", int), _parse_date_end(form.get("expires_on")),
                             (form.get("comments") or "").strip(), (form.get("conditions") or "").strip())
        elif action == "reject":
            projects.reject(db, p, actor, form.get("reason", ""))
        elif action == "changes":
            projects.request_changes(db, p, actor, form.get("comments", ""))
        else:
            raise UserError("Unknown action.")
    except UserError as e:
        db.rollback()
        p = db.get(Project, pid)
        return render(request, "admin/approval_detail.html", _detail_ctx(db, p, [e.message]), status=400)
    _flush_later(db)
    return RedirectResponse(f"/admin/approvals/{pid}?done=1", status_code=303)


@router.post("/approvals/{aid}/revoke")
def revoke(aid: int, form=Depends(get_form), user: StaffUser = Depends(supervisor), db: Session = Depends(get_db)):
    a = db.get(Approval, aid)
    if a is None:
        raise HTTPException(404, "Approval not found.")
    projects.revoke_approval(db, a, staff_actor(user), form.get("reason", ""))
    return RedirectResponse(f"/admin/approvals/{a.project_id}", status_code=303)


@router.get("/approval-records/{aid}")
def approval_print(aid: int, request: Request, user: StaffUser = Depends(monitor), db: Session = Depends(get_db)):
    from app.services import notify
    a = db.get(Approval, aid)
    if a is None:
        raise HTTPException(404, "Approval not found.")
    return render(request, "approval_record.html", {"a": a, "text": notify.approval_text(a), "pdf": f"/approval/{a.number}/pdf?t="})


# ---------------------------------------------------------------- signed file downloads
@router.get("/files/{kind}/{ident}")
def serve_file(kind: str, ident: int, t: str | None = None, user: StaffUser = Depends(monitor),
               db: Session = Depends(get_db)):
    if kind not in ("project", "damage") or not files.check_link(kind, ident, t):
        raise HTTPException(403, "This file link is invalid or has expired. Reopen the page to get a fresh link.")
    if kind == "project":
        row = db.get(ProjectFile, ident)
        key, name = (row.storage_key, row.original_name) if row else (None, None)
    else:
        from app.models import DamageReport
        row = db.get(DamageReport, ident)
        key, name = (row.photo_key, row.photo_name) if row else (None, None)
    if not key:
        raise HTTPException(404, "File not found.")
    try:
        data = files.read(db, key)
    except FileNotFoundError:
        raise HTTPException(404, "File is no longer available.") from None
    mime = files.download_mime(name)
    disp = "inline" if mime in ("application/pdf", "image/png", "image/jpeg", "image/webp") else "attachment"
    return Response(data, media_type=mime, headers={
        "Content-Disposition": f'{disp}; filename="{files.sanitize_filename(name)}"',
        "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox", "Cache-Control": "private, no-store"})


# ================================================================ shared helpers
def _done(db: Session, url: str):
    db.commit()
    return RedirectResponse(url, status_code=303)


def _local_dt(raw: str | None, field: str = "time") -> datetime:
    try:
        d = datetime.strptime((raw or "").strip(), "%Y-%m-%dT%H:%M")
    except ValueError:
        raise UserError(f"Enter a valid {field} (date and time).") from None
    return d.replace(tzinfo=clock.tz()).astimezone(clock.UTC)


def _err(request, tpl, ctx, e: UserError, status=400):
    return render(request, tpl, {**ctx, "errs": [e.message]}, status=status)


# ================================================================ dashboard + emergency
@router.get("")
def dashboard(request: Request, user: StaffUser = Depends(monitor), db: Session = Depends(get_db)):
    from app.services import board, lifecycle
    from app.services import settings as shop_settings
    lifecycle.lazy_sweep(db)
    ov = board.admin_overview(db)
    cfg = shop_settings.load(db)
    return render(request, "admin/dashboard.html", {**ov, "cfg": cfg, "errs": [], "now": clock.now()})


@router.post("/emergency")
def emergency_post(request: Request, form=Depends(get_form), user: StaffUser = Depends(supervisor),
                   db: Session = Depends(get_db)):
    from app.services import emergency
    action = form.get("action")
    try:
        if action == "close":
            if (form.get("confirm") or "").strip().upper() != "CLOSE":
                raise UserError("Type CLOSE to confirm the emergency close.")
            n = emergency.close(db, staff_actor(user), form.get("message"))
        elif action == "reopen":
            if form.get("confirm") != "on":
                raise UserError("Tick the box to confirm reopening the shop.")
            emergency.reopen(db, staff_actor(user))
        else:
            raise UserError("Unknown action.")
    except UserError as e:
        db.rollback()
        from app.services import board
        from app.services import settings as shop_settings
        return render(request, "admin/dashboard.html", {**board.admin_overview(db), "cfg": shop_settings.load(db),
                                                        "errs": [e.message], "now": clock.now()}, status=400)
    db.commit()
    emailer.flush_outbox(db)
    return _done(db, "/admin")


# ================================================================ reservations
@router.get("/reservations")
def reservations_page(request: Request, view: str = "list", date: str = "", machine: int = 0, status: str = "",
                      q: str = "", user: StaffUser = Depends(monitor), db: Session = Depends(get_db)):
    from datetime import timedelta
    from app.models import Reservation, Student
    from app.services import shop, staffres
    from app.services import settings as shop_settings
    cfg = shop_settings.load(db)
    today = clock.to_local(clock.now()).date()
    try:
        day = datetime.strptime(date, "%Y-%m-%d").date() if date else today
    except ValueError:
        day = today
    machines = projects.active_machines(db)
    ctx = {"view": view, "day": day, "machines": machines, "machine": machine, "status": status, "q": q,
           "errs": [], "conflicts": staffres.conflicts(db), "prev": day - timedelta(days=1),
           "next": day + timedelta(days=1), "msg": request.query_params.get("msg")}
    if view == "calendar":
        grid = shop.slot_grid(db, day, cfg.slot_minutes)
        ws = clock.local_dt(day, datetime.min.time())
        rows = db.scalars(select(Reservation).where(Reservation.start_at < ws + timedelta(days=1), Reservation.end_at > ws,
                                                    Reservation.status.in_(("confirmed", "checked_in", "completed")))).all()
        cell = {}
        for r in rows:
            for s, e in grid:
                if r.start_at < e and r.end_at > s:
                    cell[(r.machine_id, s)] = r
        ctx.update(grid=grid, cell=cell)
    else:
        stmt = select(Reservation).order_by(Reservation.start_at)
        start = clock.local_dt(day, datetime.min.time())
        stmt = stmt.where(Reservation.start_at >= start, Reservation.start_at < start + timedelta(days=7))
        if machine:
            stmt = stmt.where(Reservation.machine_id == machine)
        if status:
            stmt = stmt.where(Reservation.status == status)
        if q:
            stmt = stmt.join(Student, Student.id == Reservation.student_id).where(
                Student.email.like(f"%{q.lower()}%") | Student.full_name.like(f"%{q}%") | Reservation.ref.like(f"%{q.upper()}%"))
        ctx["rows"] = db.scalars(stmt.limit(300)).all()
    return render(request, "admin/reservations.html", ctx)


@router.post("/reservations/new")
def reservation_new(request: Request, form=Depends(get_form), user: StaffUser = Depends(supervisor),
                    db: Session = Depends(get_db)):
    from app.services import staffres
    try:
        r = staffres.manual_create(db, staff_actor(user), form.get("email", ""), int(form.get("machine_id") or 0),
                                   _local_dt(form.get("start"), "start time"), float(form.get("hours") or 1),
                                   form.get("approval", ""), form.get("override") == "on", form.get("reason", ""))
    except (UserError, ValueError) as e:
        db.rollback()
        msg = e.message if isinstance(e, UserError) else "Check the numbers you entered."
        return RedirectResponse(f"/admin/reservations?msg={msg}", status_code=303)
    db.commit()
    emailer.flush_outbox(db)
    return _done(db, f"/admin/reservations?msg=Created {r.ref}")


@router.post("/reservations/{rid}/{action}")
def reservation_action(rid: int, action: str, form=Depends(get_form), user: StaffUser = Depends(supervisor),
                       db: Session = Depends(get_db)):
    from app.models import Reservation
    from app.services import reservations as rsv, sessions, staffres
    r = db.get(Reservation, rid)
    if r is None:
        raise HTTPException(404, "Reservation not found.")
    actor = staff_actor(user)
    try:
        if action == "cancel":
            rsv.cancel(db, r, actor=actor, by_staff=True, reason=form.get("reason", ""))
        elif action == "release":
            rsv.cancel(db, r, actor=actor, by_staff=True, reason="released by staff", quiet=True)
        elif action == "noshow":
            staffres.mark_no_show(db, actor, r)
        elif action == "checkin":
            sessions.force_check_in(db, r, actor, form.get("reason", ""))
        elif action == "move":
            staffres.move(db, actor, r, int(form.get("machine_id") or r.machine_id), _local_dt(form.get("start"), "start"),
                          _local_dt(form.get("end"), "end"))
        else:
            raise HTTPException(404, "Unknown action.")
    except (UserError, ValueError) as e:
        db.rollback()
        msg = e.message if isinstance(e, UserError) else "Invalid input."
        return RedirectResponse(f"/admin/reservations?msg={msg}", status_code=303)
    db.commit()
    emailer.flush_outbox(db)
    return _done(db, "/admin/reservations?msg=Done")


# ================================================================ sessions
@router.get("/sessions")
def sessions_page(request: Request, show: str = "active", user: StaffUser = Depends(monitor), db: Session = Depends(get_db)):
    from app.models import Reservation, ShopSession
    from app.services import lifecycle
    lifecycle.lazy_sweep(db)
    stmt = select(ShopSession).order_by(ShopSession.id.desc())
    if show == "active":
        stmt = stmt.where(ShopSession.status == "active")
    elif show == "review":
        stmt = stmt.where(ShopSession.needs_review.is_(True))
    rows = db.scalars(stmt.limit(200)).all()
    today = clock.to_local(clock.now()).date()
    confirmed = [r for r in db.scalars(select(Reservation).where(Reservation.status == "confirmed",
                                                                  Reservation.end_at > clock.now()).order_by(Reservation.start_at).limit(100))
                 if clock.to_local(r.start_at).date() == today]
    return render(request, "admin/sessions.html", {"rows": rows, "show": show, "confirmed": confirmed,
                                                   "msg": request.query_params.get("msg"), "now": clock.now()})


@router.post("/sessions/{sid}/{action}")
def session_action(sid: int, action: str, form=Depends(get_form), user: StaffUser = Depends(supervisor),
                   db: Session = Depends(get_db)):
    from app.models import ShopSession
    from app.services import audit, sessions
    s = db.get(ShopSession, sid)
    if s is None:
        raise HTTPException(404, "Session not found.")
    try:
        if action in ("checkout", "close"):
            sessions.force_check_out(db, s, staff_actor(user), form.get("reason", ""))
        elif action == "review":
            s.needs_review = False
            audit.record(db, staff_actor(user), "session.reviewed", "session", s.id)
        else:
            raise HTTPException(404, "Unknown action.")
    except UserError as e:
        db.rollback()
        return RedirectResponse(f"/admin/sessions?msg={e.message}", status_code=303)
    return _done(db, "/admin/sessions?msg=Done")


# ================================================================ machines
admin_only = staff_dep("admin")


@router.get("/machines")
def machines_page(request: Request, user: StaffUser = Depends(monitor), db: Session = Depends(get_db)):
    from app.models import MachineGroup
    from app.services import status
    return render(request, "admin/machines.html", {
        "board": status.board(db, include_inactive=True), "groups": db.scalars(select(MachineGroup).order_by(MachineGroup.name)).all(),
        "errs": [], "msg": request.query_params.get("msg")})


@router.post("/machines/save")
def machine_save(request: Request, form=Depends(get_form), user: StaffUser = Depends(admin_only),
                 db: Session = Depends(get_db)):
    from app.services import machines as ms
    mid = form.get("id")
    try:
        m = db.get(Machine, int(mid)) if mid and mid.isdigit() else None
        ms.save_machine(db, staff_actor(user), m, {
            "name": form.get("name"), "group_id": form.get("group_id"), "description": form.get("description"),
            "requires_certification": form.get("requires_certification") == "on",
            "requires_supervision": form.get("requires_supervision") == "on",
            "access_level": form.get("access_level"),
            "active": form.get("active", "on") == "on" if mid else True, "sort_order": form.get("sort_order")})
    except UserError as e:
        db.rollback()
        return RedirectResponse(f"/admin/machines?msg={e.message}", status_code=303)
    return _done(db, "/admin/machines?msg=Saved")


@router.post("/groups/save")
def group_save(form=Depends(get_form), user: StaffUser = Depends(admin_only), db: Session = Depends(get_db)):
    from app.models import MachineGroup
    from app.services import machines as ms
    gid = form.get("id")
    try:
        ms.save_group(db, staff_actor(user), form.get("name", ""), db.get(MachineGroup, int(gid)) if gid and gid.isdigit() else None)
    except UserError as e:
        db.rollback()
        return RedirectResponse(f"/admin/machines?msg={e.message}", status_code=303)
    return _done(db, "/admin/machines?msg=Saved")


@router.get("/machines/{mid}")
def machine_detail(mid: int, request: Request, user: StaffUser = Depends(monitor), db: Session = Depends(get_db)):
    from app.models import MACHINE_STATUSES, DamageReport, MaintenanceRecord
    from app.services import status
    m = db.get(Machine, mid)
    if m is None:
        raise HTTPException(404, "Machine not found.")
    recs = db.scalars(select(MaintenanceRecord).where(MaintenanceRecord.machine_id == mid)
                      .order_by(MaintenanceRecord.id.desc()).limit(100)).all()
    dmg = db.scalars(select(DamageReport).where(DamageReport.machine_id == mid).order_by(DamageReport.id.desc()).limit(50)).all()
    from app.models import MachineHold
    from app.services import holds as holds_svc
    hold_hist = db.scalars(select(MachineHold).where(MachineHold.machine_id == mid).order_by(MachineHold.id.desc()).limit(20)).all()
    return render(request, "admin/machine.html", {"m": m, "state": status.machine_state(db, m), "records": recs,
                                                  "hold": holds_svc.active_hold(db, mid), "hold_hist": hold_hist,
                                                  "damage": dmg, "statuses": MACHINE_STATUSES, "file_link": files.signed_link,
                                                  "msg": request.query_params.get("msg")})


@router.post("/machines/{mid}/{action}")
def machine_action(mid: int, action: str, form=Depends(get_form), user: StaffUser = Depends(supervisor),
                   db: Session = Depends(get_db)):
    from app.services import machines as ms
    m = db.get(Machine, mid)
    if m is None:
        raise HTTPException(404, "Machine not found.")
    actor = staff_actor(user)
    try:
        if action == "override":
            ms.set_override(db, actor, m, form.get("status"), form.get("reason", ""))
        elif action == "maintenance":
            ms.open_maintenance(db, actor, m, form.get("kind", "issue"), form.get("description", ""), form.get("state", "none"),
                                _local_dt(form.get("window_start")) if form.get("window_start") else None,
                                _local_dt(form.get("window_end")) if form.get("window_end") else None)
        elif action == "clear":
            ms.clear_machine(db, actor, m, form.get("note", ""))
        else:
            raise HTTPException(404, "Unknown action.")
    except UserError as e:
        db.rollback()
        return RedirectResponse(f"/admin/machines/{mid}?msg={e.message}", status_code=303)
    return _done(db, f"/admin/machines/{mid}?msg=Saved")


@router.post("/maintenance/{rid}/close")
def maintenance_close(rid: int, user: StaffUser = Depends(supervisor), db: Session = Depends(get_db)):
    from app.models import MaintenanceRecord
    from app.services import machines as ms
    rec = db.get(MaintenanceRecord, rid)
    if rec is None:
        raise HTTPException(404, "Record not found.")
    try:
        ms.close_record(db, staff_actor(user), rec)
    except UserError as e:
        db.rollback()
        return RedirectResponse(f"/admin/machines/{rec.machine_id}?msg={e.message}", status_code=303)
    return _done(db, f"/admin/machines/{rec.machine_id}?msg=Closed")


# ================================================================ students & certifications
@router.get("/students")
def students_page(request: Request, q: str = "", user: StaffUser = Depends(monitor), db: Session = Depends(get_db)):
    from app.models import Student
    stmt = select(Student).order_by(Student.full_name)
    if q:
        stmt = stmt.where(Student.email.like(f"%{q.lower()}%") | Student.full_name.like(f"%{q}%"))
    return render(request, "admin/students.html", {"rows": db.scalars(stmt.limit(200)).all(), "q": q, "now": clock.now()})


@router.get("/students/{sid}")
def student_detail(sid: int, request: Request, user: StaffUser = Depends(monitor), db: Session = Depends(get_db)):
    from app.models import Certification, MachineGroup, Project, Reservation, SafetyWaiver, ShopSession, Student
    from app.services import settings as shop_settings
    s = db.get(Student, sid)
    if s is None:
        raise HTTPException(404, "Student not found.")
    cfg = shop_settings.load(db)
    return render(request, "admin/student.html", {
        "s": s, "now": clock.now(), "msg": request.query_params.get("msg"), "machines": projects.active_machines(db),
        "groups": db.scalars(select(MachineGroup).order_by(MachineGroup.name)).all(),
        "certs": db.scalars(select(Certification).where(Certification.student_id == sid).order_by(Certification.id.desc())).all(),
        "res": db.scalars(select(Reservation).where(Reservation.student_id == sid).order_by(Reservation.start_at.desc()).limit(50)).all(),
        "sess": db.scalars(select(ShopSession).where(ShopSession.student_id == sid).order_by(ShopSession.id.desc()).limit(50)).all(),
        "projs": db.scalars(select(Project).where(Project.student_id == sid).order_by(Project.id.desc())).all(),
        "waiver": db.scalar(select(SafetyWaiver).where(SafetyWaiver.student_id == sid, SafetyWaiver.version == cfg.waiver_version)),
        "waivers": db.scalars(select(SafetyWaiver).where(SafetyWaiver.student_id == sid)).all(), "cfg": cfg})


@router.post("/students/{sid}/{action}")
def student_action(sid: int, action: str, form=Depends(get_form), user: StaffUser = Depends(supervisor),
                   db: Session = Depends(get_db)):
    from app.models import Student
    from app.services import students as ss
    s = db.get(Student, sid)
    if s is None:
        raise HTTPException(404, "Student not found.")
    actor = staff_actor(user)
    try:
        if action == "cert":
            exp = _parse_date_end(form.get("expires_on"))
            target = form.get("target", "")
            kind, _, ident = target.partition(":")
            ss.grant_certification(db, s, actor, int(ident) if kind == "m" and ident.isdigit() else None,
                                   int(ident) if kind == "g" and ident.isdigit() else None, exp, form.get("notes", ""))
        elif action == "suspend":
            ss.suspend(db, s, actor, int(form.get("days") or 0), form.get("reason", ""))
        elif action == "unsuspend":
            ss.clear_suspension(db, s, actor, form.get("reason", ""))
        else:
            raise HTTPException(404, "Unknown action.")
    except (UserError, ValueError) as e:
        db.rollback()
        msg = e.message if isinstance(e, UserError) else "Invalid input."
        return RedirectResponse(f"/admin/students/{sid}?msg={msg}", status_code=303)
    return _done(db, f"/admin/students/{sid}?msg=Saved")


@router.post("/certs/{cid}/revoke")
def cert_revoke(cid: int, user: StaffUser = Depends(supervisor), db: Session = Depends(get_db)):
    from app.models import Certification
    from app.services import students as ss
    c = db.get(Certification, cid)
    if c is None:
        raise HTTPException(404, "Certification not found.")
    try:
        ss.revoke_certification(db, c, staff_actor(user))
    except UserError as e:
        db.rollback()
        return RedirectResponse(f"/admin/students/{c.student_id}?msg={e.message}", status_code=303)
    return _done(db, f"/admin/students/{c.student_id}?msg=Revoked")


# ================================================================ incidents & damage reports
@router.get("/incidents")
def incidents_page(request: Request, user: StaffUser = Depends(monitor), db: Session = Depends(get_db)):
    from app.models import DamageReport, IncidentReport
    return render(request, "admin/incidents.html", {
        "incidents": db.scalars(select(IncidentReport).order_by(IncidentReport.occurred_at.desc()).limit(200)).all(),
        "damage": db.scalars(select(DamageReport).order_by(DamageReport.id.desc()).limit(200)).all(),
        "machines": projects.active_machines(db), "errs": [], "file_link": files.signed_link, "v": {},
        "msg": request.query_params.get("msg"), "now": clock.now()})


@router.post("/incidents/new")
def incident_new(request: Request, form=Depends(get_form), user: StaffUser = Depends(supervisor),
                 db: Session = Depends(get_db)):
    from app.models import DamageReport, IncidentReport
    from app.services import incidents as inc
    try:
        when = _local_dt(form.get("occurred_at"), "incident time") if form.get("occurred_at") else None
        inc.report_incident(db, staff_actor(user), form.get("kind", ""), when,
                            int(form.get("machine_id")) if (form.get("machine_id") or "").isdigit() else None,
                            form.get("student_email", ""), form.get("description", ""), form.get("action_taken", ""),
                            form.get("severity", ""))
    except UserError as e:
        db.rollback()
        return render(request, "admin/incidents.html", {
            "incidents": db.scalars(select(IncidentReport).order_by(IncidentReport.occurred_at.desc()).limit(200)).all(),
            "damage": db.scalars(select(DamageReport).order_by(DamageReport.id.desc()).limit(200)).all(),
            "machines": projects.active_machines(db), "errs": [e.message], "file_link": files.signed_link,
            "v": dict(form), "msg": None, "now": clock.now()}, status=400)
    return _done(db, "/admin/incidents?msg=Incident recorded")


@router.post("/incidents/{iid}/resolve")
def incident_resolve(iid: int, form=Depends(get_form), user: StaffUser = Depends(supervisor), db: Session = Depends(get_db)):
    from app.models import IncidentReport
    from app.services import incidents as inc
    row = db.get(IncidentReport, iid)
    if row is None:
        raise HTTPException(404, "Incident not found.")
    inc.resolve_incident(db, staff_actor(user), row, form.get("resolved", "on") == "on")
    return _done(db, "/admin/incidents")


@router.post("/damage/{did}/status")
def damage_status(did: int, form=Depends(get_form), user: StaffUser = Depends(supervisor), db: Session = Depends(get_db)):
    from app.models import DamageReport
    from app.services import incidents as inc
    row = db.get(DamageReport, did)
    if row is None:
        raise HTTPException(404, "Report not found.")
    try:
        inc.resolve_damage(db, staff_actor(user), row, form.get("status", ""))
    except UserError as e:
        db.rollback()
        return RedirectResponse(f"/admin/incidents?msg={e.message}", status_code=303)
    return _done(db, "/admin/incidents")


# ================================================================ access lists + change requests
@router.get("/access")
def access_page(request: Request, user: StaffUser = Depends(monitor), db: Session = Depends(get_db)):
    from app.models import ACCESS_APPROVED, ACCESS_SUPER, Student
    from app.services import access
    r = access.roster(db)
    known = {s.email: s.full_name for s in db.scalars(select(Student).where(Student.email.in_(
        [x.email for lst in r.values() for x in lst] or [""])))}
    machines_ = projects.active_machines(db)
    return render(request, "admin/access.html", {"roster": r, "known": known, "A": ACCESS_APPROVED, "S": ACCESS_SUPER,
                                                 "msg": request.query_params.get("msg"), "machines": machines_,
                                                 "levels": {m.id: access.machine_levels(m) for m in machines_}})


@router.post("/access")
def access_post(form=Depends(get_form), user: StaffUser = Depends(supervisor), db: Session = Depends(get_db)):
    from app.models import ACCESS_SUPER
    from app.services import access
    action, level = form.get("action"), form.get("level", "")
    try:
        if level == ACCESS_SUPER and user.role != "admin":
            raise UserError("Only an admin can add students to the super approved list.")
        if action == "add":
            res = access.set_levels(db, staff_actor(user), access.parse_emails(form.get("emails", "")), level,
                                    form.get("note", ""))
            msg = f"Added {res['added']}, moved {res['moved']}, already there {res['unchanged']}."
        elif action == "remove":
            existing = access.level_for(db, form.get("email", ""))
            if existing == ACCESS_SUPER and user.role != "admin":
                raise UserError("Only an admin can remove someone from the super approved list.")
            access.remove(db, staff_actor(user), form.get("email", ""))
            msg = "Removed."
        else:
            raise UserError("Unknown action.")
    except UserError as e:
        db.rollback()
        return RedirectResponse(f"/admin/access?msg={e.message}", status_code=303)
    return _done(db, f"/admin/access?msg={msg}")


@router.get("/requests")
def change_requests_page(request: Request, user: StaffUser = Depends(monitor), db: Session = Depends(get_db)):
    from app.models import TimeChangeRequest
    rows = db.scalars(select(TimeChangeRequest).order_by(TimeChangeRequest.id.desc()).limit(200)).all()
    return render(request, "admin/change_requests.html", {"rows": rows})


# ================================================================ holds (staff)
@router.post("/holds/{hid}/{action}")
def hold_action(hid: int, action: str, form=Depends(get_form), user: StaffUser = Depends(supervisor),
                db: Session = Depends(get_db)):
    from app.models import MachineHold
    from app.services import holds
    h = db.get(MachineHold, hid)
    if h is None:
        raise HTTPException(404, "Hold not found.")
    try:
        if action == "release":
            holds.release(db, h, staff_actor(user), "staff")
        elif action == "extend":
            holds.extend(db, h, staff_actor(user), int(form.get("days") or 1))
        else:
            raise HTTPException(404, "Unknown action.")
    except (UserError, ValueError) as e:
        db.rollback()
        msg = e.message if isinstance(e, UserError) else "Invalid number of days."
        return RedirectResponse(f"/admin/machines/{h.machine_id}?msg={msg}", status_code=303)
    return _done(db, f"/admin/machines/{h.machine_id}?msg=Done")


@router.post("/hold-place/{mid}")
def hold_place(mid: int, form=Depends(get_form), user: StaffUser = Depends(supervisor), db: Session = Depends(get_db)):
    from app.models import Student
    from app.services import holds
    m = db.get(Machine, mid)
    stu = db.scalar(select(Student).where(Student.email == (form.get("email") or "").strip().lower()))
    try:
        if m is None:
            raise HTTPException(404, "Machine not found.")
        if stu is None:
            raise UserError("No student with that email has signed in yet.")
        holds.declare(db, stu, m, form.get("kind", ""), via="staff", actor=staff_actor(user), note=form.get("note", ""))
    except UserError as e:
        db.rollback()
        return RedirectResponse(f"/admin/machines/{mid}?msg={e.message}", status_code=303)
    return _done(db, f"/admin/machines/{mid}?msg=Hold placed")


# ================================================================ supervisor check-in approvals
@router.get("/checkins")
def checkins_page(request: Request, user: StaffUser = Depends(monitor), db: Session = Depends(get_db)):
    from app.models import ShopSession
    from app.services import lifecycle, sessions
    lifecycle.lazy_sweep(db)
    pend = db.scalars(select(ShopSession).where(ShopSession.status == "pending").order_by(ShopSession.requested_at)).all()
    recent = db.scalars(select(ShopSession).where(ShopSession.status.in_(("denied",)) | (ShopSession.approved_by.is_not(None)))
                        .order_by(ShopSession.id.desc()).limit(15)).all()
    return render(request, "admin/checkins.html", {"items": [{"s": s, "a": sessions.authorization_summary(db, s)} for s in pend],
                                                   "recent": recent, "msg": request.query_params.get("msg")})


@router.post("/checkins/{sid}/{action}")
def checkin_decide(sid: int, action: str, user: StaffUser = Depends(supervisor), db: Session = Depends(get_db)):
    from app.models import ShopSession
    from app.services import sessions
    s = db.get(ShopSession, sid)
    if s is None:
        raise HTTPException(404, "Not found.")
    try:
        if action == "approve":
            sessions.approve_checkin(db, s, staff_actor(user))
            msg = f"{s.student.public_name} is checked in."
        elif action == "deny":
            sessions.deny_checkin(db, s, staff_actor(user))
            msg = "Denied."
        else:
            raise HTTPException(404, "Unknown action.")
    except UserError as e:
        db.rollback()
        return RedirectResponse(f"/admin/checkins?msg={e.message}", status_code=303)
    return _done(db, f"/admin/checkins?msg={msg}")


@router.post("/access/machines")
def access_machines_save(form=Depends(get_form), user: StaffUser = Depends(admin_only), db: Session = Depends(get_db)):
    """Tick which machines each group may book. Unticked in both columns = staff-run."""
    from app.models import ACCESS_APPROVED, ACCESS_SUPER
    from app.services import access
    changed = 0
    for m in projects.active_machines(db):
        want = set()
        if form.get(f"a_{m.id}") == "on":
            want.add(ACCESS_APPROVED)
        if form.get(f"s_{m.id}") == "on":
            want.add(ACCESS_SUPER)
        if want != access.machine_levels(m):
            access.set_machine_levels(db, staff_actor(user), m, want)
            changed += 1
    return _done(db, f"/admin/access?msg=Saved ({changed} machine{'s' if changed != 1 else ''} changed)")
