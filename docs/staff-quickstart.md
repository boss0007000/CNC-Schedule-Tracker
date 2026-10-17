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

## Approving student check-ins
When a student checks in they enter their reservation and the TV code, then wait. **Admin > Check-ins** (also linked from a red banner on the dashboard, and emailed to the supervisor on duty) shows who is waiting and what you are confirming: their approval covers the machine, they are on the access list for it, their certification is current, and the waiver is signed. Tap **Approve** (the machine goes to In Use) or **Deny**. Supervisors and admins checking in themselves skip this. Requests expire after 20 minutes and a student who is waiting is never counted as a no-show. Turn the rule off in Settings if you ever need to.

## Who may use which machine (access lists)
* **Admin/Supervisor > Access** holds two lists: **Approved** and **Super approved**. A student must be on a list to book anything. Paste several emails at once; students never see which list anyone is on.
* **Admin > Access > "Which machines can each group use?"** is a grid: tick the machines the **Approved** group may book and the machines the **Super approved** group may book. Untick both for a staff-run machine. (The simple "Who can book it" dropdown on Admin > Machines sets both columns at once; renaming a machine never resets the grid.)
* Staff-run example: Staff-run machines (for example the Fibercell FC50) cannot be booked by students, who submit parts for them on the project form; you make the part.
* Longest single session: 6 hours (approved) and 12 hours (super approved), editable in Settings. There are no weekly caps; the project approval is the gate.
* Only an admin can add to or remove from the super approved list.

## Setups and tools left in a machine
Students mark "only my tools" or "my setup" when they check out (or from the email link sent before closing). The machine then shows **DO NOT TOUCH** with the owner's number on the schedule page and TV. Limits: setup 3 open days, tools 1 open day (Settings). Owners get a friendly reminder each open day. When it expires the machine opens up and you get an email and a dashboard flag so you can put tools aside. Machines > the machine lets you place, extend or release a hold.

## Public schedule and the TV's week view
Students (and anyone with the link) can open `/schedule` without signing in to see who is booked when; the TV shows a "This week" availability panel under the machine cards. If you don't want phone numbers visible to people who aren't signed in, untick **Settings > Show phone numbers on the public schedule page**.

## Contact details on the schedule and TV
Giving a phone number is **optional**. Each student chooses what other students and the TV may see: their **phone number**, their **campus email**, or **nothing** (if they pick phone but add no number, their email is shown). Nothing is shown unless the student chose it. Staff can always see a student's details on their record. You can hide contacts from the TV (Settings > "Show contacts on the shop TV") or from the public schedule page (Settings > "Show contacts on the public schedule page"); you can also require a contact to reserve (Settings > "Require students to show a contact to reserve", off by default).

## Editing how long someone can be on a machine
* **Reservations > Edit time** (upcoming or current bookings) and **Sessions > Adjust end time** (someone on the machine now). Both show what that student's approval still allows: hours left, sessions, expiry and their longest single session. A reason is required and the student is emailed.
* Edits are refused if they would exceed the approval's remaining hours or sessions, run past its expiry, exceed the student's longest allowed session (6 h Approved / 12 h Super approved), overlap another booking, fall outside shop hours or hit a hold/maintenance. To go beyond an approval, edit the approval itself on the project page first.
* A student already on the machine keeps their start time; only the end changes.

## Students changing times with each other
Students can politely ask each other to move a slot (Access/Requests page shows them). Nobody can order anyone, a "no" is never shown to others, and there is no priority setting. If two students can't agree you decide, using Reservations.

## Emergency
Dashboard > **Emergency close** → type CLOSE. Every machine shows *Under Maintenance*, the TV goes full-screen red, active users get an email.
Reopen from the same dashboard (tick the confirmation). Both are audited.

## Closing
Check Sessions > Active is empty. Nightly backup runs automatically at 3:15 AM (Admin > Backups to check or run one).

## Printing the QR codes
Admin > **QR codes** > *Print-ready sheet (PDF)* (all four on one page) or *Poster PDF* per code. The URLs never change, so printed codes stay valid.
If you ever change `BASE_URL`, reprint.
