from __future__ import annotations

import pendulum
from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..poller import PollingError
from .meta import Base


class NodeError(Base):
    """Information about nodes with errors during collection."""

    __tablename__ = "node_error"

    timestamp: Mapped[pendulum.DateTime] = mapped_column(
        ForeignKey("collector_stat.started_at"), primary_key=True
    )
    ip_address: Mapped[str] = mapped_column(String(15), primary_key=True)
    dns_name: Mapped[str] = mapped_column(String(70))
    error_type: Mapped[PollingError]
    details: Mapped[str]


class CollectorStat(Base):
    """Statistics from the collection process."""

    __tablename__ = "collector_stat"

    started_at: Mapped[pendulum.DateTime] = mapped_column(primary_key=True)
    finished_at: Mapped[pendulum.DateTime] = mapped_column(default=pendulum.now)
    node_count: Mapped[int]
    link_count: Mapped[int]
    error_count: Mapped[int]
    polling_duration: Mapped[float]
    total_duration: Mapped[float]
    other_stats: Mapped[dict] = mapped_column(JSON)

    node_errors: Mapped[list[NodeError]] = relationship(
        foreign_keys="NodeError.timestamp", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<models.CollectorStat(started_at={self.started_at})>"
