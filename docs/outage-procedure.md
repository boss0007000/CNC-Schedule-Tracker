# Outage / paper fallback procedure

The system lives in the cloud, so it keeps working if the **shop PC is off** — students still book, check in and check out from their phones.
Use paper only if the **internet/Wi-Fi is down** or the **site is down** (check `/healthz`, or try from a phone on cellular).

1. **Announce** at the door: "Sign in on paper today." Print `docs/paper-signin-sheet.pdf` (keep ~10 copies in the drawer).
2. **Sign-in:** every visitor writes name, campus email, approval number, machine, time in. A supervisor **verifies the student is certified
   and has an approved project** (look them up later if you can't now; if unsure, don't release the machine).
3. **Sign-out:** time out + the three cleanup checks + any damage. Tick off machines on a whiteboard so you know what's in use.
4. **Physical code:** the TV code isn't needed on paper — staff presence replaces it.
5. **Recovery:** when the site is back, enter the day in Admin: Reservations > *Create a reservation* (tick "ignore rules", reason "paper outage"), then
   *Force check-in*/*Force check-out* on Sessions. Keep the paper sheets for the retention period.
6. **If only the TV is down:** staff can read the current shop code from Admin (dashboard/TV page `/tv` on any device) and tell students at the door.
7. **Emergency during an outage:** follow the building procedure first; record it later under Incidents.
