"""MySQL配置和连接；不读取公共root配置，不在日志输出连接凭据。"""

from datetime import date, datetime
from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import (
    JSON,
    URL,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    create_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FITLIFE_DB_", extra="ignore")
    environment: Literal["dev", "test"] = "test"
    host: str = "127.0.0.1"
    port: int = 3306
    password: SecretStr | None = None

    def engine(self):
        if self.password is None:
            from fitlife.credentials import database_password

            self.password = SecretStr(
                database_password(self.environment, self.host, self.port, interactive=False)
            )
        suffix = self.environment
        return create_engine(
            URL.create(
                "mysql+pymysql",
                username=f"fit_{suffix}",
                password=self.password.get_secret_value(),
                host=self.host,
                port=self.port,
                database=f"fitlife_{suffix}",
                query={"charset": "utf8mb4"},
            ),
            pool_pre_ping=True,
            pool_size=5,
            max_overflow=0,
            pool_timeout=5,
            hide_parameters=True,
            connect_args={
                "connect_timeout": 5,
                "read_timeout": 10,
                "write_timeout": 10,
            },
        )


class Base(DeclarativeBase):
    pass


MYSQL = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_bin",
}


class User(Base):
    __tablename__ = "fl_users"
    __table_args__ = MYSQL
    id: Mapped[str] = mapped_column(String(36), primary_key=True)


class AccessToken(Base):
    __tablename__ = "fl_access_tokens"
    __table_args__ = MYSQL
    digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("fl_users.id"), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    revoked: Mapped[bool] = mapped_column(default=False)


class AgentRequest(Base):
    __tablename__ = "fl_agent_requests"
    __table_args__ = (
        UniqueConstraint("owner_id", "request_key", name="uq_fl_agent_request"),
        MYSQL,
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("fl_users.id"), index=True)
    request_key: Mapped[str] = mapped_column(String(36))
    message_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    nonce: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    lease_until: Mapped[datetime] = mapped_column(DateTime)
    response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(
        ForeignKey("fl_conversations.id"), index=True, nullable=True
    )
    user_message: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    context: Mapped[list | None] = mapped_column(JSON, nullable=True)
    turn_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    memory_scope: Mapped[str | None] = mapped_column(String(64), nullable=True)


class Conversation(Base):
    __tablename__ = "fl_conversations"
    __table_args__ = MYSQL
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("fl_users.id"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)
    active_request_key: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime] = mapped_column(DateTime)
    deleted: Mapped[bool] = mapped_column(default=False)
    version: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(80), default="新对话")
    title_source: Mapped[str] = mapped_column(String(12), default="pending")
    title_revision: Mapped[int] = mapped_column(Integer, default=0)


class PendingAction(Base):
    __tablename__ = "fl_pending_actions"
    __table_args__ = (
        UniqueConstraint("owner_id", "request_key", "kind", name="uq_fl_request"),
        MYSQL,
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("fl_users.id"), index=True)
    request_key: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    revision: Mapped[int] = mapped_column(Integer)
    digest: Mapped[str] = mapped_column(String(64))
    base_profile_version: Mapped[int] = mapped_column(Integer)
    base_plan_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class Profile(Base):
    __tablename__ = "fl_profiles"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_fl_profile_version"),
        MYSQL,
    )
    owner_id: Mapped[str] = mapped_column(ForeignKey("fl_users.id"), primary_key=True)
    values: Mapped[dict] = mapped_column(JSON)
    provenance: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class Activity(Base):
    __tablename__ = "fl_activities"
    __table_args__ = (
        UniqueConstraint("source_action_id", name="uq_fl_activity_action"),
        CheckConstraint("minutes >= 1 AND minutes <= 1440", name="ck_fl_activity_minutes"),
        MYSQL,
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("fl_users.id"), index=True)
    source_action_id: Mapped[str] = mapped_column(ForeignKey("fl_pending_actions.id"))
    activity_id: Mapped[str] = mapped_column(String(40))
    occurred_on: Mapped[date] = mapped_column(Date, index=True)
    minutes: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class Plan(Base):
    __tablename__ = "fl_plans"
    __table_args__ = (UniqueConstraint("owner_id", "week_start", name="uq_fl_plan_week"), MYSQL)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("fl_users.id"), index=True)
    week_start: Mapped[date] = mapped_column(Date)
    version: Mapped[int] = mapped_column(Integer)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class PlanRevision(Base):
    __tablename__ = "fl_plan_revisions"
    __table_args__ = (UniqueConstraint("source_action_id", name="uq_fl_plan_action"), MYSQL)
    plan_id: Mapped[str] = mapped_column(ForeignKey("fl_plans.id"), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_action_id: Mapped[str] = mapped_column(ForeignKey("fl_pending_actions.id"))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class DomainRecord(Base):
    __tablename__ = "fl_domain_records"
    __table_args__ = (
        UniqueConstraint("owner_id", "record_type", "record_key", name="uq_fl_domain_scope"),
        CheckConstraint("version >= 1", name="ck_fl_domain_version"),
        MYSQL,
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("fl_users.id"), index=True)
    record_type: Mapped[str] = mapped_column(String(32))
    record_key: Mapped[str] = mapped_column(String(100))
    values: Mapped[dict] = mapped_column(JSON)
    source_media: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    version: Mapped[int] = mapped_column(Integer)
    source_action_id: Mapped[str] = mapped_column(ForeignKey("fl_pending_actions.id"))
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class MediaAsset(Base):
    __tablename__ = "fl_media_assets"
    __table_args__ = (MYSQL,)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("fl_users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    sha256: Mapped[str] = mapped_column(String(64))
    metadata_: Mapped[dict] = mapped_column("metadata", JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime)


class MediaJob(Base):
    __tablename__ = "fl_media_jobs"
    __table_args__ = (
        UniqueConstraint("owner_id", "request_key", name="uq_fl_media_request"),
        MYSQL,
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("fl_users.id"), index=True)
    asset_id: Mapped[str] = mapped_column(ForeignKey("fl_media_assets.id"))
    request_key: Mapped[str] = mapped_column(String(36))
    input_digest: Mapped[str] = mapped_column(String(64))
    task: Mapped[str] = mapped_column(String(24))
    message: Mapped[str] = mapped_column(String(2000))
    status: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(Integer)
    attempts: Mapped[int] = mapped_column(Integer)
    model_calls: Mapped[int] = mapped_column(Integer)
    progress: Mapped[int] = mapped_column(Integer)
    lease_until: Mapped[datetime] = mapped_column(DateTime)
    nonce: Mapped[str] = mapped_column(String(36))
    reviewed: Mapped[bool] = mapped_column(Boolean)
    evidence: Mapped[dict] = mapped_column(JSON)
    partial: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class KnowledgeChunk(Base):
    __tablename__ = "fl_knowledge_chunks"
    __table_args__ = (MYSQL,)
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    sha256: Mapped[str] = mapped_column(String(64))
