from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
     BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)

from app.db.base import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    github_login = Column(String(255), nullable=False, unique=True, index=True)
    github_id = Column(String(255), nullable=False, unique=True, index=True)
    display_name = Column(String(255), nullable=True)
    email = Column(String(255), nullable=True)
    encrypted_access_token = Column(String(1024), nullable=True)
    encrypted_refresh_token = Column(String(1024), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, index=True)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)
    is_active = Column(Boolean, default=True, nullable=False)


class Session(Base):
    __tablename__ = "sessions"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    session_token_hash = Column(String(256), nullable=False, unique=True, index=True)
    csrf_token = Column(String(256), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    last_activity_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class GitHubConnection(Base):
    __tablename__ = "github_connections"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    connection_type = Column(String(32), nullable=False, index=True)
    encrypted_token = Column(String(1024), nullable=True)
    github_app_installation_id = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class Installation(Base):
    __tablename__ = "installations"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    github_app_installation_id = Column(String(255), nullable=False, unique=True, index=True)
    github_account_login = Column(String(255), nullable=False, index=True)
    github_account_type = Column(String(32), nullable=False)
    permissions = Column(String(1024), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class Repository(Base):
    __tablename__ = "repositories"
    __table_args__ = (Index("ix_repositories_owner_repo", "owner_login", "repo_name"),)

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    github_repo_id = Column(String(255), nullable=False, unique=True, index=True)
    owner_login = Column(String(255), nullable=False, index=True)
    repo_name = Column(String(255), nullable=False, index=True)
    full_name = Column(String(512), nullable=False, index=True)
    is_public = Column(Boolean, default=True, nullable=False)
    default_branch = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_user_state_created", "user_id", "execution_state", "created_at"),
        Index("ix_runs_source_target_sha", "source_sha", "target_sha"),
    )

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)

    source_type = Column(String(32), nullable=False)
    repo_id = Column(Uuid(as_uuid=True), ForeignKey("repositories.id", ondelete="SET NULL"), nullable=True, index=True)
    source_sha = Column(String(40), nullable=False, index=True)
    target_sha = Column(String(40), nullable=False, index=True)

    execution_state = Column(
        Enum(
            "queued", "preparing", "generating", "validating",
            "running", "checking_coverage", "improving",
            "completed", "failed", "cancelled",
            name="run_execution_state",
        ),
        nullable=False,
        default="queued",
        index=True,
    )
    verdict = Column(
        Enum("passed", "failed", "not_applicable", name="run_verdict"),
        nullable=True,
        index=True,
    )
    freshness = Column(
        Enum("current", "stale", name="run_freshness"),
        nullable=False,
        default="current",
        index=True,
    )
    merge_status = Column(
        Enum("pending", "allowed", "denied", "completed", "conflict", name="run_merge_status"),
        nullable=False,
        default="pending",
        index=True,
    )

    mode = Column(String(32), nullable=False)
    auto_merge_enabled = Column(Boolean, default=False, nullable=False)
    environment_profile = Column(String(128), nullable=False)
    dependency_profile = Column(String(128), nullable=False)

    max_iterations = Column(Integer, nullable=False, default=3)
    remaining_budget = Column(Integer, nullable=False, default=3)

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)
    completed_at = Column(DateTime(timezone=True), nullable=True)
    cancelled_at = Column(DateTime(timezone=True), nullable=True)


class Attempt(Base):
    __tablename__ = "attempts"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id = Column(Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    snapshot_id = Column(Uuid(as_uuid=True), nullable=False, unique=True, index=True)
    source_sha = Column(String(40), nullable=False)
    target_sha = Column(String(40), nullable=False)
    content_hash = Column(String(128), nullable=False, index=True)
    test_bundle_hash = Column(String(128), nullable=False, index=True)
    environment_profile = Column(String(128), nullable=False)

    attempt_state = Column(
        Enum("pending", "running", "completed", "failed", "cancelled", name="attempt_state"),
        nullable=False,
        default="pending",
        index=True,
    )

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class Scenario(Base):
    __tablename__ = "scenarios"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    attempt_id = Column(Uuid(as_uuid=True), ForeignKey("attempts.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id = Column(Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)
    scenario_id = Column(Uuid(as_uuid=True), nullable=False, index=True)
    source_refs = Column(String(1024), nullable=False)
    requirement_tag = Column(String(32), nullable=False)
    gherkin_text = Column(Text, nullable=False)
    prompt_model = Column(String(256), nullable=False)
    prompt_version = Column(String(32), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class Artifact(Base):
    __tablename__ = "artifacts"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id = Column(Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True)
    attempt_id = Column(Uuid(as_uuid=True), ForeignKey("attempts.id", ondelete="SET NULL"), nullable=True, index=True)

    volume_path = Column(String(1024), nullable=False, unique=True, index=True)
    relative_path = Column(String(1024), nullable=False)
    size_bytes = Column(BigInteger, nullable=False, default=0)
    content_hash = Column(String(128), nullable=False, index=True)
    artifact_type = Column(String(64), nullable=False, index=True)

    owner_user_id = Column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    retention_until = Column(DateTime(timezone=True), nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)


class CoverageReport(Base):
    __tablename__ = "coverage_reports"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    attempt_id = Column(
        Uuid(as_uuid=True),
        ForeignKey("attempts.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    run_id = Column(Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True)

    covered_changed_lines = Column(Integer, nullable=False, default=0)
    executable_changed_lines = Column(Integer, nullable=False, default=0)
    coverage_percentage = Column(Float, nullable=True)

    covered_files = Column(String(2048), nullable=True)
    uncovered_changed_lines = Column(String(2048), nullable=True)
    raw_coverage_path = Column(String(1024), nullable=True)

    coverage_verdict = Column(
        Enum("passed", "failed", "not_applicable", name="coverage_verdict"),
        nullable=False,
        default="not_applicable",
        index=True,
    )

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class GitHubAction(Base):
    __tablename__ = "github_actions"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    run_id = Column(Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="SET NULL"), nullable=True, index=True)

    operation_type = Column(String(64), nullable=False)
    repo_owner = Column(String(255), nullable=True)
    repo_name = Column(String(255), nullable=True)
    branch_name = Column(String(255), nullable=True)
    pr_number = Column(Integer, nullable=True)

    expected_sha = Column(String(40), nullable=True)
    actual_sha = Column(String(40), nullable=True)

    status = Column(String(32), nullable=False, index=True)
    safe_reason = Column(String(2000), nullable=True)
    reconciliation_id = Column(Uuid(as_uuid=True), nullable=False, unique=True, index=True)
    initiator_user_id = Column(Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)

    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
    updated_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow)


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"

    id = Column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    delivery_id = Column(String(255), nullable=False, unique=True, index=True)
    event_type = Column(String(64), nullable=False, index=True)
    payload_hash = Column(String(128), nullable=False)
    signature_valid = Column(Boolean, nullable=False, default=False, index=True)
    processed = Column(Boolean, nullable=False, default=False, index=True)
    processed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)
