# Security & privacy review (Milestone 9)

Checked by automated tests (`tests/test_m9_hardening.py`) unless noted.

| Area | Result |
|---|---|
| CSRF | Every POST route is enumerated by a test and rejects missing/forged tokens (double-submit cookie). |
| AuthN/AuthZ | Every `/admin` route enumerated: unauthenticated → login redirect. Suspensions are time-limited and only reachable via three justified, expiring, voidable strikes; no-shows re-verify check-in and exclude shop-caused misses. Safety holds can only be lifted by their author or by Maintenance/Lead supervisor/Admin (`machines.safety_clear`); trainers certify only on machines they are authorized for (`trainer_authorizations`). Per-machine QR check-in: unguessable per-machine token, student must be verified and hold a confirmed reservation for that exact machine; the QR only pre-selects the machine; the rotating TV code is still required for presence, so a saved QR image cannot check anyone in remotely. Supervisor shifts require a clock-in with the rotating shop code (same presence proof as student check-in; the code is on the public TV page, so it proves presence only as well as the TV is kept in the shop). Temporary staff passwords are random, emailed once, scrubbed from the outbox after sending, and force a change before any portal page works. Permission-based roles (monitor, floor supervisor, part reviewer, trainer, scheduler, maintenance, lead supervisor, admin; multiple per person). Tested: monitor gets 403 on every admin write route, specialised roles can't reach users/settings, last-admin protection, default-deny for any new write route. |
| Passwords / lockout | argon2id; ≥10 chars; 5 failures → 15 min lock; password change invalidates sessions; login also rate-limited per IP. |
| Student identity | Code is 6 digits, hashed (HMAC) at rest, 10 min, 5 attempts, per-email + per-IP limits, single use; signed 8 h cookie (HttpOnly, SameSite=Lax, Secure in prod). |
| IDOR | Reservation / approval / project / waitlist / done pages require ownership or a signed token; others get 404. Wrong-owner and unknown references return identical messages. |
| Presence | Rotating shop code; failed guesses limited per IP and per reference. |
| Injection / XSS | SQLAlchemy parameterised queries only; Jinja autoescape; tested with `<script>` payloads and SQL metacharacters; CSV export neutralises formula injection. |
| CSP & headers | `script-src 'self'`, `style-src 'self'` (no inline), `frame-ancestors 'none'`, nosniff, HSTS, no-store on all pages. Templates are tested for inline styles/handlers. |
| Uploads | Extension + MIME allow-list, magic-byte checks, executable/HTML sniffing, 5×10 MB, sanitised names, random storage keys, stored outside web root (or in DB), served only to staff via signed 15-min links with `sandbox` CSP. Virus-scan hook: `services/files.scan_file` (ClamAV via `CLAMD_HOST`). |
| Public data | TV feed exposes first name + last initial only; no emails, IDs, or file URLs (tested). |
| Config | `ENV=prod` refuses weak `SECRET_KEY`, non-https `BASE_URL`, SQLite, console email. |
| Audit | Append-only (ORM guard + Postgres trigger); records staff logins, approvals, overrides, settings diffs, exports, purges. |
| Privacy | Student ID optional and off by default; retention purge with preview; no third-party scripts/fonts/analytics. |

**Contact details (a deliberate decision)** Giving a phone number is optional. Each student chooses what is shown on the schedule page and the shop TV: their phone, their campus email, or nothing (nothing is shown until they choose; choosing phone without a number shows their email). That departs from the original "no student PII beyond first name and last initial on the TV" rule, so it is opt-in per student. Other safeguards: the choice and consent timestamp are stored, numbers and emails are never written to the audit log, the TV and public page can each be switched off in Settings, and details are deleted with a student by the retention purge. Confirm this with the department/IT (FERPA) before use.

**Public schedule page (a deliberate decision)** `/schedule` and `/machine/<id>` need no login, so first name + last initial and (by default) phone numbers of students with a booking are visible to anyone who has the link. Mitigations: noindex meta tag and `robots.txt`, lookups limited to 300/hour per network address, no emails/approval numbers/access levels ever shown, and a Settings switch ("Show phone numbers on the public schedule page") to hide numbers from non-signed-in visitors while signed-in students still see them. Consider turning it off if the link is shared widely.

**Known limits / recommendations**
* Rate limits key on client IP (`X-Forwarded-For` when `TRUSTED_PROXY=true`). Campus NAT can make many students share an IP; limits are set generously (120 POST/min/IP).
* No CAPTCHA by design (honeypot + limits). If abuse appears, tighten limits in `web.public_post_guard`.
* Virus scanning is a hook, not enabled by default.
* **FERPA / data policy:** this stores names, campus emails, optional student IDs and usage history. Confirm with the department and campus IT whether the
  free hosts and email provider are acceptable, the retention period, and who may see the data, before collecting real student data.
* Run one app worker (the in-process scheduler); the email outbox uses `SKIP LOCKED` so extra workers will not double-send.
