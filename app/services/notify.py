"""All outbound email content in one place. Every function only *queues* into the outbox."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock, emailer, security
from app.config import get_config
from app.models import Approval, Project, Reservation, StaffUser, Student
from app.services import settings as shop_settings


def _shop(db) -> str:
    return shop_settings.load(db).shop_name


def _fmt(dt) -> str:
    return clock.to_local(dt).strftime("%A %b %d, %I:%M %p").replace(" 0", " ")


def staff_recipients(db: Session) -> list[str]:
    cfg = get_config()
    if cfg.staff_notify_email:
        return [e.strip() for e in cfg.staff_notify_email.split(",") if e.strip()]
    rows = db.scalars(select(StaffUser).where(StaffUser.active.is_(True), StaffUser.email.is_not(None),
                                              StaffUser.role.in_(["admin", "supervisor"])))
    return [u.email for u in rows if u.email]


def to_staff(db: Session, subject: str, text: str) -> None:
    for to in staff_recipients(db):
        emailer.queue(db, to, subject, text)


def approval_text(a: Approval) -> str:
    lines = [
        f"APPROVAL RECORD  {a.number}",
        f"Student: {a.student.full_name} <{a.student.email}>",
        f"Project: {a.project.title}",
        f"Approved by: {a.approved_by} on {clock.to_local(a.approved_at).strftime('%b %d, %Y')}",
        f"Valid until: {clock.to_local(a.expires_at).strftime('%b %d, %Y') if a.expires_at else 'no expiry'}",
        "Covered machines: " + ", ".join(_machine_names(a)),
        f"Max total hours: {a.max_total_hours:g}" if a.max_total_hours else "Max total hours: unlimited",
        f"Max sessions: {a.max_sessions}" if a.max_sessions else "Max sessions: unlimited",
    ]
    if a.restrictions:
        lines.append("Restrictions:")
        lines += [f"  - {r.value}" for r in a.restrictions]
    if a.conditions:
        lines.append(f"Conditions: {a.conditions}")
    if a.comments:
        lines.append(f"Staff comments: {a.comments}")
    return "\n".join(lines)


def _machine_names(a: Approval) -> list[str]:
    from sqlalchemy.orm import object_session

    from app.models import Machine
    s = object_session(a)
    return [m.name for m in s.scalars(select(Machine).where(Machine.id.in_(a.machine_ids)).order_by(Machine.name))]


def approval_link(a: Approval) -> str:
    tok = security.sign("approval-view", {"n": a.number})
    return f"{get_config().base_url}/approval/{a.number}?t={tok}"


# --- projects ------------------------------------------------------------------------------
def project_submitted(db: Session, p: Project, revised: bool = False) -> None:
    emailer.queue(db, p.student.email, f"We received your project: {p.title}",
                  f"Hi {p.student.first_name},\n\nWe received {'your updated ' if revised else ''}project "
                  f"“{p.title}” and a supervisor will review it. You'll get an email when its status changes.\n\n"
                  f"— {_shop(db)}")
    to_staff(db, f"New {'resubmission' if revised else 'project submission'}: {p.title}",
             f"{p.student.full_name} ({p.student.email}) submitted “{p.title}”.\n"
             f"Review it: {get_config().base_url}/admin/approvals/{p.id}")


def project_approved(db: Session, a: Approval) -> None:
    emailer.queue(db, a.student.email, f"Project approved: {a.number}",
                  f"Hi {a.student.first_name},\n\nGreat news — “{a.project.title}” is approved.\n\n"
                  f"{approval_text(a)}\n\nYou will need this approval number to reserve machines at "
                  f"{get_config().base_url}/reserve\nPrintable record: {approval_link(a)}\n\n— {_shop(db)}")


def project_rejected(db: Session, p: Project, reason: str) -> None:
    emailer.queue(db, p.student.email, f"Project not approved: {p.title}",
                  f"Hi {p.student.first_name},\n\n“{p.title}” was not approved.\n\nReason: {reason}\n\n"
                  f"You are welcome to submit a new project at {get_config().base_url}/submit\n\n— {_shop(db)}")


def project_changes(db: Session, p: Project, comments: str) -> None:
    emailer.queue(db, p.student.email, f"Changes requested: {p.title}",
                  f"Hi {p.student.first_name},\n\nA supervisor asked for changes to “{p.title}”:\n\n{comments}\n\n"
                  f"Update and resubmit here: {get_config().base_url}/submit/{p.id}/revise\n\n— {_shop(db)}")


# --- reservations ------------------------------------------------------------------------------
def cancel_link(r: Reservation) -> str:
    tok = security.sign("res-cancel", {"ref": r.ref})
    return f"{get_config().base_url}/reservation/{r.ref}?t={tok}"


def reservation_confirmed(db: Session, r: Reservation, extended: bool = False) -> None:
    emailer.queue(db, r.student.email, f"Reservation {'updated' if extended else 'confirmed'}: {r.machine.name} {r.ref}",
                  f"Hi {r.student.first_name},\n\nYour reservation is {'extended' if extended else 'confirmed'}.\n\n"
                  f"Reference: {r.ref}\nMachine: {r.machine.name}\nStart: {_fmt(r.start_at)}\nEnd: {_fmt(r.end_at)}\n\n"
                  f"Keep the reference handy: you need it, plus the shop code on the TV, to check in "
                  f"({get_config().base_url}/checkin). Late by more than the grace period counts as a no-show.\n\n"
                  f"Need to cancel? {cancel_link(r)}\n\n— {_shop(db)}")


def reservation_cancelled(db: Session, r: Reservation, by_staff: bool = False, reason: str = "") -> None:
    emailer.queue(db, r.student.email, f"Reservation cancelled: {r.ref}",
                  f"Hi {r.student.first_name},\n\nYour reservation {r.ref} for {r.machine.name} on {_fmt(r.start_at)} "
                  f"was cancelled{' by shop staff' if by_staff else ''}.{(' Reason: ' + reason) if reason else ''}\n\n— {_shop(db)}")


def waitlist_offer(db: Session, student: Student, machine_name: str, start, end, token: str, minutes: int) -> None:
    emailer.queue(db, student.email, f"A slot opened up: {machine_name}",
                  f"Hi {student.first_name},\n\nThe {machine_name} slot you waited for ({_fmt(start)} – {_fmt(end)}) "
                  f"is now free. Claim it within {minutes} minutes or it goes to the next student:\n\n"
                  f"{get_config().base_url}/waitlist/claim/{token}\n\n— {_shop(db)}")


def no_show(db: Session, r: Reservation, suspended_until=None) -> None:
    extra = (f"\n\nBecause of repeated no-shows your booking privileges are paused until {_fmt(suspended_until)}. "
             f"Contact shop staff if you think this is a mistake.") if suspended_until else ""
    emailer.queue(db, r.student.email, f"No-show recorded: {r.ref}",
                  f"Hi {r.student.first_name},\n\nYou didn't check in for {r.machine.name} ({_fmt(r.start_at)}), so the slot "
                  f"was released for others.{extra}\n\n— {_shop(db)}")


def overdue_warning(db: Session, s) -> None:
    emailer.queue(db, s.student.email, f"Your {s.machine.name} time is up",
                  f"Hi {s.student.first_name},\n\nYour reservation for {s.machine.name} ended at {_fmt(s.expected_end_at)}. "
                  f"Please wrap up and check out at {get_config().base_url}/checkout. Sessions that are not closed will be "
                  f"closed automatically and flagged for staff.\n\nComing back later? Tell everyone what's staying in the machine "
                  f"(one tap): {session_link(s.id)}\n\n— {_shop(db)}")


def auto_closed(db: Session, s) -> None:
    emailer.queue(db, s.student.email, f"Your {s.machine.name} session was auto-closed",
                  f"Hi {s.student.first_name},\n\nYou didn't check out of {s.machine.name}, so we closed the session "
                  f"automatically and flagged it for staff review. Please remember to check out next time.\n\nIf you left tools or a "
                  f"setup in the machine, tell everyone (one tap): {session_link(s.id)}\n\n— {_shop(db)}")


def emergency(db: Session, to: str, message: str) -> None:
    emailer.queue(db, to, "EMERGENCY: shop closed", f"{message}\n\nStop all machines and follow staff instructions.")


# --- peer time-change requests ----------------------------------------------------------------
def change_requested(db: Session, req) -> None:
    r = req.target_reservation
    note = f"\nTheir note: “{req.note}”\n" if req.note else ""
    emailer.queue(db, req.target_student.email, f"{req.requester.public_name} asked about your {r.machine.name} time",
                  f"Hi {req.target_student.first_name},\n\n{req.requester.public_name} would like to use the {r.machine.name} "
                  f"during your booked time ({_fmt(r.start_at)} – {_fmt(r.end_at)}) and asked if you could move to "
                  f"{_fmt(req.proposed_start)} – {_fmt(req.proposed_end)} instead.\n{note}\n"
                  f"This is completely optional. Say yes or no with one tap — no reason needed, and a no is never shown to anyone:\n"
                  f"{get_config().base_url}/requests/{req.id}\n\nIf you can't agree, a shop supervisor can help.\n\n— {_shop(db)}")


def change_accepted(db: Session, req, token: str, claim_hours: float) -> None:
    r = req.target_reservation
    emailer.queue(db, req.requester.email, f"Good news: a {r.machine.name} time is yours to claim",
                  f"Hi {req.requester.first_name},\n\nThey agreed to move, so the {r.machine.name} time you asked about is free for you. "
                  f"Claim it here within {claim_hours:g} hours or it is released:\n"
                  f"{get_config().base_url}/waitlist/claim/{token}\n\nThank them if you see them!\n\n— {_shop(db)}")
    emailer.queue(db, req.target_student.email, f"Your {r.machine.name} time was moved",
                  f"Hi {req.target_student.first_name},\n\nThanks for being flexible. Your reservation {r.ref} is now "
                  f"{_fmt(r.start_at)} – {_fmt(r.end_at)} on the {r.machine.name}.\n\n— {_shop(db)}")


def change_declined(db: Session, req) -> None:
    emailer.queue(db, req.requester.email, "About your time-change request",
                  f"Hi {req.requester.first_name},\n\nThey weren't able to change their time this time. No hard feelings: you can "
                  f"join the waiting list for a slot, pick another time, or ask a shop supervisor for help.\n\n— {_shop(db)}")


def machine_free_early(db: Session, r: Reservation) -> None:
    emailer.queue(db, r.student.email, f"The {r.machine.name} is free right now",
                  f"Hi {r.student.first_name},\n\nThe {r.machine.name} just became free and nobody is booked before you. Your time is "
                  f"{_fmt(r.start_at)} – {_fmt(r.end_at)}, but you're welcome to start early if you're ready: check in at "
                  f"{get_config().base_url}/checkin (your end time stays {_fmt(r.end_at)}). No obligation.\n\n— {_shop(db)}")


# --- holds (setup / tools left in a machine) ---------------------------------------------------
def session_link(sid: int) -> str:
    return f"{get_config().base_url}/hold/session/{sid}?t=" + security.sign("hold-session", {"sid": sid})


def hold_link(hid: int) -> str:
    return f"{get_config().base_url}/hold/{hid}?t=" + security.sign("hold-manage", {"hid": hid})


def hold_affects_booking(db: Session, h, r: Reservation) -> None:
    emailer.queue(db, r.student.email, f"Heads up: someone's {'setup' if h.kind == 'setup' else 'tools'} are in the {r.machine.name}",
                  f"Hi {r.student.first_name},\n\n{h.student.public_name} has left {'a setup' if h.kind == 'setup' else 'their tools'} in the "
                  f"{r.machine.name} (please don't touch it). That may affect your booking {_fmt(r.start_at)} – {_fmt(r.end_at)}.\n"
                  f"You can see their number on the schedule page and ask them to clear it: {get_config().base_url}/machine/{r.machine_id}\n"
                  f"A shop supervisor can also help.\n\n— {_shop(db)}")


def hold_reminder(db: Session, h, day: int, total: int, final: bool) -> None:
    what = "setup" if h.kind == "setup" else "tools"
    emailer.queue(db, h.student.email, f"Your {what} is still in the {h.machine.name}" + (" (last day)" if final else ""),
                  f"Hi {h.student.first_name},\n\nYour {what} {'is' if what == 'setup' else 'are'} still in the {h.machine.name} "
                  f"(day {day} of {total}, held until {_fmt(h.expires_at)}). "
                  f"{'Today is the last day: please come back and clear it, or it will be released and staff will clear it.' if final else 'Coming back to work on it? Great. If you are finished, please clear it so others can use the machine.'}\n\n"
                  f"One tap to say it's cleared: {hold_link(h.id)}\n\n— {_shop(db)}")


def hold_expired(db: Session, h) -> None:
    emailer.queue(db, h.student.email, f"Your hold on the {h.machine.name} has ended",
                  f"Hi {h.student.first_name},\n\nThe time limit for keeping your {'setup' if h.kind == 'setup' else 'tools'} in the "
                  f"{h.machine.name} has passed, so it is open to others again. If anything of yours is still in the machine, "
                  f"please collect it soon; staff have been told and will put it aside.\n\n— {_shop(db)}")
    to_staff(db, f"Hold expired: {h.machine.name}", f"{h.student.full_name}'s {h.kind} hold on {h.machine.name} expired. "
                                                       f"Check whether tools or a setup still need clearing.")


def closing_prompt(db: Session, s) -> None:
    emailer.queue(db, s.student.email, f"Closing soon: anything staying in the {s.machine.name}?",
                  f"Hi {s.student.first_name},\n\nThe shop is closing soon. One tap to tell everyone what you're leaving in the "
                  f"{s.machine.name}:\n  • Cleared out (nothing staying)\n  • Only my tools are in it: do not touch\n  • A setup is in it: do not touch\n\n"
                  f"{session_link(s.id)}\n\nThis lets others know not to disturb your work. Holds last a limited number of open days.\n\n— {_shop(db)}")


def checkin_waiting(db: Session, s) -> None:
    """Tell the supervisor on duty (or all supervisors if nobody is scheduled) that someone is waiting."""
    from app.services import shop
    sup = shop.supervisor_on_duty(db, clock.now())
    to = [sup.email] if sup and sup.email else staff_recipients(db)
    for addr in to:
        emailer.queue(db, addr, f"Check-in waiting: {s.student.public_name} on {s.machine.name}",
                      f"{s.student.full_name} is at the shop and checked in to the {s.machine.name}. Please confirm they are "
                      f"authorized (one tap): {get_config().base_url}/admin/checkins")
