"""Load test: ~50 concurrent virtual students + TV polling against a running server.

    # server running with the SAME SECRET_KEY / DATABASE_URL as this script
    python -m scripts.loadtest --base http://127.0.0.1:8000 --users 50 --prepare

Each virtual student: loads the reserve page, books a slot, views /my, checks the TV feed, repeatedly.
"""
import argparse
import asyncio
import random
import statistics
import time
from datetime import timedelta

import httpx

from app import clock, db as dbm, security
from app.models import AccessRoster, Approval, ApprovalMachine, Certification, Machine, MachineGroup, Project, SafetyWaiver, Student
from app.services import identity, numbers
from sqlalchemy import select


def prepare(n: int) -> list[str]:
    emails = []
    with dbm.session_scope() as s:
        mills = s.scalar(select(MachineGroup).where(MachineGroup.name == "CNC Mills"))
        machines = list(s.scalars(select(Machine).where(Machine.group_id == mills.id)))
        for i in range(n):
            email = f"load{i}@csuchico.edu"
            emails.append(email)
            if s.scalar(select(Student.id).where(Student.email == email)):
                continue
            st = Student(email=email, full_name=f"Load Tester{i}", standing="junior", phone=f"+1530555{i:04d}",
                         phone_ack_at=clock.now())
            s.add(st)
            s.flush()
            s.add(SafetyWaiver(student_id=st.id, version="1"))
            s.add(AccessRoster(email=email, level="approved", added_by="load"))
            s.add(Certification(student_id=st.id, group_id=mills.id, granted_by="load", expires_at=clock.now() + timedelta(days=300)))
            p = Project(student_id=st.id, title="load", description="load test project", status="approved",
                        machine_ids=[m.id for m in machines])
            s.add(p)
            s.flush()
            a = Approval(number=numbers.next_approval_number(s), project_id=p.id, student_id=st.id, max_total_hours=500,
                         expires_at=clock.now() + timedelta(days=200), approved_by="load")
            s.add(a)
            s.flush()
            for m in machines:
                s.add(ApprovalMachine(approval_id=a.id, machine_id=m.id))
    return emails


async def user(i: int, base: str, email: str, approval: str, machines: list[int], stop_at: float, stats: dict):
    cookie = identity.session_token(email)
    async with httpx.AsyncClient(base_url=base, cookies={identity.COOKIE: cookie}, timeout=20, follow_redirects=False) as c:
        async def hit(method, url, label, **kw):
            t = time.perf_counter()
            try:
                r = await c.request(method, url, **kw)
                code = r.status_code
            except Exception as e:  # noqa: BLE001
                code = f"EXC {type(e).__name__}"
            stats.setdefault(label, []).append((time.perf_counter() - t, code))
            return r if isinstance(code, int) else None

        await hit("GET", "/my", "warmup")
        await hit("GET", f"/reserve?approval={approval}", "warmup")  # sets the CSRF cookie
        tok = c.cookies.get("csrf")
        day = clock.now().date()
        while time.perf_counter() < stop_at:
            d = day + timedelta(days=random.randint(1, 12))
            mid = random.choice(machines)
            await hit("GET", f"/reserve?approval={approval}&machine={mid}&date={d}", "reserve page")
            hour = random.randint(8, 16)
            start = f"{d.isoformat()}T{hour + 8:02d}:00:00+00:00"  # roughly local hour in UTC-8
            await hit("POST", "/reserve", "reserve POST", data={
                "csrf_token": tok, "form_token": security.new_form_token(), "approval": approval,
                "machine_id": str(mid), "start": start, "nslots": "1", "action": "book", "accept_waiver": "on"})
            await hit("GET", "/my", "my page")
            for _ in range(3):
                await hit("GET", "/tv/data", "tv poll")
                await asyncio.sleep(random.uniform(0.2, 1.0))


async def main_async(a):
    emails = prepare(a.users) if a.prepare else [f"load{i}@csuchico.edu" for i in range(a.users)]
    with dbm.session_scope() as s:
        mills = s.scalar(select(MachineGroup).where(MachineGroup.name == "CNC Mills"))
        machines = list(s.scalars(select(Machine.id).where(Machine.group_id == mills.id)))
        approvals = {e: s.scalar(select(Approval.number).join(Student, Student.id == Approval.student_id).where(Student.email == e)) for e in emails}
    stats: dict = {}
    stop_at = time.perf_counter() + a.seconds
    t0 = time.perf_counter()
    await asyncio.gather(*[user(i, a.base, e, approvals[e], machines, stop_at, stats) for i, e in enumerate(emails)])
    wall = time.perf_counter() - t0
    print(f"\n{a.users} concurrent users, {wall:.0f}s, {sum(len(v) for v in stats.values())} requests "
          f"({sum(len(v) for v in stats.values()) / wall:.1f} req/s)")
    print(f"{'endpoint':16} {'n':>6} {'p50 ms':>8} {'p95 ms':>8} {'p99 ms':>8} {'max ms':>8}  status codes")
    bad = 0
    for label, rows in sorted(stats.items()):
        lat = sorted(r[0] * 1000 for r in rows)
        codes: dict = {}
        for _, c in rows:
            codes[c] = codes.get(c, 0) + 1
            if not isinstance(c, int) or c >= 500:
                bad += 1
        q = lambda p: lat[min(len(lat) - 1, int(len(lat) * p))]  # noqa: E731
        print(f"{label:16} {len(lat):6} {statistics.median(lat):8.0f} {q(.95):8.0f} {q(.99):8.0f} {lat[-1]:8.0f}  {codes}")
    print("server errors / exceptions:", bad)
    return 1 if bad else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--users", type=int, default=50)
    ap.add_argument("--seconds", type=int, default=30)
    ap.add_argument("--prepare", action="store_true")
    raise SystemExit(asyncio.run(main_async(ap.parse_args())))
