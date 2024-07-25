"""Database model(s) for representing links between nodes."""

from typing import TYPE_CHECKING, Optional

import pendulum
from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..types import LinkId, LinkStatus, LinkType
from .meta import Base

if TYPE_CHECKING:
    from .node import Node


class Link(Base):
    """Represents a link between two nodes."""

    __tablename__ = "link"

    source_id: Mapped[int] = mapped_column(ForeignKey("node.node_id"), primary_key=True)
    destination_id: Mapped[int] = mapped_column(
        ForeignKey("node.node_id"), primary_key=True
    )
    type: Mapped[LinkType] = mapped_column(primary_key=True)
    status: Mapped[LinkStatus]
    last_seen: Mapped[pendulum.DateTime] = mapped_column(default=pendulum.now)

    olsr_cost: Mapped[float]
    distance: Mapped[Optional[float]]
    bearing: Mapped[Optional[float]]

    signal: Mapped[Optional[float]]
    noise: Mapped[Optional[float]]
    tx_rate: Mapped[Optional[float]]
    rx_rate: Mapped[Optional[float]]
    quality: Mapped[Optional[float]]
    neighbor_quality: Mapped[Optional[float]]

    created_at: Mapped[pendulum.DateTime] = mapped_column(default=pendulum.now)
    last_updated_at: Mapped[pendulum.DateTime] = mapped_column(
        default=pendulum.now, onupdate=pendulum.now
    )

    source: Mapped["Node"] = relationship(
        foreign_keys="Link.source_id", back_populates="links"
    )
    destination: Mapped["Node"] = relationship(foreign_keys="Link.destination_id")

    @property
    def signal_noise_ratio(self):
        if self.signal is None or self.noise is None:
            return None
        return self.signal - self.noise

    @property
    def id(self) -> LinkId:
        """Simple link identifier (useful for consistent serialization)."""
        return LinkId(self.source_id, self.destination_id, self.type)

    def __repr__(self):
        return (
            f"<models.Link(source_id={self.source_id!r}, "
            f"destination_id={self.destination_id!r})>"
        )
