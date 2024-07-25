from typing import TYPE_CHECKING, Optional

import pendulum
from sqlalchemy import JSON, Enum, Index, String, Unicode
from sqlalchemy.orm import Mapped, mapped_column, relationship

from ..types import Band, NodeStatus
from .meta import Base

if TYPE_CHECKING:
    from .link import Link


class Node(Base):
    """Information about a node in the mesh network."""

    __tablename__ = "node"

    id: Mapped[int] = mapped_column("node_id", primary_key=True)
    name: Mapped[str] = mapped_column(String(70))
    status: Mapped[NodeStatus]
    display_name: Mapped[str] = mapped_column(String(70))

    # store the wireless/primary IP address
    ip_address: Mapped[str] = mapped_column("wlan_ip", String(15))
    description: Mapped[str] = mapped_column(Unicode(1024))

    # store the MAC address (without colons) corresponding the primary interface
    mac_address: Mapped[str] = mapped_column("wlan_mac_address", String(12))

    last_seen: Mapped[pendulum.DateTime]

    up_time: Mapped[str] = mapped_column(String(25))
    up_time_seconds: Mapped[Optional[int]]
    load_averages: Mapped[Optional[list[float]]] = mapped_column(JSON)
    model: Mapped[str] = mapped_column(String(50))
    board_id: Mapped[str] = mapped_column(String(50))
    firmware_version: Mapped[str] = mapped_column(String(50))
    firmware_manufacturer: Mapped[str] = mapped_column(String(100))
    api_version: Mapped[str] = mapped_column(String(5))

    latitude: Mapped[Optional[float]]
    longitude: Mapped[Optional[float]]
    grid_square: Mapped[str] = mapped_column(String(20))

    ssid: Mapped[str] = mapped_column(String(50))
    channel: Mapped[str] = mapped_column(String(50))
    channel_bandwidth: Mapped[str] = mapped_column(String(50))
    band: Mapped[Band] = mapped_column(
        Enum(Band, values_callable=lambda x: [e.value for e in x], native_enum=False),
    )

    services: Mapped[dict] = mapped_column(JSON)

    # As of API v1.10 this is irrelevant (because it is always enabled)
    # (probably worth deleting at some point in the future)
    tunnel_installed: Mapped[bool] = mapped_column(default=True)
    active_tunnel_count: Mapped[int]

    link_count: Mapped[Optional[int]]
    radio_link_count: Mapped[Optional[int]]
    dtd_link_count: Mapped[Optional[int]]
    tunnel_link_count: Mapped[Optional[int]]

    system_info: Mapped[dict] = mapped_column(JSON)

    created_at: Mapped[pendulum.DateTime] = mapped_column(default=pendulum.now)
    last_updated_at: Mapped[pendulum.DateTime] = mapped_column(
        default=pendulum.now, onupdate=pendulum.now
    )

    links: Mapped[list["Link"]] = relationship(
        foreign_keys="Link.source_id", back_populates="source"
    )

    # Is this premature optimization?
    Index("idx_mac_name", mac_address, name)

    @property
    def location(self) -> str:
        if self.longitude and self.latitude:
            return f"{self.longitude:.3f},{self.latitude:.3f}"
        elif self.grid_square:
            return self.grid_square
        else:
            return "Unknown"

    def __repr__(self):
        return f"<models.Node(id={self.id!r}, name={self.name!r})>"

    def __str__(self):
        return f"{self.name} ({self.ip_address})"
