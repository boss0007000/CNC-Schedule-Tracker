"""Email provider abstraction + DB outbox. Messages are queued in the same transaction as the
state change that caused them, then delivered after commit or by the background job."""
import logging
import smtplib
from email.message import EmailMessage

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.config import get_config
from app.models import EmailOutbox

log = logging.getLogger("emailer")
SENT: list[dict] = []  # populated by the 'memory' provider (tests)


class EmailError(Exception):
    pass


def _send_console(to, subject, text, html, reply_to=None):
    log.info("EMAIL to=%s subject=%s\n%s", to, subject, text)
    print(f"\n--- EMAIL to={to} subject={subject}\n{text}\n---", flush=True)


def _send_memory(to, subject, text, html, reply_to=None):
    SENT.append({"to": to, "subject": subject, "text": text, "html": html, "reply_to": reply_to})


def _send_brevo(to, subject, text, html, reply_to=None):
    c = get_config()
    r = httpx.post("https://api.brevo.com/v3/smtp/email", timeout=15,
                   headers={"api-key": c.brevo_api_key, "accept": "application/json"},
                   json={"sender": {"name": c.email_from_name, "email": c.email_from},
                         "to": [{"email": to}], "subject": subject,
                         "textContent": text, **({"htmlContent": html} if html else {}),
                         **({"replyTo": {"email": reply_to}} if reply_to else {})})
    if r.status_code >= 300:
        raise EmailError(f"brevo {r.status_code}: {r.text[:200]}")


def _send_resend(to, subject, text, html, reply_to=None):
    c = get_config()
    r = httpx.post("https://api.resend.com/emails", timeout=15,
                   headers={"Authorization": f"Bearer {c.resend_api_key}"},
                   json={"from": f"{c.email_from_name} <{c.email_from}>", "to": [to],
                         "subject": subject, "text": text, **({"html": html} if html else {}),
                         **({"reply_to": reply_to} if reply_to else {})})
    if r.status_code >= 300:
        raise EmailError(f"resend {r.status_code}: {r.text[:200]}")


def _send_smtp(to, subject, text, html, reply_to=None):
    c = get_config()
    msg = EmailMessage()
    msg["From"] = f"{c.email_from_name} <{c.email_from}>"
    msg["To"] = to
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")
    with smtplib.SMTP(c.smtp_host, c.smtp_port, timeout=15) as s:
        s.starttls()
        if c.smtp_user:
            s.login(c.smtp_user, c.smtp_password)
        s.send_message(msg)


PROVIDERS = {"console": _send_console, "memory": _send_memory, "brevo": _send_brevo,
             "resend": _send_resend, "smtp": _send_smtp}


def queue(db: Session, to: str, subject: str, text: str, html: str | None = None, reply_to: str | None = None) -> EmailOutbox:
    row = EmailOutbox(to_email=to, subject=subject, body_text=text, body_html=html, reply_to=reply_to)
    db.add(row)
    db.flush()
    return row


SECRET_SUBJECT_PREFIX = "Your staff login"


def flush_outbox(db: Session, limit: int = 50) -> int:
    """Deliver pending messages. Failures are retried up to 5 times, never raised."""
    provider = PROVIDERS.get(get_config().email_provider, _send_console)
    sent = 0
    # SKIP LOCKED: several workers/processes may flush concurrently without double-sending (Postgres)
    rows = db.scalars(select(EmailOutbox).where(EmailOutbox.status == "pending")
                      .order_by(EmailOutbox.id).limit(limit).with_for_update(skip_locked=True)).all()
    for row in rows:
        row.attempts += 1
        try:
            provider(row.to_email, row.subject, row.body_text, row.body_html, row.reply_to)
            row.status, row.sent_at, row.last_error = "sent", clock.now(), None
            if row.subject.startswith(SECRET_SUBJECT_PREFIX):  # don't keep temporary passwords in the database
                row.body_text, row.body_html = "(removed after sending)", None
            sent += 1
        except Exception as e:  # noqa: BLE001
            row.last_error = str(e)[:500]
            if row.attempts >= 5:
                row.status = "failed"
            log.warning("email to %s failed: %s", row.to_email, e)
    db.flush()
    return sent


def flush_in_new_session() -> None:
    """For FastAPI BackgroundTasks, run after the response is sent."""
    from app.db import session_scope
    with session_scope() as s:
        flush_outbox(s)
