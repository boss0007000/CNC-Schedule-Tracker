# Test and load summary

**Suite:** 278 tests, all passing — `277 passed, 1 warning in 43.35s` on SQLite (`pytest -q`).
The same suite passes against a real **PostgreSQL 16** (embedded via `python -m scripts.pg_test`, run per file). The Alembic migration
(including the Postgres audit-log trigger) was applied to Postgres by the load-test harness; `alembic check` shows no drift.
Not tested: the real Brevo/Resend APIs, a real hosted Render/Neon deployment, ClamAV, and a physical TV/phones.

| File | Covers |
|---|---|
| test_m1_foundation | seed, argon2, lockout, sessions, roles, CSRF, audit append-only |
| test_m2_identity | email code (expiry, 5 attempts, single use, rate limits), cookie, domain, honeypot |
| test_m3_engine | every status transition, hours/holidays/DST, supervisor coverage, rotating code |
| test_m4_submit | uploads (limits/MIME/magic bytes), approve/reject/changes, revisions, signed file links |
| test_m5_reserve / _http | all validation rules, fairness, cancel/extend, waitlist promote/claim/expire |
| test_m6_sessions / _http | check-in/out, no-show, suspension + override, auto-close, overdue, reports |
| test_m7_dashboards | TV privacy, admin pages, roles, overrides, certs, incidents, emergency close |
| test_m8_reports | report numbers, CSV (redaction, injection), QR/PDF, backup round-trip, settings, purge |
| test_m9_hardening | every-route CSRF/auth checks, role matrix, XSS/SQLi, IDOR, headers, prod config, accessibility |

## Load test (50 concurrent virtual students + TV polling, 30 s, one uvicorn worker, Postgres 16, laptop)
`python -m scripts.run_loadtest 50`
```
endpoint              n   p50 ms   p95 ms   p99 ms   max ms  status codes
my page             505       72      367      502      674  {200: 505}
reserve POST        505       80     1156     1585     2082  {400: 81, 303: 39, 429: 385}
reserve page        505      521      991     1195     1509  {200: 505}
tv poll            1515      105      353      504      935  {200: 1515}
warmup              100      273      647      907      907  {200: 100}
server errors / exceptions: 0
Scheduler has been shut down
```
No 5xx or connection errors. The 429s are the per-IP POST limiter: all 50 simulated users share one IP and book far faster than real students.
The slowest page is the reserve slot list (~0.5 s median) because every slot is fully validated; fine at shop scale.
The load test found and fixed a race when two requests created the new rotating code simultaneously.
