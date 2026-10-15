# Staff quick-start

**Admin site:** `https://<your-site>/admin` — log in with your own username (never share logins).
Roles: **monitor** = read-only · **supervisor** = approvals, reservations, machine status, overrides, incidents ·
**admin** = everything incl. settings, users, audit log, backups.

## Opening the shop
1. Put the TV on `/tv` (full screen, F11). It shows the 4-digit **shop code**, machine status and the four QR codes.
2. Check **Dashboard**: pending approvals, flagged/auto-closed sessions from yesterday (mark them reviewed), overdue banners.
3. Make sure a **supervisor shift** exists for today (Settings > Supervisor shifts) — machines that need supervision can't be booked otherwise.

## During the day
* **Approve a project:** Approvals > Review. Check files (links expire after 15 min — reopen the page for a fresh one), pick covered machines,
  set max hours/sessions, expiry, restrictions, then *Approve* (the student gets the approval number by email) or *Request modifications* / *Reject* (reason required).
* **Certify a student:** Students > the student > Grant certification (machine or whole group, optional expiry). Booking is blocked without it.
* **Student's phone died / code unreadable:** Sessions > *Force check-in* or *Force check-out* (reason required; audited).
* **Machine broke:** Machines > the machine > Log maintenance (sets Under Maintenance or Awaiting Inspection). When fixed: *Clear and return to service*.
  Need a quick manual state? Use *Manual status override* with a reason.
* **Forgotten check-outs:** sessions auto-close 30 minutes after the booking ends and appear under *Flagged sessions*. Review, then *Mark reviewed*.
* **Repeated no-shows:** 3 in 30 days auto-suspends booking for 14 days. Lift it on the student page (reason required) — earlier no-shows are forgiven.
* **Injury or near-miss:** Incidents > Record an incident (separate from damage reports).

## Emergency
Dashboard > **Emergency close** → type CLOSE. Every machine shows *Under Maintenance*, the TV goes full-screen red, active users get an email.
Reopen from the same dashboard (tick the confirmation). Both are audited.

## Closing
Check Sessions > Active is empty. Nightly backup runs automatically at 3:15 AM (Admin > Backups to check or run one).

## Printing the QR codes
Admin > **QR codes** > *Print-ready sheet (PDF)* (all four on one page) or *Poster PDF* per code. The URLs never change, so printed codes stay valid.
If you ever change `BASE_URL`, reprint.
