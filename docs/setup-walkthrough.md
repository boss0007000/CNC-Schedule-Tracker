# Complete setup walkthrough (internet-hosted, free)

This takes you from "a folder on my computer" to "students can use it from their phones anywhere." Allow 1.5–2 hours the first time.
Websites change their screens now and then, so a button may be named slightly differently; the intent of each step stays the same.
Free-plan limits change too, so check each provider's current terms when you sign up.

**What you will end up with**

| Piece | Service | Cost | Job |
|---|---|---|---|
| Code | GitHub | free | stores the project so Render can build it |
| Web app | Render | free | runs the website 24/7 |
| Database | Neon | free | stores all data (Postgres) |
| Email | Brevo | free | sends login codes, approvals, confirmations |
| Keep-awake | UptimeRobot | free | stops the free web app sleeping during shop hours |

**Things to have ready before you start:** a computer with internet; an email address you can receive mail at; a shop/department email
address to send *from* (for example `machineshop@csuchico.edu`) that you can receive mail at; about 30 minutes of attention for Part 3 because email deliverability needs care;
and 4 notes for passwords (use a password manager).

---

## PART 0 — Decide a few things (10 minutes)

Write these down; you will type them in later.

1. **Admin username** for you, e.g. `boss`. **Admin password**: at least 14 characters, not used anywhere else.
2. **Sender email** (the "From" address students see), e.g. `machineshop@csuchico.edu`. You must be able to open its inbox, because Brevo emails a verification link to it.
3. **Staff alert email(s)**: where new project submissions are announced. One or more addresses separated by commas.
4. **Student email domain**: `csuchico.edu` unless you say otherwise.
5. **Shop name** shown on pages and the TV, e.g. "Advanced Manufacturing Lab".

---

## PART 1 — Put the project on GitHub

Render builds your site from GitHub, so the code must live there first.

### 1.1 Install Git and create a GitHub account
1. Go to **https://git-scm.com/downloads**, download Git for your computer, and install it with all the default options (on Windows keep "Git from the command line").
2. Go to **https://github.com/signup** and create a free account. Verify your email when GitHub asks.
3. Easiest way to avoid password/token trouble: install **GitHub Desktop** from https://desktop.github.com and sign in with your GitHub account. Steps below show both GitHub Desktop and the command line; use either.

