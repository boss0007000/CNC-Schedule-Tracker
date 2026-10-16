"""Database models. All timestamps are stored as UTC and returned tz-aware."""
from __future__ import annotations

from datetime import date, datetime, time, timezone

from sqlalchemy import (
    JSON, Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer,
    LargeBinary, String, Text, Time, UniqueConstraint, event, false, text, true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


class UTCDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("naive datetime not allowed; use tz-aware UTC")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {datetime: UTCDateTime}


def _now() -> datetime:
    from app import clock
    return clock.now()


# --- status vocabularies -----------------------------------------------------
STANDINGS = ["freshman", "sophomore", "junior", "senior", "graduate"]
ROLES = ["monitor", "supervisor", "admin"]  # ascending privilege

PROJECT_SUBMITTED = "submitted"
PROJECT_CHANGES = "modifications_requested"
PROJECT_APPROVED = "approved"
PROJECT_REJECTED = "rejected"

RES_CONFIRMED = "confirmed"
RES_CHECKED_IN = "checked_in"
RES_COMPLETED = "completed"
RES_CANCELLED = "cancelled"
RES_NO_SHOW = "no_show"
RES_ACTIVE = (RES_CONFIRMED, RES_CHECKED_IN)
RES_COUNTED = (RES_CONFIRMED, RES_CHECKED_IN, RES_COMPLETED)

MS_AVAILABLE = "available"
MS_RESERVED = "reserved"
MS_IN_USE = "in_use"
MS_OVERDUE = "overdue"
MS_MAINTENANCE = "under_maintenance"
MS_INSPECTION = "awaiting_inspection"
MS_HOLD = "on_hold"  # someone's setup / tools are in the machine: do not touch
MACHINE_STATUSES = [MS_AVAILABLE, MS_RESERVED, MS_IN_USE, MS_OVERDUE, MS_MAINTENANCE, MS_INSPECTION, MS_HOLD]


class Location(Base):
    __tablename__ = "locations"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)


class Student(Base):
    __tablename__ = "students"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)  # lower-cased
    full_name: Mapped[str] = mapped_column(String(120))
    standing: Mapped[str] = mapped_column(String(20))
    course: Mapped[str | None] = mapped_column(String(120))
    student_id: Mapped[str | None] = mapped_column(String(30))
    suspended_until: Mapped[datetime | None]
    suspension_reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(default=_now)
    last_verified_at: Mapped[datetime | None]
    noshow_forgiven_at: Mapped[datetime | None]  # staff override: earlier no-shows no longer count
    phone: Mapped[str | None] = mapped_column(String(20))  # +E.164-ish; required to reserve, shown on schedule + TV
    phone_ack_at: Mapped[datetime | None]  # when they acknowledged their number is shown to other students/the TV
    allow_requests: Mapped[bool] = mapped_column(default=True, server_default=true())  # time-change requests
    __table_args__ = (CheckConstraint("standing in ('freshman','sophomore','junior','senior','graduate')",
                                      name="ck_student_standing"),)

    @property
    def first_name(self) -> str:
        return self.full_name.split()[0] if self.full_name.split() else ""

    @property
    def public_name(self) -> str:
        parts = self.full_name.split()
        if not parts:
            return "Student"
        return parts[0] if len(parts) == 1 else f"{parts[0]} {parts[-1][0]}."


class EmailVerification(Base):
    __tablename__ = "email_verifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), index=True)
    code_hash: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(default=_now)
    expires_at: Mapped[datetime]
    attempts: Mapped[int] = mapped_column(default=0)
    consumed_at: Mapped[datetime | None]
    ip: Mapped[str | None] = mapped_column(String(64))
    pending_profile: Mapped[dict | None] = mapped_column(JSON)


class StaffUser(Base):
    __tablename__ = "staff_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(60), unique=True)
    display_name: Mapped[str] = mapped_column(String(120), default="")
    email: Mapped[str | None] = mapped_column(String(255))
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default="monitor")
    location_id: Mapped[int | None] = mapped_column(ForeignKey("locations.id"))
    active: Mapped[bool] = mapped_column(default=True)
    failed_logins: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[datetime | None]
    session_epoch: Mapped[int] = mapped_column(default=0)  # bump to invalidate sessions
    created_at: Mapped[datetime] = mapped_column(default=_now)
    last_login_at: Mapped[datetime | None]
    __table_args__ = (CheckConstraint("role in ('admin','supervisor','monitor')", name="ck_staff_role"),)


class MachineGroup(Base):
    __tablename__ = "machine_groups"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str | None] = mapped_column(String(255))


