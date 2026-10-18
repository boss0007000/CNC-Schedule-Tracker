from datetime import timedelta

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.db import get_db
from app.errors import UserError
from app.services import contact, holds
from fastapi.responses import RedirectResponse
from app.web import get_form
from app.models import Approval, MachineHold, ShopSession, Project, Reservation, Student, TimeChangeRequest, WaitlistEntry
from app.services import reservations
from app.services import settings as shop_settings
from app.services import strikes as strikes_svc
from app.web import render, student_required

router = APIRouter()


@router.get("/my")
def my(request: Request, joined: int = 0, msg: str = "", student: Student = Depends(student_required), db: Session = Depends(get_db)):
    now = clock.now()
    projects_ = db.scalars(select(Project).where(Project.student_id == student.id).order_by(Project.id.desc())).all()
    approvals = db.scalars(select(Approval).where(Approval.student_id == student.id).order_by(Approval.id.desc())).all()
    upcoming = db.scalars(select(Reservation).where(Reservation.student_id == student.id,
                                                    Reservation.status.in_(("confirmed", "checked_in")),
                                                    Reservation.end_at > now).order_by(Reservation.start_at)).all()
    waits = db.scalars(select(WaitlistEntry).where(WaitlistEntry.student_id == student.id,
                                                   WaitlistEntry.status.in_(("waiting", "offered")))).all()
    incoming = db.scalars(select(TimeChangeRequest).where(TimeChangeRequest.target_student_id == student.id,
                                                          TimeChangeRequest.status == "pending",
                                                          TimeChangeRequest.expires_at > now)).all()
    outgoing = db.scalars(select(TimeChangeRequest).where(TimeChangeRequest.requester_id == student.id,
                                                          TimeChangeRequest.status == "pending",
                                                          TimeChangeRequest.expires_at > now)).all()
    my_holds = db.scalars(select(MachineHold).where(MachineHold.student_id == student.id, MachineHold.released_at.is_(None),
                                                    MachineHold.expires_at > now)).all()
    since = now - timedelta(hours=holds.RECENT_HOURS)
    recent = {}
    for ss in db.scalars(select(ShopSession).where(ShopSession.student_id == student.id,
                                                   (ShopSession.status == "active") | (ShopSession.checked_out_at >= since))
                         .order_by(ShopSession.id.desc())):
        recent.setdefault(ss.machine_id, ss.machine)
    held_ids = {h.machine_id for h in my_holds}
    recent = [m for mid, m in recent.items() if mid not in held_ids]
    usage = {a.id: reservations.approval_usage(db, a) for a in approvals}
    return render(request, "my.html", {"student": student, "projects": projects_, "approvals": approvals,
                                       "upcoming": upcoming, "waits": waits, "usage": usage, "my_holds": my_holds, "recent_machines": recent, "incoming": incoming, "outgoing": outgoing, "joined": bool(joined), "msg": msg,
                                       "suspended": student.suspended_until and student.suspended_until > now,
                                       "strikes": strikes_svc.active(db, student, now), "strike_limit": shop_settings.load(db).noshow_threshold})


@router.post("/my/contact")
def my_contact(request: Request, form=Depends(get_form), student: Student = Depends(student_required),
               db: Session = Depends(get_db)):
    try:
        contact.update_contact(db, student, form.get("phone"), form.get("show_contact", "none"),
                               form.get("allow_requests") == "on")
    except UserError as e:
        db.rollback()
        return RedirectResponse(f"/my?msg={e.message}", status_code=303)
    db.commit()
    return RedirectResponse("/my?msg=Contact settings saved", status_code=303)