### 1.2 Create an empty private repository
1. On github.com click the **+** at the top right → **New repository**.
2. Repository name: `machine-shop`.
3. Choose **Private** (the code doesn't contain secrets, but there is no reason to publish it).
4. Do **not** tick "Add a README", ".gitignore" or "license" (the project already has files).
5. Click **Create repository**. Leave the page open; it shows the repository address like `https://github.com/YOURNAME/machine-shop.git`.

### 1.3 Upload the project

**With GitHub Desktop (recommended)**
1. File → **Add local repository…** → choose the project folder `CNC Shop time slot booking`.
2. It says "this directory does not appear to be a Git repository" → click **create a repository**. Keep the name; leave "Git ignore" as **None** (the project already has a `.gitignore`); click **Create repository**.
3. In the left list you should see many files. Check that **no** file named `.env`, and no `.venv` or `var` folder, is listed (they are ignored on purpose).
4. Type a summary such as `Initial version` and click **Commit to main**.
5. Click **Publish repository**, untick "Keep this code private" **only if** you want it public (leave it ticked), and click **Publish**.

**With the command line** (Windows PowerShell, or Mac/Linux Terminal), inside the project folder:
```bash
git init
git add .
git commit -m "Initial version"
git branch -M main
git remote add origin https://github.com/YOURNAME/machine-shop.git
git push -u origin main
```
If Git asks who you are: `git config --global user.name "Your Name"` and `git config --global user.email "you@example.edu"`, then repeat the commit.
When pushing, GitHub opens a browser window to sign you in (Git Credential Manager). If it asks for a password in the terminal, use a **personal access token** (GitHub → Settings → Developer settings → Personal access tokens), not your password.

### 1.4 Check it worked
Refresh the repository page on github.com. You should see folders `app`, `alembic`, `docs`, `scripts`, `tests` and files `Dockerfile`, `render.yaml`, `requirements.lock`, `README.md`.
If `Dockerfile` or `render.yaml` is missing, go back to 1.3; Render needs both.

---

## PART 2 — Create the database (Neon)

1. Go to **https://neon.tech** → **Sign up** (use GitHub or Google sign-in, or email).
2. Create a project: name `machine-shop`; **Postgres version**: the default is fine; **Region**: choose the one closest to your users (for California choose a US West region such as "AWS US West 2 (Oregon)"). **Choose the same region group you will pick for Render** (Part 4) when possible, because it makes the site faster.
3. Click **Create project**.
4. On the project dashboard find the **Connect** panel (button "Connect" or a "Connection string" box).
   - Select your branch `main`, database `neondb`, role as shown.
   - **Turn OFF "Connection pooling"** if there is a toggle (we want the direct connection).
   - The string looks like:
     `postgresql://neondb_owner:AbCd1234@ep-cool-name-123456.us-west-2.aws.neon.tech/neondb?sslmode=require`
5. Click **Copy**. **Paste it into a safe note labelled DATABASE_URL.** Check that it ends with `?sslmode=require`. If it does not, type `?sslmode=require` at the very end.
   Treat this string like a password: anyone with it can read all student data.
6. Nothing else to do: you do **not** create tables yourself. The app builds them the first time it starts.

Notes: Neon's free database "sleeps" after a few minutes without use and wakes in about a second on the next request; the app copes with this automatically.

---

## PART 3 — Set up email (Brevo)

Email is essential: every student login needs a 6-digit code sent by email.

### 3.1 Create the account
1. Go to **https://www.brevo.com** → **Sign up free**. Use your work email. Confirm the email Brevo sends.
2. Complete the short profile questions. Choose the **Free** plan (check its daily sending limit, currently about 300 emails per day; a busy shop day uses roughly 2 emails per student action, so estimate your volume).

### 3.2 Add and verify your sender address
1. In Brevo click your name (top right) → **Senders, domains & dedicated IPs** → **Senders** → **Add a sender**.
2. **From name**: `Machine Shop` (or your shop's name). **From email**: the sender address from Part 0, e.g. `machineshop@csuchico.edu`.
3. Brevo emails a verification code or link to that address. Open that inbox, copy the code, and enter it in Brevo. The sender should now show as **Verified**.

### 3.3 Make email actually arrive (important!)
Brevo can send mail "from" a campus address only if the campus domain allows it. Otherwise student mail providers may put your emails in **spam** or reject them.
1. Ask your campus IT / department web admin: "Please add Brevo's SPF and DKIM DNS records for the domain I send from, or authorise Brevo to send for `machineshop@csuchico.edu`."
2. In Brevo go to **Senders, domains & dedicated IPs → Domains → Add a domain**, enter the domain, and Brevo shows the DNS records (a TXT record for DKIM, a TXT for the Brevo code, and an SPF/DMARC suggestion). Send those exact records to IT. Click **Authenticate** once IT confirms they are added.
3. If IT cannot do this, use a sender address Brevo can verify but expect some delivery problems, and tell students to check spam on first use. Test early (Part 9).

### 3.4 Create the API key
1. Click your name → **SMTP & API** → **API Keys** tab → **Generate a new API key**.
2. Name it `machine-shop`. Click **Generate**.
3. **Copy the key now** (Brevo shows it once). Paste it into a safe note labelled **BREVO_API_KEY**. It starts with `xkeysib-`.
4. If Brevo asks you to activate your SMTP/transactional account, follow its prompts (sometimes it needs a short manual approval).

---

## PART 4 — Deploy the website (Render)

### 4.1 Account
1. Go to **https://render.com** → **Get started** → sign up with **GitHub** (easiest because it links your repositories). Render might ask for a card to verify identity; free web services are not charged, but read the screen carefully and do not choose a paid plan.

### 4.2 Create the service from the Blueprint
1. In the Render dashboard click **New +** → **Blueprint**.
2. If asked, click **Connect GitHub**/**Configure account**, and allow Render access to the `machine-shop` repository only (the "Only select repositories" option).
3. Choose the repository `machine-shop` → **Connect**.
4. Render reads `render.yaml` and shows a service named **machine-shop** (plan: Free) and asks you to fill in environment variables. Fill in each box exactly:

| Variable | What to type |
|---|---|
| `DATABASE_URL` | the Neon string from Part 2 (ending `?sslmode=require`) |
| `BASE_URL` | `https://machine-shop.onrender.com` for now (you will correct it in 4.4 if Render gave a different address) |
| `BREVO_API_KEY` | the key from Part 3 |
| `EMAIL_FROM` | your verified sender address, e.g. `machineshop@csuchico.edu` |
| `STAFF_NOTIFY_EMAIL` | the alert address(es) from Part 0 (comma-separated) |
| `BOOTSTRAP_ADMIN_USERNAME` | e.g. `boss` |
| `BOOTSTRAP_ADMIN_PASSWORD` | your 14+ character admin password |
| `BACKUP_PUT_URL` | leave blank (see Part 10) |

   Values already filled in by the Blueprint: `ENV=prod`, `STORAGE_BACKEND=db` (uploads are stored in the database because the free server's disk is wiped), `EMAIL_PROVIDER=brevo`, `FORCE_HTTPS=true`, and a randomly generated `SECRET_KEY`. Leave those alone.
5. Click **Apply** / **Deploy Blueprint**.

### 4.3 Watch the first build (5–10 minutes)
1. Click the service **machine-shop** → **Logs**.
2. You will see it building the Docker image, then lines such as `Running upgrade … initial schema` and `Application startup complete`. When the top of the page says **Live**, it worked.
3. If it says **Deploy failed**, read the last red lines of the log:
   - `Unsafe production configuration: …` → one of the variables is wrong (the message names it: `BASE_URL must be https://`, `SECRET_KEY …`, `EMAIL_PROVIDER …`). Fix it in **Environment** (4.4).
   - `could not connect to server` / `password authentication failed` → `DATABASE_URL` is mistyped. Copy it again from Neon.
   - `ssl` errors → the string is missing `?sslmode=require`.

### 4.4 Set the correct public address (BASE_URL)
1. At the top of the service page Render shows your address, such as `https://machine-shop-x7k2.onrender.com`. **Copy it exactly.**
2. Click **Environment** in the left menu → find `BASE_URL` → **Edit** → paste the real address (no trailing slash) → **Save changes**. Render redeploys automatically (a few minutes).
3. Why this matters: emails contain links built from `BASE_URL`, and **the QR codes you print contain it**. Get it right before printing anything.

### 4.5 Check it is alive
Open these in your browser (replace with your address):
- `https://YOUR-ADDRESS/healthz` → shows `{"status":"ok"}`.
- `https://YOUR-ADDRESS/tv` → shows the board (empty at first: no machines yet).
- `https://YOUR-ADDRESS/admin` → the staff login page.
The first load after a quiet period can take 30–60 seconds on the free plan (the server is waking up).

### 4.6 (Optional) Use your own web address
Pretty addresses like `machineshop.csuchico.edu` need IT. In Render: service → **Settings → Custom Domains → Add**, then give IT the DNS record Render shows (usually a CNAME). After it verifies, set `BASE_URL` to the new address and **reprint QR codes**.

---

## PART 5 — First login and shop configuration (30–45 minutes)

### 5.1 Log in and secure your account
1. Go to `https://YOUR-ADDRESS/admin`. Log in with `BOOTSTRAP_ADMIN_USERNAME` / `BOOTSTRAP_ADMIN_PASSWORD`.
2. Top bar → **Settings** → **Staff users** (or go to `/admin/users`).
3. In the row for your own account, type a **new password** (at least 10 characters) in "New password" → **Reset password**. You'll be logged out; log in again with the new password.
4. **Remove the bootstrap variables:** in Render → Environment → delete `BOOTSTRAP_ADMIN_USERNAME` and `BOOTSTRAP_ADMIN_PASSWORD` → Save. (They only matter on a completely empty database, and keeping a password in Render is unnecessary.)

### 5.2 Create staff accounts (everyone gets their own login)
On `/admin/users` → **Add staff user**:
- **Username** (lowercase, e.g. `jsmith`), **Display name**, **Email** (needed so they get new-submission alerts), **Role**, **Password** (10+ characters; they should change it after first login).
- **Roles:** `admin` = everything (settings, users, audit log, backups, machines); `supervisor` = approvals, reservations, machine status, overrides, incidents, certifications; `monitor` = read-only.
- Create supervisors and student employees accordingly. Never share one login.

### 5.3 Rename the shop and set the rules
Top bar → **Settings** (`/admin/settings`). Work from top to bottom:

1. **Weekly hours** → set each weekday's open/close times (24-hour format like `08:00`/`18:00`), tick **Closed** for days off → **Save hours**. (New databases start Mon–Fri 08:00–22:00.)
2. **Holidays and special hours** → add each closed date or short day (date + label, tick "Closed all day" or give hours) → **Add**.
3. **Access lists** (do this before students can book!): top bar → **Access** → paste your students' campus emails into the *Approved* list (an admin can also fill *Super approved*). Then Machines → set each machine's **Who can book it** (standard / super approved only / staff-run).
3b. **Supervisor shifts** → this is **essential**: machines marked "requires supervision" cannot be booked unless a shift covers the time. Choose the supervisor, the start and end of the first shift, and **Repeat weekly for** `26` to create half a year of identical weekly shifts → **Add shift**. Repeat for each person/pattern. You can remove or add single shifts later.
4. **Rules and limits** (click each field; defaults are sensible):
   - *Shop name* — shown on the site and TV.
   - *Allowed student email domain* — `csuchico.edu`.
   - *Collect student ID* — leave **off** unless the department needs it (privacy).
   - *Slot length* 60 min · *Max slots per reservation* 4 · *Max reservations/week* 3 · *Max hours/week* 8 · *Max advance days* 14 · *Max active per machine group* 1.
   - *Cancellation cutoff* 120 min · *Check-in early* 15 · *Late grace* 15 · *Auto-close buffer* 30 · *No-show threshold* 3 in *window* 30 days → *suspension* 14 days.
   - *Shop code rotation* 10 min (previous code valid 2 more minutes) · *Waitlist claim window* 30 min.
   - *Waiver text* — replace with your department's official safety waiver wording. If you change it later, raise the *Waiver version* number so every student signs the new one.
   - *Retention days* 730 — agree this with your department.
   Click **Save settings**. Any wrong entry shows a red message and nothing is saved.

### 5.4 Add your real machines
1. Top bar → **Machines** (`/admin/machines`). A new database has no machines.
2. First create **groups** (right side, "Groups"): e.g. `CNC Mills`, `CNC Lathes`, `Laser Cutters`, `Saws`, `3D Printers`. A group is how training and the "one active booking per group" rule work.
3. **Add machine** for each: name (e.g. `Haas VF-3`), group, short description; tick **Requires training/certification** (nearly always yes); tick **Requires supervisor on duty** where staff must be present.
4. Each machine appears on the TV and on the reservation page immediately.

### 5.5 Printing QR codes and posting them
1. Top bar → **QR codes**. Confirm the small text under each code shows **your real https address**, not `localhost`.
2. Click **Print-ready sheet (PDF)** → print on regular paper → laminate. Or use **Poster PDF** for a full page per code.
3. Post them by the door and near the TV: Submit, Reserve, Check In, Check Out. Test each by scanning with a phone.

---

## PART 6 — Set up the TV

1. Plug a computer or streaming stick (anything with a modern browser: Chrome, Edge) into the TV.
2. **Rotate the screen to portrait:** Windows → Settings → System → Display → *Display orientation* → **Portrait**. (Many TVs also rotate via their own menu; use whichever gives the picture upright.)
3. Open the browser and go to `https://YOUR-ADDRESS/tv`. Press **F11** for full screen (or start Chrome with `--kiosk https://YOUR-ADDRESS/tv`, which also hides the cursor).
4. **Stop the screen sleeping:** Windows → Settings → System → Power → set "Screen" and "Sleep" to **Never**. Turn off automatic Windows updates restart hours if possible.
5. It refreshes itself every 5 seconds. If the internet drops you will see a yellow banner "Connection lost, last updated …" and it recovers by itself.
6. Make the browser/computer start at login so a power cut doesn't leave it blank: add a shortcut to the kiosk command in the Windows Startup folder (press `Win+R`, type `shell:startup`).

---

## PART 7 — Keep it awake during shop hours (UptimeRobot)

Without this, the free Render site sleeps after ~15 minutes idle and the first student waits up to a minute.
1. Go to **https://uptimerobot.com** → sign up free.
2. **+ Add New Monitor** → type **HTTP(s)** → friendly name `Machine Shop` → URL `https://YOUR-ADDRESS/healthz` → interval **5 minutes** (free plan minimum) → **Create Monitor**.
3. (Optional) Add your email under alert contacts so you are told if the site goes down.
4. Render's free plan gives roughly 750 running hours a month; one always-on service fits (check current terms). The app is still *correct* if it sleeps: status is computed from timestamps; this only affects speed.

---

## PART 8 — Student and staff onboarding (before opening)

**A student's first visit**
1. Scans the **Submit** QR (or opens the site) → **Verify your campus email**: full name, `@csuchico.edu` email, standing, optional course → "Email me a code".
2. Types the 6-digit code from the email (valid 10 minutes, 5 tries) → stays signed in 8 hours on that device.
3. **Submit a project** (title, description, machines, materials, estimated hours, up to 5 files of 10 MB: PDF, images, DXF, STEP, STL).
4. Waits for approval email containing an **approval number** like `APR-2026-0042`.
5. **Reserve**: enters the approval number, machine, date → picks a slot → accepts the safety waiver (first time) → gets reference `RSV-7K3Q9F` by email.
6. **Check in**: within 15 minutes before the slot until 15 minutes after it starts → enters reference + the 4-digit code on the TV.
7. **Check out**: ticks the 3 cleanup items, enters the TV code, reports any problem.

**What staff must do for a student to be able to book**
1. Student has verified their email at least once (this creates their record).
2. A supervisor **approves** their project (Approvals queue → Review → choose machines, hours, expiry → Approve).
3. A supervisor **certifies** them: Students → click the student → *Grant certification* (a machine or a whole group, optional expiry). **Without this the system blocks the booking by design.**
4. A supervisor is on **shift** for the booking time if the machine requires one.

---

## PART 9 — Test everything before announcing it (30 minutes)

Do this with a real phone on **mobile data (not your Wi-Fi)** to prove "from anywhere" works.
1. Open `https://YOUR-ADDRESS/healthz` → ok.
2. `/verify` with your own campus email → the code email arrives within a minute (also check spam). If not: Part 11.
3. Submit a test project with a small PDF → you receive "We received your project"; a staff member receives a "New project submission" email.
4. Approve it in `/admin` → you receive the approval email with the number.
5. Grant yourself a certification (Students → your record).
6. Reserve a slot for the next hour → confirmation email arrives; the reference works.
7. Check in with the code on the TV; confirm the TV card shows your name as "First L." with a timeline bar.
8. Check out; confirm the card returns to Available.
9. Dashboard → **Emergency close**: type `CLOSE` → the TV turns red; then **Reopen**.
10. Admin → **Backups → Run a backup now** → download the file.
11. Delete your test data if you wish: use the audit log to confirm everything was recorded, and remove test projects by letting retention purge them later, or keep them as history.

---

## PART 10 — Backups (do not skip)

The free Render server's disk is wiped when it restarts or sleeps, so a backup file saved there can vanish. Choose at least one:

**Option 1 — manual weekly download (simplest).** Every Friday: `/admin/backups` → **Run a backup now** → click the new file to download → store it somewhere safe (department drive).
**Option 2 — automatic to a place you control.** If the department has a WebDAV or file-upload endpoint (Nextcloud, Box WebDAV, university file server with HTTP PUT), set in Render → Environment: `BACKUP_PUT_URL` (the folder address) and `BACKUP_PUT_AUTH` (`username:password`). The nightly 3:15 AM backup then uploads automatically (while the server is awake).
**Option 3 — nightly database dump with GitHub Actions** (works even when the web app sleeps; the dump contains student data, so use a **private** repository and short retention). Create the file `.github/workflows/backup.yml` in the repository:
```yaml
name: nightly-db-backup
on:
  schedule: [{cron: "30 10 * * *"}]   # 10:30 UTC daily
  workflow_dispatch:
jobs:
  dump:
    runs-on: ubuntu-latest
    steps:
      - run: sudo apt-get install -y postgresql-client
      - run: pg_dump "$DATABASE_URL" | gzip > dump.sql.gz
        env: {DATABASE_URL: "${{ secrets.DATABASE_URL }}"}
      - uses: actions/upload-artifact@v4
        with: {name: db-dump, path: dump.sql.gz, retention-days: 14}
```
Then in GitHub → repository → Settings → Secrets and variables → Actions → **New repository secret** named `DATABASE_URL` with the Neon string.
**Restoring:** create an empty Neon database, run `alembic upgrade head` against it, then `python -m scripts.restore_backup backup-XXXX.json.gz` (for the app's own backup files).
Also look at what history/restore your Neon plan includes.

---

## PART 11 — Troubleshooting

| Symptom | Fix |
|---|---|
| Site shows Render's error page / very slow first load | The free server was asleep. Wait a minute and refresh. Set up UptimeRobot (Part 7). |
| Deploy failed, log says `Unsafe production configuration` | The message names the bad variable (BASE_URL not https, secret too short, wrong EMAIL_PROVIDER). Fix in Render → Environment. |
| Deploy failed, database error | Re-copy `DATABASE_URL` from Neon; must end `?sslmode=require`. |
| Verification email never arrives | Check Brevo → **Transactional → Logs** to see whether it was sent/blocked; make sure the sender is **Verified** (3.2); check spam; verify the API key was pasted fully (`xkeysib-…`); check you are under the daily limit; do domain authentication (3.3). Render → Logs shows `email … failed` lines with the reason. |
| "Your form expired. Go back, reload…" | The page was open a very long time or cookies are blocked. Reload the page and retry. |
| Student can't book: "need a current certification" | Grant it (Students → student → Grant certification). |
| Student can't book: "supervisor … none is scheduled" | Add supervisor shifts (5.3 step 3). |
| Student says "shop code is wrong" | They must read the code from the TV *now* (it changes every 10 minutes; the previous code works 2 extra minutes). 5 wrong tries per reservation lock it for 10 minutes; staff can force check-in. |
| TV shows "Connection lost" | TV computer's internet is down, or the site is waking. It recovers automatically. |
| Locked out of admin | Another admin resets your password at `/admin/users`. Five wrong passwords lock an account for 15 minutes, so first just wait. If you are the only admin and forgot the password, it can only be reset directly in the database (Neon dashboard → SQL editor) by someone who knows how, so **create a second admin account right after setup** and store both passwords safely. |
| Forgot to put the real address in BASE_URL before printing QR codes | Fix `BASE_URL` (4.4) and reprint. |

---

## PART 12 — Running it day to day, and updating it

- **Daily routine:** see `docs/staff-quickstart.md`. Outage plan and printable paper sheet: `docs/outage-procedure.md`, `docs/paper-signin-sheet.pdf`.
- **Updating the software:** change files → commit → push to GitHub → Render redeploys automatically and migrates the database. Watch **Logs** for `Live`.
- **Changing a setting:** shop rules are in Admin → Settings (no redeploy). Server settings (email key, address) are in Render → Environment (saving redeploys).
- **Data and privacy:** confirm FERPA, retention and hosting with your department/campus IT *before* real students use it. Admin → Settings → *Data retention* previews and purges old records.
- **Costs:** all five services have free plans; confirm limits periodically (email per day, database size, server hours).
