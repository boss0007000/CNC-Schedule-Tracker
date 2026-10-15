# Student Machine Shop Management System

Replaces paper sign-ins and word-of-mouth scheduling: project approvals, machine reservations, QR check-in/out,
and a live TV board. Students use phones (no accounts, no apps); staff use `/admin`; a TV shows `/tv`.

Python 3.12 · FastAPI · SQLAlchemy 2 · Alembic · Postgres (SQLite for local dev) · Jinja2 + a little vanilla JS · APScheduler.
Zero-cost stack: Render (free web), Neon (free Postgres), Brevo (free email) — all optional/replaceable.

## Quick start (local)
```bash
./scripts/dev.sh            # macOS/Linux: venv, migrate, seed demo data, run on http://localhost:8000
```
```powershell
.\scripts\dev.ps1          # Windows PowerShell (or scripts\dev.bat in cmd.exe). Needs Python 3.12 from python.org ("py" launcher).
```
If PowerShell blocks the script: `powershell -ExecutionPolicy Bypass -File scripts\dev.ps1`.
Manual (any OS): `python -m venv .venv`, activate it, `pip install -r requirements-dev.txt`, `alembic upgrade head`, `python -m scripts.seed`, `uvicorn app.main:app --reload`.
Demo logins (password `demo-password-123`): `admin`, `sam` (supervisor), `mo` (monitor). Verification codes are printed to the console in dev.
Open `/tv`, `/admin`, `/submit`. Reset demo data any time: `python -m scripts.seed --reset-demo`.
Seeded machines: 2× Haas Mini Mill, Haas VF-3, Haas ST-10, Fibercell FC50 (edit in Admin > Machines).

Tests: `python -m pytest -q` inside the venv (SQLite). Same suite on real Postgres: `python -m scripts.pg_test` (needs `uv pip install pgserver`).

## How it works
* **Server is the source of truth.** Rules live in `app/services/*`; pages are thin.
* **Status is derived, never stored** (`services/status.py`): Available / Reserved / In Use / Overdue / Under Maintenance / Awaiting Inspection come from reservations, sessions
  and flags on every request, so a sleeping host is never wrong. APScheduler (every minute) and a throttled lazy sweep only *persist* things
  (no-shows, auto-close, overdue/waitlist emails) and flush the email outbox.
* **Identity** is behind `services/identity.IdentityProvider`; today it is the emailed 6-digit code → signed 8 h cookie. SSO would only need to return a verified email.
* **Presence:** check-in/out need the 4-digit shop code on the TV (rotates every 10 min, previous valid 2 more minutes). Staff can force check-in/out (audited).
* **Times:** stored UTC, shown in `America/Los_Angeles`, DST-safe slot grid (tested across both DST changes).

## Environment variables
See `.env.example` (all settings are env vars). Essentials: `ENV`, `DATABASE_URL`, `SECRET_KEY`, `BASE_URL`, `EMAIL_PROVIDER` (+ key), `STORAGE_BACKEND`.
Shop rules (hours, limits, grace periods, domain, retention, code interval, waiver text…) are edited in **Admin > Settings**.
`ENV=prod` refuses to start with a weak `SECRET_KEY`, non-https `BASE_URL`, SQLite or console email.

## Deploy for free (Render + Neon + Brevo)
**Every click, start to finish: [docs/setup-walkthrough.md](docs/setup-walkthrough.md).** Summary:
1. **Neon:** create a free project → copy the connection string (append `?sslmode=require`).
2. **Brevo:** create a free account → verify a sender address (ideally the department's, with SPF/DKIM) → create an API key (SMTP & API). Free tiers change; check current daily limits.
3. **Render:** push this repo to GitHub → New > Blueprint → select the repo (uses `render.yaml`/`Dockerfile`). Fill in: `DATABASE_URL`, `BASE_URL` (the https URL),
   `BREVO_API_KEY`, `EMAIL_FROM`, `STAFF_NOTIFY_EMAIL`, `BOOTSTRAP_ADMIN_USERNAME/PASSWORD` (first admin; change the password after login). `SECRET_KEY` is generated. Migrations run on every start.
4. Open `/admin`, log in, then **Settings**: weekly hours, holidays, **supervisor shifts** (required for supervised machines), email domain, limits. Add real machines and groups; delete the demo ones.
5. **Keep it awake in shop hours (optional):** free Render services sleep after ~15 min idle (first request then takes a while). Point a free uptime pinger (UptimeRobot / cron-job.org) at `/healthz` every 10 minutes during opening hours. Correctness doesn't depend on this.
6. **Backups:** Render's free disk is ephemeral, so set `BACKUP_PUT_URL` (+`BACKUP_PUT_AUTH`) to any WebDAV/HTTP-PUT folder (e.g. Nextcloud/Box WebDAV, or a department share),
   or download from **Admin > Backups** weekly. For Google Drive, run a free scheduled job (e.g. GitHub Actions + `rclone` + `pg_dump`) pointed at Neon. Restore: migrate an empty DB, then `python -m scripts.restore_backup <file>`. Also check what point-in-time recovery your Neon plan includes.
7. Print QR codes (below), post them, put the TV on `/tv`.
Other hosts work the same: it is a plain Docker image honouring `$PORT`; run **one** process.

## Printing the QR codes
Admin > **QR codes**: *Print-ready sheet (PDF)* has all four on one page (labels + instructions); per-code PNG and poster PDFs are also there.
URLs are fixed (`/submit`, `/reserve`, `/checkin`, `/checkout`), so codes never need reprinting unless `BASE_URL` changes.

## Running the shop day-to-day
See [docs/staff-quickstart.md](docs/staff-quickstart.md). Outage / paper fallback: [docs/outage-procedure.md](docs/outage-procedure.md) with [docs/paper-signin-sheet.pdf](docs/paper-signin-sheet.pdf).
Data model: [docs/data-model.md](docs/data-model.md). Security review: [docs/security-review.md](docs/security-review.md). Test and load results: [docs/test-summary.md](docs/test-summary.md).

## Assumptions (no clarifying questions were possible, so these are decisions)
* Slot length 60 min (configurable); a reservation is 1–4 consecutive slots. Machines are booked per machine; "group" = machine group (lathes, mills…).
* Certification = per machine **or** per group, with optional expiry; revocable. Hard block, no override except staff "ignore rules" manual bookings (audited).
* Approval "exhausted" counts confirmed + checked-in + completed reservations (cancelled/no-show don't count).
* "Max one active reservation per machine group" means one current/upcoming booking per group per student at a time (as specified); raise it in Settings.
* Weekly limits use Monday–Sunday in local time.
* Cancel cutoff default 2 h; check-in opens 15 min early; late grace 15 min (counted from the later of slot start and booking time, so waitlist claims of an already-started slot stay usable); auto-close 30 min after end; 3 no-shows/30 days → 14-day suspension.
* Waitlist: up to 3 per student; claim window 30 min; an offered slot is held for that student; unclaimed offers pass to the next person.
* Email goes through an outbox (retry up to 5×). Student-visible TV names are "First L."; the supervisor name is shown the same way.
* Staff passwords: ≥10 characters, argon2; sessions 8 h. No self-service password reset (an admin resets it).
* Virus scanning is a documented hook (`CLAMD_HOST`), off by default. File viewer links expire after 15 minutes.
* Student identity fields are collected on the verify page (once per 8 h), not repeated on every form.
* Not built (designed for): SSO, ID-card scanning, hardware access control, tool inventory, multi-location UI (`location_id` columns exist).
* **Data policy:** confirm FERPA/retention/hosting with the department and campus IT before using real student data.
