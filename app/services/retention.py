"""Data retention: purge old personal data (admin tool). The audit log is never purged."""
from datetime import timedelta

from sqlalchemy import delete, exists, func, select
from sqlalchemy.orm import Session

from app import clock
from app.models import (
    Approval, ApprovalMachine, ApprovalRestriction, Certification, ConsumableLog, DamageReport, EmailOutbox,
    EmailVerification, FileBlob, IdempotencyKey, IncidentReport, Project, ProjectFile, ProjectRevision, RateEvent,
    Reservation, SafetyWaiver, ShopSession, Student, WaitlistEntry,
)
from app.services import audit, files


def purge(db: Session, days: int, actor: str, dry_run: bool = True, include_incidents: bool = False) -> dict:
    """Delete personal data older than `days`. Returns counts per table (and applies them unless dry_run)."""
    cutoff = clock.now() - timedelta(days=max(days, 30))
    counts: dict[str, int] = {}

    def run(name: str, select_stmt, delete_fn):
        n = db.scalar(select(func.count()).select_from(select_stmt.subquery())) or 0
        counts[name] = n
        if n and not dry_run:
            delete_fn()

    kept_damage_sessions = select(DamageReport.session_id).where(DamageReport.session_id.is_not(None),
                                                                 DamageReport.created_at >= cutoff)
    done_sessions = select(ShopSession.id).where(ShopSession.status != "active", ShopSession.checked_in_at < cutoff,
                                                 ShopSession.id.not_in(kept_damage_sessions))
    run("consumable_log", select(ConsumableLog.id).where(ConsumableLog.session_id.in_(done_sessions)),
        lambda: db.execute(delete(ConsumableLog).where(ConsumableLog.session_id.in_(done_sessions))))
    old_damage = select(DamageReport).where(DamageReport.created_at < cutoff)

    def del_damage():
        for rep in db.scalars(old_damage):
            if rep.photo_key:
                files.delete(db, rep.photo_key)
        db.execute(delete(DamageReport).where(DamageReport.created_at < cutoff))
    run("damage_reports", select(DamageReport.id).where(DamageReport.created_at < cutoff), del_damage)
    # damage reports can reference sessions: they are removed above first
    run("sessions", done_sessions, lambda: db.execute(delete(ShopSession).where(ShopSession.id.in_(done_sessions))))
    run("waitlist_entries", select(WaitlistEntry.id).where(WaitlistEntry.end_at < cutoff, WaitlistEntry.status != "offered"),
        lambda: db.execute(delete(WaitlistEntry).where(WaitlistEntry.end_at < cutoff, WaitlistEntry.status != "offered")))
    still_referenced = select(ShopSession.reservation_id).where(ShopSession.reservation_id.is_not(None))
    old_res = (Reservation.end_at < cutoff, Reservation.status.in_(("completed", "cancelled", "no_show", "confirmed")),
               Reservation.id.not_in(still_referenced))
    run("reservations", select(Reservation.id).where(*old_res), lambda: db.execute(delete(Reservation).where(*old_res)))

    old_appr = (Approval.expires_at < cutoff,
                ~exists().where(Reservation.approval_id == Approval.id),
                ~exists().where(WaitlistEntry.approval_id == Approval.id))

    def del_approvals():
        ids = select(Approval.id).where(*old_appr)
        db.execute(delete(ApprovalMachine).where(ApprovalMachine.approval_id.in_(ids)))
        db.execute(delete(ApprovalRestriction).where(ApprovalRestriction.approval_id.in_(ids)))
        db.execute(delete(Approval).where(Approval.id.in_(ids)))
    run("approvals", select(Approval.id).where(*old_appr), del_approvals)

    old_proj = (Project.updated_at < cutoff, ~exists().where(Approval.project_id == Project.id))

    def del_projects():
        ids = select(Project.id).where(*old_proj)
        for f in db.scalars(select(ProjectFile).where(ProjectFile.project_id.in_(ids))):
            files.delete(db, f.storage_key)
        db.execute(delete(ProjectFile).where(ProjectFile.project_id.in_(ids)))
        db.execute(delete(ProjectRevision).where(ProjectRevision.project_id.in_(ids)))
        db.execute(delete(Project).where(Project.id.in_(ids)))
    run("projects", select(Project.id).where(*old_proj), del_projects)

    if include_incidents:
        run("incident_reports", select(IncidentReport.id).where(IncidentReport.occurred_at < cutoff),
            lambda: db.execute(delete(IncidentReport).where(IncidentReport.occurred_at < cutoff)))

    def inactive_students():
        refs = [Reservation.student_id, ShopSession.student_id, Project.student_id, Approval.student_id,
                WaitlistEntry.student_id, DamageReport.student_id, IncidentReport.student_id]
        conds = [~exists().where(c == Student.id) for c in refs]
        last_seen = func.coalesce(Student.last_verified_at, Student.created_at)
        return (last_seen < cutoff, *conds)
    if dry_run:
        # reflect what the real run would free up, assuming the deletes above happened
        counts["students"] = db.scalar(select(func.count()).select_from(Student).where(*inactive_students())) or 0
    else:
        ids = select(Student.id).where(*inactive_students())
        n = db.scalar(select(func.count()).select_from(ids.subquery())) or 0
        counts["students"] = n
        if n:
            db.execute(delete(Certification).where(Certification.student_id.in_(ids)))
            db.execute(delete(SafetyWaiver).where(SafetyWaiver.student_id.in_(ids)))
            db.execute(delete(EmailVerification).where(EmailVerification.email.in_(select(Student.email).where(Student.id.in_(ids)))))
            db.execute(delete(Student).where(Student.id.in_(ids)))

    small = clock.now() - timedelta(days=30)
    run("email_verifications", select(EmailVerification.id).where(EmailVerification.created_at < small),
        lambda: db.execute(delete(EmailVerification).where(EmailVerification.created_at < small)))
    run("email_outbox", select(EmailOutbox.id).where(EmailOutbox.created_at < small, EmailOutbox.status != "pending"),
        lambda: db.execute(delete(EmailOutbox).where(EmailOutbox.created_at < small, EmailOutbox.status != "pending")))
    run("rate_events", select(RateEvent.id).where(RateEvent.at < small),
        lambda: db.execute(delete(RateEvent).where(RateEvent.at < small)))
    run("idempotency_keys", select(IdempotencyKey.token).where(IdempotencyKey.created_at < small),
        lambda: db.execute(delete(IdempotencyKey).where(IdempotencyKey.created_at < small)))
    if not dry_run:
        db.flush()
        audit.record(db, actor, "retention.purge", "system", None, after={"days": days, "counts": counts})
    return counts