class Machine(Base):
    __tablename__ = "machines"
    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), default=1, index=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("machine_groups.id"))
    description: Mapped[str | None] = mapped_column(String(255))
    requires_certification: Mapped[bool] = mapped_column(default=True)
    requires_supervision: Mapped[bool] = mapped_column(default=False)
    # standard = any listed student; advanced = students with extended access; staff_only = students may only
    # submit parts for it (staff run it)
    access_level: Mapped[str] = mapped_column(String(20), default="standard", server_default="standard")
    active: Mapped[bool] = mapped_column(default=True)  # inactive = hidden / retired
    # 'none' | 'awaiting_inspection' | 'under_maintenance'
    maintenance_state: Mapped[str] = mapped_column(String(30), default="none")
    override_status: Mapped[str | None] = mapped_column(String(30))
    override_reason: Mapped[str | None] = mapped_column(String(255))
    override_by: Mapped[str | None] = mapped_column(String(60))
    sort_order: Mapped[int] = mapped_column(default=0)
    group: Mapped[MachineGroup | None] = relationship()


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    materials: Mapped[str] = mapped_column(String(500), default="")
    estimated_hours: Mapped[float] = mapped_column(default=1.0)
    course: Mapped[str | None] = mapped_column(String(120))
    instructor: Mapped[str | None] = mapped_column(String(120))
    machine_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(30), default=PROJECT_SUBMITTED, index=True)
    revision_no: Mapped[int] = mapped_column(default=1)
    staff_comments: Mapped[str | None] = mapped_column(Text)
    reviewed_by: Mapped[str | None] = mapped_column(String(60))
    reviewed_at: Mapped[datetime | None]
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now)
    student: Mapped[Student] = relationship()
    files: Mapped[list["ProjectFile"]] = relationship(back_populates="project", order_by="ProjectFile.id")
    revisions: Mapped[list["ProjectRevision"]] = relationship(back_populates="project",
                                                              order_by="ProjectRevision.revision_no")


class ProjectRevision(Base):
    __tablename__ = "project_revisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    revision_no: Mapped[int]
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text)
    materials: Mapped[str] = mapped_column(String(500), default="")
    estimated_hours: Mapped[float] = mapped_column(default=1.0)
    machine_ids: Mapped[list] = mapped_column(JSON, default=list)
    staff_comments: Mapped[str | None] = mapped_column(Text)  # feedback that led to the next revision
    created_at: Mapped[datetime] = mapped_column(default=_now)
    project: Mapped[Project] = relationship(back_populates="revisions")
    __table_args__ = (UniqueConstraint("project_id", "revision_no"),)


class ProjectFile(Base):
    __tablename__ = "project_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    revision_no: Mapped[int] = mapped_column(default=1)
    original_name: Mapped[str] = mapped_column(String(255))
    storage_key: Mapped[str] = mapped_column(String(255), unique=True)
    mime: Mapped[str] = mapped_column(String(120))
    size: Mapped[int]
    sha256: Mapped[str] = mapped_column(String(64))
    scan_status: Mapped[str] = mapped_column(String(20), default="skipped")
    created_at: Mapped[datetime] = mapped_column(default=_now)
    project: Mapped[Project] = relationship(back_populates="files")


class FileBlob(Base):
    """Used when STORAGE_BACKEND=db (hosts with ephemeral disks)."""
    __tablename__ = "file_blobs"
    storage_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    data: Mapped[bytes] = mapped_column(LargeBinary)


class Approval(Base):
    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(20), unique=True, index=True)  # APR-2026-0042
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active | revoked
    comments: Mapped[str | None] = mapped_column(Text)
    conditions: Mapped[str | None] = mapped_column(Text)
    max_total_hours: Mapped[float | None]
    max_sessions: Mapped[int | None]
    expires_at: Mapped[datetime | None]
    approved_by: Mapped[str] = mapped_column(String(60))
    approved_at: Mapped[datetime] = mapped_column(default=_now)
    project: Mapped[Project] = relationship()
    student: Mapped[Student] = relationship()
    machines: Mapped[list["ApprovalMachine"]] = relationship(cascade="all, delete-orphan")
    restrictions: Mapped[list["ApprovalRestriction"]] = relationship(cascade="all, delete-orphan")

    @property
    def machine_ids(self) -> list[int]:
        return [m.machine_id for m in self.machines]


class ApprovalMachine(Base):
    __tablename__ = "approval_machines"
    approval_id: Mapped[int] = mapped_column(ForeignKey("approvals.id", ondelete="CASCADE"), primary_key=True)
    machine_id: Mapped[int] = mapped_column(ForeignKey("machines.id"), primary_key=True)


