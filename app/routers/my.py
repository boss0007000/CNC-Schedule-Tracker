from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.db import get_db
from app.models import Approval, Project, Reservation, Student, WaitlistEntry
from app.services import reservations
from app.web import render, student_required

router = APIRouter()


@router.get("/my")
def my(request: Request, joined: int = 0, student: Student = Depends(student_required), db: Session = Depends(get_db)):
    now = clock.now()
    projects_ = db.scalars(select(Project).where(Project.student_id == student.id).order_by(Project.id.desc())).all()
    approvals = db.scalars(select(Approval).where(Approval.student_id == student.id).order_by(Approval.id.desc())).all()
    upcoming = db.scalars(select(Reservation).where(Reservation.student_id == student.id,
                                                    Reservation.status.in_(("confirmed", "checked_in")),
                                                    Reservation.end_at > now).order_by(Reservation.start_at)).all()
    waits = db.scalars(select(WaitlistEntry).where(WaitlistEntry.student_id == student.id,
                                                   WaitlistEntry.status.in_(("waiting", "offered")))).all()
    usage = {a.id: reservations.approval_usage(db, a) for a in approvals}
    return render(request, "my.html", {"student": student, "projects": projects_, "approvals": approvals,
                                       "upcoming": upcoming, "waits": waits, "usage": usage, "joined": bool(joined),
                                       "suspended": student.suspended_until and student.suspended_until > now})
