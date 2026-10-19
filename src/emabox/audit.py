"""Metadata-only request audit with automatic retention cleanup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import DateTime, Integer, String, create_engine, delete
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column


class Base(DeclarativeBase):
    pass


class AuditRow(Base):
    __tablename__ = "request_audit"

    request_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    app_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    specialist: Mapped[str] = mapped_column(String(20), nullable=False)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    streamed: Mapped[bool] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)


@dataclass(frozen=True)
class AuditEvent:
    request_id: str
    app_id: str
    specialist: str
    provider: str
    model: str
    streamed: bool
    status: str
    latency_ms: int
    input_tokens: int | None = None
    output_tokens: int | None = None


class AuditStore:
    def __init__(self, database_url: str, retention_days: int):
        options = {"check_same_thread": False} if database_url.startswith("sqlite:") else {}
        self.engine = create_engine(database_url, connect_args=options, pool_pre_ping=True)
        self.retention_days = retention_days

    def create(self) -> None:
        Base.metadata.create_all(self.engine)
        self.purge()

    def write(self, event: AuditEvent) -> None:
        with Session(self.engine) as session:
            session.add(AuditRow(
                request_id=event.request_id,
                created_at=datetime.now(UTC).replace(tzinfo=None),
                app_id=event.app_id,
                specialist=event.specialist,
                provider=event.provider,
                model=event.model,
                streamed=event.streamed,
                status=event.status,
                latency_ms=event.latency_ms,
                input_tokens=event.input_tokens,
                output_tokens=event.output_tokens,
            ))
            session.commit()

    def purge(self) -> int:
        cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=self.retention_days)
        with Session(self.engine) as session:
            result = session.execute(delete(AuditRow).where(AuditRow.created_at < cutoff))
            session.commit()
            return result.rowcount or 0

    def close(self) -> None:
        self.engine.dispose()
