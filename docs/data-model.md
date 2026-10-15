# Data model

All timestamps are stored in UTC and displayed in `America/Los_Angeles` (configurable). `location_id` exists on
machines, shop hours, shifts and staff so a second shop can be added later (one location by default).

```mermaid
erDiagram
  LOCATIONS ||--o{ MACHINES : has
  LOCATIONS ||--o{ SHOP_HOURS : has
  LOCATIONS ||--o{ SHIFT_SCHEDULE : has
  LOCATIONS ||--o{ STAFF_USERS : scopes
  MACHINE_GROUPS ||--o{ MACHINES : groups
  STUDENTS ||--o{ PROJECTS : submits
  PROJECTS ||--o{ PROJECT_REVISIONS : "revision history"
  PROJECTS ||--o{ PROJECT_FILES : attaches
  PROJECTS ||--o{ APPROVALS : "approved as"
  STUDENTS ||--o{ APPROVALS : holds
  APPROVALS ||--o{ APPROVAL_MACHINES : covers
  MACHINES ||--o{ APPROVAL_MACHINES : "covered by"
  APPROVALS ||--o{ APPROVAL_RESTRICTIONS : limits
  STUDENTS ||--o{ RESERVATIONS : books
  MACHINES ||--o{ RESERVATIONS : "booked on"
  APPROVALS ||--o{ RESERVATIONS : "spent against"
  RESERVATIONS ||--o| SESSIONS : "checked in as"
  STUDENTS ||--o{ SESSIONS : uses
  MACHINES ||--o{ SESSIONS : "used in"
  STUDENTS ||--o{ WAITLIST_ENTRIES : waits
  MACHINES ||--o{ WAITLIST_ENTRIES : "waited for"
  STUDENTS ||--o{ CERTIFICATIONS : "certified (machine OR group)"
  MACHINE_GROUPS ||--o{ CERTIFICATIONS : covers
  STUDENTS ||--o{ SAFETY_WAIVERS : signs
  MACHINES ||--o{ MAINTENANCE_RECORDS : logs
  MACHINES ||--o{ DAMAGE_REPORTS : "reported on"
  SESSIONS ||--o{ DAMAGE_REPORTS : "reported during"
  SESSIONS ||--o{ CONSUMABLE_LOG : consumes
  STAFF_USERS ||--o{ SHIFT_SCHEDULE : "on duty"
  STUDENTS ||--o{ INCIDENT_REPORTS : involved
  MACHINES ||--o{ INCIDENT_REPORTS : involved
```

Standalone tables: `email_verifications` (hashed 6-digit codes), `rotating_codes` (shop presence code),
`settings` (key → JSON), `audit_log` (append-only; enforced by an ORM guard and, on Postgres, a trigger),
and infrastructure tables `rate_events`, `idempotency_keys`, `email_outbox`, `file_blobs`.

## Key rules encoded in the schema
* `students.email` unique and lower-cased: the identity key (SSO later only has to produce a verified email).
* `approvals.number` unique (`APR-2026-0042`); `reservations.ref` unique (`RSV-7K3Q9F`, no ambiguous characters).
* `sessions`: partial unique indexes allow at most one *active* session per machine and per student (double-scan safe).
* `certifications`: exactly one of `machine_id` / `group_id`.
* `reservations.end_at > start_at`; overlap prevention is done under a row lock on the machine.
* `shop_hours`: either a weekly row (`weekday`) or a dated exception (`on_date`), never both.
