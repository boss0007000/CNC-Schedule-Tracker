# Security & privacy review (Milestone 9)

Checked by automated tests (`tests/test_m9_hardening.py`) unless noted.

| Area | Result |
|---|---|
| CSRF | Every POST route is enumerated by a test and rejects missing/forged tokens (double-submit cookie). |
| AuthN/AuthZ | Every `/admin` route enumerated: unauthenticated → login redirect. Role matrix tested (monitor read-only, supervisor ops, admin-only settings/users/audit/backups/sensitive CSV). |
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

**Known limits / recommendations**
* Rate limits key on client IP (`X-Forwarded-For` when `TRUSTED_PROXY=true`). Campus NAT can make many students share an IP; limits are set generously (120 POST/min/IP).
* No CAPTCHA by design (honeypot + limits). If abuse appears, tighten limits in `web.public_post_guard`.
* Virus scanning is a hook, not enabled by default.
* **FERPA / data policy:** this stores names, campus emails, optional student IDs and usage history. Confirm with the department and campus IT whether the
  free hosts and email provider are acceptable, the retention period, and who may see the data, before collecting real student data.
* Run one app worker (the in-process scheduler); the email outbox uses `SKIP LOCKED` so extra workers will not double-send.