class ApprovalRestriction(Base):
    __tablename__ = "approval_restrictions"
    id: Mapped[int] = mapped_column(primary_key=True)
    approval_id: Mapped[int] = mapped_column(ForeignKey("approvals.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))  # e.g. material_limit
    value: Mapped[str] = mapped_column(String(255))


class Reservation(Base):
    __tablename__ = "reservations"
    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(16), unique=True, index=True)  # RSV-7K3Q9F
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    approval_id: Mapped[int | None] = mapped_column(ForeignKey("approvals.id"), index=True)
    machine_id: Mapped[int] = mapped_column(ForeignKey("machines.id"), index=True)
    start_at: Mapped[datetime]
    end_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(20), default=RES_CONFIRMED)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    cancelled_at: Mapped[datetime | None]
    cancel_reason: Mapped[str | None] = mapped_column(String(255))
    manual: Mapped[bool] = mapped_column(default=False)  # created by staff
    student: Mapped[Student] = relationship()
    machine: Mapped[Machine] = relationship()
    approval: Mapped[Approval | None] = relationship()
    __table_args__ = (
        CheckConstraint("end_at > start_at", name="ck_res_window"),
        Index("ix_res_machine_window", "machine_id", "start_at", "end_at"),
    )

    @property
    def grace_anchor(self) -> datetime:
        """Late-arrival grace counts from the later of slot start and booking time
        (so a slot claimed from the waitlist after it began is still usable)."""
        return max(self.start_at, self.created_at)

    @property
    def hours(self) -> float:
        return (self.end_at - self.start_at).total_seconds() / 3600


class WaitlistEntry(Base):
    __tablename__ = "waitlist_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    approval_id: Mapped[int] = mapped_column(ForeignKey("approvals.id"))
    machine_id: Mapped[int] = mapped_column(ForeignKey("machines.id"), index=True)
    start_at: Mapped[datetime]
    end_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(20), default="waiting")  # waiting|offered|claimed|expired|cancelled
    created_at: Mapped[datetime] = mapped_column(default=_now)
    offered_at: Mapped[datetime | None]
    claim_expires_at: Mapped[datetime | None]
    claim_token: Mapped[str | None] = mapped_column(String(64), unique=True)
    student: Mapped[Student] = relationship()
    machine: Mapped[Machine] = relationship()


