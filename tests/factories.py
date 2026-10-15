"""Test helpers that build domain objects directly."""
from datetime import date, datetime, time, timedelta

from sqlalchemy import select

from app import clock
from app.models import (
    Approval, ApprovalMachine, Certification, Machine, Project, Reservation, SafetyWaiver, ShiftSchedule, Student,
)
from app.services import numbers, reservations


def student(db, first):
    return db.scalar(select(Student).where(Student.email.like(f"{first}.%")))


def machine(db, name):
    return db.scalar(select(Machine).where(Machine.name == name))


def at(day: date | int, hour: int, minute: int = 0) -> datetime:
    """Local wall-clock time. int day = offset from 'today' (Wed 2026-03-04)."""
    if isinstance(day, int):
        day = clock.to_local(clock.now()).date() + timedelta(days=day)
    return clock.local_dt(day, time(hour, minute))


def make_approval(db, stu, machine_names, hours=20.0, sessions=None, expires_days=90):
    p = Project(student_id=stu.id, title="P", description="d" * 20, status="approved",
                machine_ids=[machine(db, n).id for n in machine_names])
    db.add(p)
    db.flush()
    a = Approval(number=numbers.next_approval_number(db), project_id=p.id, student_id=stu.id, max_total_hours=hours,
                 max_sessions=sessions, expires_at=clock.now() + timedelta(days=expires_days), approved_by="sam")
    db.add(a)
    db.flush()
    for n in machine_names:
        db.add(ApprovalMachine(approval_id=a.id, machine_id=machine(db, n).id))
    db.flush()
    return a


def certify(db, stu, group=None, machine_name=None, expires_days=365):
    from app.models import MachineGroup
    kw = {}
    if group:
        kw["group_id"] = db.scalar(select(MachineGroup.id).where(MachineGroup.name == group))
    else:
        kw["machine_id"] = machine(db, machine_name).id
    db.add(Certification(student_id=stu.id, granted_by="sam",
                         expires_at=None if expires_days is None else clock.now() + timedelta(days=expires_days), **kw))
    db.flush()


def waiver(db, stu, version="1"):
    if db.scalar(select(SafetyWaiver.id).where(SafetyWaiver.student_id == stu.id, SafetyWaiver.version == version)):
        return
    db.add(SafetyWaiver(student_id=stu.id, version=version))
    db.flush()


def book(db, stu, a, machine_name, start, hours=1, **kw):
    return reservations.create(db, stu, a.number if a else None, machine(db, machine_name).id, start,
                               start + timedelta(hours=hours), waiver_accepted=True, **kw)


def new_student(db, email, name="Test Student", standing="junior"):
    s = Student(email=email, full_name=name, standing=standing)
    db.add(s)
    db.flush()
    return s
