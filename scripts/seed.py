"""Seed demo data.

    python -m scripts.seed                 # create base data + demo data (idempotent for base)
    python -m scripts.seed --reset-demo    # WIPE all data and rebuild the demo (refuses in prod)
"""
import argparse
import sys
from datetime import time, timedelta

from sqlalchemy import delete, select

from app import clock, db as dbm
from app.config import get_config
from app.models import (
    Approval, ApprovalMachine, ApprovalRestriction, Base, Certification, Location, Machine, MachineGroup,
    Project, ProjectRevision, SafetyWaiver, ShiftSchedule, ShopHours, StaffUser, Student,
)
from app.services import numbers, staffauth

DEMO_PASSWORD = "demo-password-123"


def seed_base(db) -> None:
    from app.services import shopconfig
    shopconfig.ensure_base_data(db)


def seed_demo(db) -> dict:
    seed_base(db)
    mills = MachineGroup(name="CNC Mills", description="Vertical machining centers")
    lathes = MachineGroup(name="CNC Lathes")
    lasers = MachineGroup(name="Laser Cutters")
    db.add_all([mills, lathes, lasers])
    db.flush()
    specs = [
        ("Haas Mini Mill 1", mills, False, 1), ("Haas Mini Mill 2", mills, False, 2),
        ("Haas VF-3", mills, True, 3), ("Haas ST-10", lathes, True, 4),
        ("Fibercell FC50", lasers, True, 5),
    ]
    machines = {}
    for name, grp, sup, order in specs:
        m = Machine(location_id=1, name=name, group_id=grp.id, requires_certification=True,
                    requires_supervision=sup, sort_order=order,
                    description="Demo machine - edit in Admin > Machines")
        db.add(m)
        machines[name] = m
    db.flush()

    staff = {
        "admin": staffauth.create_user(db, "admin", DEMO_PASSWORD, "admin", "Alex Admin", "admin@example.edu"),
        "sam": staffauth.create_user(db, "sam", DEMO_PASSWORD, "supervisor", "Sam Supervisor", "sam@example.edu"),
        "mo": staffauth.create_user(db, "mo", DEMO_PASSWORD, "monitor", "Mo Monitor", "mo@example.edu"),
    }
    # Supervisor on duty Mon-Fri 08:00-18:00 for the next 3 weeks.
    today = clock.to_local(clock.now()).date()
    for i in range(21):
        d = today + timedelta(days=i)
        if d.weekday() < 5:
            db.add(ShiftSchedule(location_id=1, staff_id=staff["sam"].id,
                                 start_at=clock.local_dt(d, time(8)), end_at=clock.local_dt(d, time(18))))

    students = {}
    for email, name, standing, course in [
        ("alex.rivera@csuchico.edu", "Alex Rivera", "junior", "ME 250"),
        ("jordan.lee@csuchico.edu", "Jordan Lee", "senior", "MECH 480"),
        ("sam.patel@csuchico.edu", "Sam Patel", "sophomore", "ENGR 110"),
        ("riley.chen@csuchico.edu", "Riley Chen", "graduate", "MSE 600"),
    ]:
        s = Student(email=email, full_name=name, standing=standing, course=course)
        db.add(s)
        students[email.split(".")[0]] = s
    db.flush()
    now = clock.now()
    for s in students.values():
        db.add(SafetyWaiver(student_id=s.id, version="1", signed_at=now))
    for key, targets in {"alex": [mills.id], "jordan": [mills.id, lathes.id], "sam": [], "riley": [lasers.id]}.items():
        for gid in targets:
            db.add(Certification(student_id=students[key].id, group_id=gid, granted_by="sam",
                                 expires_at=now + timedelta(days=365)))
    db.flush()

    def approved(student, title, desc, names, hours, mats):
        p = Project(student_id=student.id, title=title, description=desc, materials=mats,
                    estimated_hours=hours, status="approved", machine_ids=[machines[n].id for n in names],
                    reviewed_by="sam", reviewed_at=now)
        db.add(p)
        db.flush()
        db.add(ProjectRevision(project_id=p.id, revision_no=1, title=title, description=desc, materials=mats,
                               estimated_hours=hours, machine_ids=p.machine_ids))
        a = Approval(number=numbers.next_approval_number(db), project_id=p.id, student_id=student.id,
                     max_total_hours=hours, expires_at=now + timedelta(days=90), approved_by="sam",
                     comments="Looks good. Use flood coolant on aluminum.",
                     conditions="Wear safety glasses. Supervisor must sign off first part.")
        db.add(a)
        db.flush()
        for n in names:
            db.add(ApprovalMachine(approval_id=a.id, machine_id=machines[n].id))
        db.add(ApprovalRestriction(approval_id=a.id, kind="material", value="Aluminum 6061 and mild steel only"))
        db.flush()
        return a

    a1 = approved(students["alex"], "Robot arm base plate", "Mill a 6061 base plate for a capstone robot arm.",
                  ["Haas Mini Mill 1", "Haas Mini Mill 2", "Haas VF-3"], 12, "6061 aluminum")
    a2 = approved(students["jordan"], "Spindle shaft", "Turn a stepped shaft from 1045 steel.",
                  ["Haas ST-10"], 6, "1045 steel bar")
    db.add(Project(student_id=students["sam"].id, title="Bottle opener keychain",
                   description="Laser cut 20 stainless keychains.", materials="304 stainless 1mm",
                   estimated_hours=2, machine_ids=[machines["Fibercell FC50"].id], status="submitted"))
    db.flush()
    return {"approvals": [a1.number, a2.number]}


def reset_all(db) -> None:
    for table in reversed(Base.metadata.sorted_tables):
        if table.name == "audit_log":
            continue  # append-only; never wiped by app code
        db.execute(delete(table))
    db.flush()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset-demo", action="store_true", help="wipe everything and re-seed demo data")
    ap.add_argument("--force", action="store_true", help="allow --reset-demo when ENV=prod")
    args = ap.parse_args()
    cfg = get_config()
    if args.reset_demo and cfg.env == "prod" and not args.force:
        print("Refusing to wipe a production database. Pass --force if you really mean it.")
        return 2
    with dbm.session_scope() as db:
        if args.reset_demo:
            reset_all(db)
        elif db.scalar(select(Machine.id).limit(1)):
            print("Data already present; use --reset-demo to rebuild.")
            return 0
        info = seed_demo(db)
    print(f"Demo data ready. Staff logins: admin / sam (supervisor) / mo (monitor), password: {DEMO_PASSWORD}")
    print("Approval numbers:", ", ".join(info["approvals"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