class ShopSession(Base):
    """Actual check-in/out (table is named 'sessions' per the spec)."""
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    reservation_id: Mapped[int | None] = mapped_column(ForeignKey("reservations.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    machine_id: Mapped[int] = mapped_column(ForeignKey("machines.id"), index=True)
    checked_in_at: Mapped[datetime]
    checked_out_at: Mapped[datetime | None]
    expected_end_at: Mapped[datetime]
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|closed|auto_closed|force_closed
    auto_closed: Mapped[bool] = mapped_column(default=False)
    needs_review: Mapped[bool] = mapped_column(default=False)
    forced_by: Mapped[str | None] = mapped_column(String(60))
    checklist: Mapped[dict | None] = mapped_column(JSON)
    overdue_notified_at: Mapped[datetime | None]
    student: Mapped[Student] = relationship()
    machine: Mapped[Machine] = relationship()
    reservation: Mapped[Reservation | None] = relationship()
    __table_args__ = (
        # At most one active session per machine and per student (race-proof, Postgres + SQLite).
        Index("uq_active_session_machine", "machine_id", unique=True,
              postgresql_where=text("status = 'active'"), sqlite_where=text("status = 'active'")),
        Index("uq_active_session_student", "student_id", unique=True,
              postgresql_where=text("status = 'active'"), sqlite_where=text("status = 'active'")),
    )

    @property
    def duration_minutes(self) -> int | None:
        if not self.checked_out_at:
            return None
        return int((self.checked_out_at - self.checked_in_at).total_seconds() // 60)


class Certification(Base):
    __tablename__ = "certifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    machine_id: Mapped[int | None] = mapped_column(ForeignKey("machines.id"))
    group_id: Mapped[int | None] = mapped_column(ForeignKey("machine_groups.id"))
    granted_by: Mapped[str] = mapped_column(String(60))
    granted_at: Mapped[datetime] = mapped_column(default=_now)
    expires_at: Mapped[datetime | None]
    revoked_at: Mapped[datetime | None]
    notes: Mapped[str | None] = mapped_column(String(255))
    student: Mapped[Student] = relationship()
    machine: Mapped[Machine | None] = relationship()
    group: Mapped[MachineGroup | None] = relationship()
    __table_args__ = (CheckConstraint("(machine_id is null) <> (group_id is null)", name="ck_cert_target"),)


class SafetyWaiver(Base):
    __tablename__ = "safety_waivers"
    id: Mapped[int] = mapped_column(primary_key=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    version: Mapped[str] = mapped_column(String(20))
    signed_at: Mapped[datetime] = mapped_column(default=_now)
    ip: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("student_id", "version"),)


class MaintenanceRecord(Base):
    __tablename__ = "maintenance_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    machine_id: Mapped[int] = mapped_column(ForeignKey("machines.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # issue | scheduled | inspection | repair
    description: Mapped[str] = mapped_column(Text)
    state_set: Mapped[str | None] = mapped_column(String(30))  # maintenance_state applied
    opened_at: Mapped[datetime] = mapped_column(default=_now)
    opened_by: Mapped[str] = mapped_column(String(80))
    closed_at: Mapped[datetime | None]
    closed_by: Mapped[str | None] = mapped_column(String(80))
    window_start: Mapped[datetime | None]  # scheduled maintenance blocks bookings
    window_end: Mapped[datetime | None]
    machine: Mapped[Machine] = relationship()


class DamageReport(Base):
    __tablename__ = "damage_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    machine_id: Mapped[int] = mapped_column(ForeignKey("machines.id"), index=True)
    student_id: Mapped[int | None] = mapped_column(ForeignKey("students.id"))
    session_id: Mapped[int | None] = mapped_column(ForeignKey("sessions.id"))
    severity: Mapped[str] = mapped_column(String(20))  # damage | maintenance
    description: Mapped[str] = mapped_column(Text)
    photo_key: Mapped[str | None] = mapped_column(String(255))
    photo_name: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="open")
    created_at: Mapped[datetime] = mapped_column(default=_now)
    machine: Mapped[Machine] = relationship()
    student: Mapped[Student | None] = relationship()


class IncidentReport(Base):
    __tablename__ = "incident_reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # injury | near_miss | other
    occurred_at: Mapped[datetime]
    machine_id: Mapped[int | None] = mapped_column(ForeignKey("machines.id"))
    student_id: Mapped[int | None] = mapped_column(ForeignKey("students.id"))
    description: Mapped[str] = mapped_column(Text)
    action_taken: Mapped[str | None] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(20), default="minor")
    reported_by: Mapped[str] = mapped_column(String(60))
    resolved: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    machine: Mapped[Machine | None] = relationship()
    student: Mapped[Student | None] = relationship()


class ShopHours(Base):
    """Weekly pattern (weekday set) or a dated exception/holiday (date set)."""
    __tablename__ = "shop_hours"
    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), default=1)
    weekday: Mapped[int | None]  # 0=Mon
    on_date: Mapped[date | None] = mapped_column(Date)
    open_time: Mapped[time | None] = mapped_column(Time)
    close_time: Mapped[time | None] = mapped_column(Time)
    closed: Mapped[bool] = mapped_column(default=False)
    label: Mapped[str | None] = mapped_column(String(80))
    __table_args__ = (
        CheckConstraint("(weekday is null) <> (on_date is null)", name="ck_hours_kind"),
        UniqueConstraint("location_id", "weekday"),
        UniqueConstraint("location_id", "on_date"),
    )


class ShiftSchedule(Base):
    __tablename__ = "shift_schedule"
    id: Mapped[int] = mapped_column(primary_key=True)
    location_id: Mapped[int] = mapped_column(ForeignKey("locations.id"), default=1)
    staff_id: Mapped[int] = mapped_column(ForeignKey("staff_users.id"))
    start_at: Mapped[datetime]
    end_at: Mapped[datetime]
    staff: Mapped[StaffUser] = relationship()
    __table_args__ = (CheckConstraint("end_at > start_at", name="ck_shift_window"),)


class ConsumableLog(Base):
    __tablename__ = "consumable_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("sessions.id"))
    item: Mapped[str] = mapped_column(String(120))
    quantity: Mapped[float]
    unit: Mapped[str] = mapped_column(String(20), default="ea")
    logged_at: Mapped[datetime] = mapped_column(default=_now)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[object] = mapped_column(JSON)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(default=_now, index=True)
    actor: Mapped[str] = mapped_column(String(120))  # staff:alice | student:a@x | system
    action: Mapped[str] = mapped_column(String(80), index=True)
    entity: Mapped[str | None] = mapped_column(String(60))
    entity_id: Mapped[str | None] = mapped_column(String(60))
    before: Mapped[dict | None] = mapped_column(JSON)
    after: Mapped[dict | None] = mapped_column(JSON)
    ip: Mapped[str | None] = mapped_column(String(64))


@event.listens_for(AuditLog, "before_update")
@event.listens_for(AuditLog, "before_delete")
def _audit_append_only(mapper, connection, target):  # noqa: ANN001
    raise RuntimeError("audit_log is append-only")


class RotatingCode(Base):
    __tablename__ = "rotating_codes"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(8))
    starts_at: Mapped[datetime] = mapped_column(unique=True)
    ends_at: Mapped[datetime]


# --- infrastructure tables (not in the spec list, needed for the design) -------
class RateEvent(Base):
    """DB-backed rate limiting: works across restarts and multiple workers."""
    __tablename__ = "rate_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(200))
    at: Mapped[datetime] = mapped_column(default=_now)
    __table_args__ = (Index("ix_rate_key_at", "key", "at"),)


class IdempotencyKey(Base):
    """Form tokens already processed: double-taps replay the original result."""
    __tablename__ = "idempotency_keys"
    token: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[str] = mapped_column(String(40))
    result: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(default=_now)


class EmailOutbox(Base):
    __tablename__ = "email_outbox"
    id: Mapped[int] = mapped_column(primary_key=True)
    to_email: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str] = mapped_column(String(255))
    body_text: Mapped[str] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending|sent|failed
    attempts: Mapped[int] = mapped_column(default=0)
    last_error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(default=_now)
    sent_at: Mapped[datetime | None]


# --- access lists and peer time-change requests ----------------------------------------
ACCESS_APPROVED = "approved"
ACCESS_SUPER = "super_approved"


class AccessRoster(Base):
    """Who may use the machines. Two lists, managed by staff; students never see which list anyone is on."""
    __tablename__ = "access_roster"
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)  # lower-cased
    level: Mapped[str] = mapped_column(String(20))
    added_by: Mapped[str] = mapped_column(String(60))
    added_at: Mapped[datetime] = mapped_column(default=_now)
    note: Mapped[str | None] = mapped_column(String(255))
    __table_args__ = (CheckConstraint("level in ('approved','super_approved')", name="ck_roster_level"),)


class TimeChangeRequest(Base):
    """A polite, optional ask: 'could you move your slot to X so I can use the machine?' No priority, no
    pressure: the other student can accept or decline in one tap, and a decline is never shown to anyone."""
    __tablename__ = "time_change_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    requester_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    target_reservation_id: Mapped[int] = mapped_column(ForeignKey("reservations.id"), index=True)
    target_student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    requester_approval_id: Mapped[int] = mapped_column(ForeignKey("approvals.id"))
    proposed_start: Mapped[datetime]  # where the target's reservation would move to
    proposed_end: Mapped[datetime]
    note: Mapped[str | None] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)  # pending|accepted|declined|expired|cancelled
    created_at: Mapped[datetime] = mapped_column(default=_now)
    expires_at: Mapped[datetime]
    responded_at: Mapped[datetime | None]
    requester: Mapped[Student] = relationship(foreign_keys=[requester_id])
    target_student: Mapped[Student] = relationship(foreign_keys=[target_student_id])
    target_reservation: Mapped[Reservation] = relationship(foreign_keys=[target_reservation_id])


class MachineHold(Base):
    """'My setup / my tools are in this machine: don't touch.' Capped in business days; one active hold per machine."""
    __tablename__ = "machine_holds"
    id: Mapped[int] = mapped_column(primary_key=True)
    machine_id: Mapped[int] = mapped_column(ForeignKey("machines.id"), index=True)
    student_id: Mapped[int] = mapped_column(ForeignKey("students.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # setup | tools
    started_at: Mapped[datetime] = mapped_column(default=_now)
    expires_at: Mapped[datetime]
    released_at: Mapped[datetime | None]
    release_reason: Mapped[str | None] = mapped_column(String(20))  # owner | staff | expired
    declared_via: Mapped[str] = mapped_column(String(20), default="checkout")
    last_reminder_on: Mapped[date | None] = mapped_column(Date)  # local date of the last daily reminder
    note: Mapped[str | None] = mapped_column(String(200))
    machine: Mapped[Machine] = relationship()
    student: Mapped[Student] = relationship()
    __table_args__ = (
        Index("uq_active_hold_machine", "machine_id", unique=True,
              postgresql_where=text("released_at is null"), sqlite_where=text("released_at is null")),
        CheckConstraint("kind in ('setup','tools')", name="ck_hold_kind"),
    )
