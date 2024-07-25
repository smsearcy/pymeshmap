"""Repeatedly collects data about the network and stores it to the database."""

from __future__ import annotations

import asyncio
import functools
import math
import time
from collections import defaultdict
from collections.abc import Iterable
from operator import attrgetter
from typing import Sequence

import attrs
import pendulum
import structlog
from sqlalchemy import sql, Row
from sqlalchemy.orm import Session
from structlog.contextvars import bound_contextvars

from . import models
from .aredn import LinkInfo, SystemInfo
from .config import AppConfig
from .historical import HistoricalStats
from .models import CollectorStat, Link, Node, NodeError
from .poller import poll_network
from .types import LinkStatus, NodeStatus

logger = structlog.get_logger()


def main(
    local_node: str,
    dbsession_factory,
    historical_stats: HistoricalStats,
    *,
    config: AppConfig.Collector,
    run_once: bool = False,
):
    """Map the network and store information in the database."""

    collection = functools.partial(
        collector,
        local_node,
        dbsession_factory,
        historical_stats,
        workers=config.workers,
        timeout=config.timeout,
        nodes_expire=config.node_inactive,
        links_expire=config.link_inactive,
    )

    if run_once:
        # collect once then quit
        try:
            asyncio.run(collection())
        except Exception as exc:
            logger.exception("Error!", exc=exc)
            return str(exc)
        except KeyboardInterrupt as exc:
            logger.exception("Aborted!", exc=exc)
            return str(exc)

        return

    try:
        asyncio.run(service(collection, polling_period=config.period))
    except KeyboardInterrupt as exc:
        logger.exception("Aborted!", exc=exc)
        return str(exc)
    return


async def service(collect, *, polling_period: int):
    run_period_seconds = polling_period * 60
    connection_failures = 0
    while True:
        start_time = time.monotonic()

        try:
            await collect()
        except ConnectionError as exc:
            connection_failures += 1
            logger.exception("Connection error", error=exc, tries=connection_failures)
            await asyncio.sleep(run_period_seconds)
            continue
        else:
            # reset the failure count if runs successfully
            connection_failures = 0

        total_elapsed = time.monotonic() - start_time

        remaining_time = run_period_seconds - (total_elapsed % run_period_seconds)
        logger.debug("Sleeping until next period start", sleep_time=remaining_time)
        await asyncio.sleep(remaining_time)

    return


async def collector(
    local_node: str,
    session_factory,
    historical_stats: HistoricalStats,
    *,
    workers: int,
    timeout: int,
    nodes_expire: int,
    links_expire: int,
):
    """Collect the network information and save the data.

    Args:
        local_node: Name of the local node to connect to
        poller: Poller for getting information from the AREDN network
        session_factory: SQLAlchemy session factory
        nodes_expire: Number of days before absent nodes are marked inactive
        links_expire: Number of days before absent links are marked inactive

    """

    started_at = pendulum.now()
    start_time = time.monotonic()

    try:
        nodes, links, errors = await poll_network(
            start_node=local_node, timeout=timeout, workers=workers
        )
    except RuntimeError:
        raise ConnectionError(
            f"Failed to connect to OLSR daemon on {local_node} for network data"
        ) from None

    poller_finished = time.monotonic()
    poller_elapsed = poller_finished - start_time
    logger.info("Network polling complete", seconds=poller_elapsed)

    summary: defaultdict[str, int] = defaultdict(int)
    with models.session_scope(session_factory) as dbsession:
        node_updater = NodeUpdater(dbsession)
        summary |= await node_updater.save_nodes(nodes)
        link_models = save_links(links, dbsession, count=summary)
        # expire data after the data has been refreshed
        # (otherwise the first run after a long gap will mark current stuff expired)
        expire_data(
            dbsession,
            nodes_expire=nodes_expire,
            links_expire=links_expire,
            count=summary,
        )
        dbsession.flush()

        updates_finished = time.monotonic()
        updates_elapsed = updates_finished - poller_finished

        logger.info("Database updates complete", seconds=updates_elapsed)

        await save_historical_data(node_models, link_models, historical_stats)

        history_finished = time.monotonic()
        history_elapsed = history_finished - updates_finished

        logger.info("Saving historical data complete", seconds=history_elapsed)

        total_duration = time.monotonic() - start_time

        stats = CollectorStat(
            started_at=started_at,
            node_count=len(nodes),
            link_count=len(links),
            error_count=len(errors),
            polling_duration=poller_elapsed,
            total_duration=total_duration,
            other_stats=dict(summary),
        )
        for error in errors:
            stats.node_errors.append(
                NodeError(
                    ip_address=error.ip_address,
                    dns_name=error.name,
                    error_type=error.error,
                    details=error.response,
                )
            )
        dbsession.add(stats)

    total_elapsed = time.monotonic() - start_time
    logger.info(
        "Network collection complete", seconds=total_elapsed, minutes=total_elapsed / 60
    )
    historical_stats.update_network_stats(
        node_count=len(nodes),
        link_count=len(links),
        error_count=len(errors),
        poller_time=poller_elapsed,
        total_time=total_duration,
    )

    return

@attrs.define
class NodeUpdater:
    dbsession: Session
    count: defaultdict[str, int] = attrs.field(init=False)
    timestamp: pendulum.DateTime = attrs.field(init=False)
    _insert_nodes: list[dict] = attrs.field(factory=list, init=False)
    _update_nodes: list[dict] = attrs.field(factory=list, init=False)

    def __attrs_post_init__(self):
        self.count = defaultdict(int)
        self.timestamp = pendulum.now()

    async def save_nodes(self, nodes: Iterable[SystemInfo]) -> dict[str, int]:
        """Save nodes to the database.

        Args:
            nodes: Iterable of SystemInfo objects

        Returns:
            Dictionary of counts for nodes added, updated, and total

        """
        self._insert_nodes = []
        self._update_nodes = []

        for node in nodes:
            await self._process(node)

        if self._insert_nodes:
            self.dbsession.execute(
                sql.insert(Node),
                self._insert_nodes,
            )
        if self._update_nodes:
            self.dbsession.execute(
                sql.update(Node),
                self._update_nodes,
            )
        return dict(self.count)

    async def _process(self, node: SystemInfo) -> None:
        """Identify data to insert/update into database.

        Looks for existing nodes by WLAN MAC address and name.

        """

        self.count["nodes: total"] += 1
        # check to see if node exists in database by name and WLAN MAC address

        data = {
            "name": node.node_name,
            "display_name": node.display_name,
            "ip_address": node.ip_address,
            "description": node.description,
            "mac_address": node.mac_address,
            "up_time": node.up_time,
            "up_time_seconds": node.up_time_seconds,
            "load_averages": node.load_averages,
            "model": node.model,
            "board_id": node.board_id,
            "firmware_version": node.firmware_version,
            "firmware_manufacturer": node.firmware_manufacturer,
            "api_version": node.api_version,
            "latitude": node.latitude,
            "longitude": node.longitude,
            "grid_square": node.grid_square,
            "ssid": node.ssid,
            "channel": node.channel,
            "channel_bandwidth": node.channel_bandwidth,
            "band": node.band,
            "services": node.services_json,
            "active_tunnel_count": node.active_tunnel_count,
            "system_info": node.source_json,
            "link_count": node.link_count,
            "radio_link_count": node.radio_link_count,
            "dtd_link_count": node.dtd_link_count,
            "tunnel_link_count": node.tunnel_link_count,
            "last_seen": self.timestamp,
            "status": NodeStatus.ACTIVE,
        }

        with bound_contextvars(node=node.node_name):
            model_id = await self._get_node_id(node)

            if model_id is None:
                # create new database model
                self.count["nodes: added"] += 1
                logger.debug("Added node to database")
                self._insert_nodes.append(data)
            else:
                # update database model
                self.count["nodes: updated"] += 1
                logger.debug("Updated node in database", model=model_id)
                data["id"] = model_id
                self._update_nodes.append(data)


    async def _get_node_id(self, node: SystemInfo) -> int | None:
        """Get the best match database record for this node."""
        # Find the most recently seen node that matches both name and MAC address
        results = self.dbsession.execute(
            sql.select(Node.id, Node.status, Node.last_seen).where(
                Node.mac_address == node.mac_address,
                Node.name == node.node_name,
            )
        ).all()
        if model_id := self._get_most_recent(results):
            return model_id

        # Find active node with same hardware
        results = self.dbsession.execute(
            sql.select(Node.id, Node.status, Node.last_seen).where(
                Node.mac_address == node.mac_address,
                Node.status == NodeStatus.ACTIVE,
                Node.mac_address != "",
            )
        ).all()
        if model_id := self._get_most_recent(results):
            return model_id

        # Find active node with same name
        results = self.dbsession.execute(
            sql.select(Node.id, Node.status, Node.last_seen).where(
                Node.name == node.node_name,
                Node.status == NodeStatus.ACTIVE,
            )
        ).all()
        if model := self._get_most_recent(results):
            return model_id

        # Nothing found, treat as a new node
        return None

    def _get_most_recent(self, results: Sequence[Row]) -> int | None:
        """Get the most recently seen node, marking the others inactive."""
        if len(results) == 0:
            return None

        results = sorted(results, key=attrgetter("last_seen"), reverse=True)
        for row in results[1:]:
            if row.status == NodeStatus.ACTIVE:
                logger.debug("Marking older match inactive", model=row)
                self.count["nodes: inactive"] += 1
                self._update_nodes.append({"id": row.id, "status": NodeStatus.INACTIVE})

        return results[0].id



@attrs.define
class LinkUpdater:
    dbsession: Session
    count: defaultdict[str, int] = attrs.field(init=False)
    timestamp: pendulum.DateTime = attrs.field(init=False)
    _insert_links: list[dict] = attrs.field(factory=list, init=False)
    _update_links: list[dict] = attrs.field(factory=list, init=False)

    def __attrs_post_init__(self):
        self.count = defaultdict(int)
        self.timestamp = pendulum.now()

    def save(self, links: Iterable[LinkInfo]) -> dict[str, int]:
        # Downgrade all "current" links to "recent" so that only ones updated are "current"
        self.dbsession.execute(
            sql.update(Link).where(Link.status == LinkStatus.CURRENT).values(status=LinkStatus.RECENT)
        )


def save_links(
    links: Iterable[LinkInfo],
    dbsession: Session,
    *,
    count: defaultdict[str, int] | None = None,
) -> list[Link]:
    """Saves link data to the database.

    This implements the bearing/distance functionality in Python
    rather than using SQL triggers (i.e. how MeshMap does it).

    """


    active_nodes: dict[str, Node] = {
        node.name: node
        for node in dbsession.query(Node).filter(Node.status == NodeStatus.ACTIVE)
    }

    timestamp = pendulum.now()
    link_models = []
    for link in links:
        count["links: total"] += 1
        source = active_nodes.get(link.source)
        destination = active_nodes.get(link.destination)
        if source is None or destination is None:
            logger.warning(
                "Failed to save link due to missing node",
                source=link.source,
                destination=link.destination,
            )
            count["links: errors"] += 1
            continue
        model = (
            dbsession.query(Link)
            .filter(
                Link.source == source,
                Link.destination == destination,
                Link.type == link.type,
            )
            .one_or_none()
        )

        if model is None:
            count["links: new"] += 1
            model = Link(source=source, destination=destination, type=link.type)
            dbsession.add(model)
        else:
            count["links: updated"] += 1
        link_models.append(model)

        model.status = LinkStatus.CURRENT
        model.last_seen = timestamp

        for attribute in [
            "type",
            "signal",
            "noise",
            "tx_rate",
            "rx_rate",
            "quality",
            "neighbor_quality",
            "olsr_cost",
        ]:
            setattr(model, attribute, getattr(link, attribute))

        if (
            source.longitude is None
            or source.latitude is None
            or destination.longitude is None
            or destination.latitude is None
        ):
            count["links: missing location info"] += 1
            model.distance = None
            model.bearing = None
        else:
            count["links: location calculated"] += 1
            # calculate the bearing/distance
            model.distance = distance(
                source.latitude,
                source.longitude,
                destination.latitude,
                destination.longitude,
            )
            model.bearing = bearing(
                source.latitude,
                source.longitude,
                destination.latitude,
                destination.longitude,
            )

    logger.info("Links written to database", summary=dict(count))
    return link_models


def expire_data(
    dbsession: Session,
    *,
    nodes_expire: int,
    links_expire: int,
    count: defaultdict[str, int],
):
    """Update the status of nodes/links that have not been seen recently.

    Args:
        dbsession: SQLAlchemy database session
        nodes_expire: Number of days a node is not seen before marked inactive
        links_expire: Number of days a link is not seen before marked inactive
        count: Default dictionary for tracking statistics

    """

    timestamp = pendulum.now()

    inactive_cutoff = timestamp.subtract(days=links_expire)
    stmt = (
        sql.update(Link)
        .where(
            Link.status == LinkStatus.RECENT,
            Link.last_seen < inactive_cutoff,
        )
        .values(status=LinkStatus.INACTIVE)
    )
    link_count = dbsession.execute(stmt).rowcount
    logger.info(
        "Marked inactive links",
        count=link_count,
        cutoff=inactive_cutoff,
    )

    inactive_cutoff = timestamp.subtract(days=nodes_expire)
    stmt = (
        sql.update(Node)
        .where(
            Node.status == NodeStatus.ACTIVE,
            Node.last_seen < inactive_cutoff,
        )
        .values(status=NodeStatus.INACTIVE)
    )
    node_count = dbsession.execute(stmt).rowcount
    logger.info(
        "Marked inactive nodes",
        count=node_count,
        cutoff=inactive_cutoff,
    )

    count["expired: links"] = link_count
    count["expired: nodes"] = node_count
    return

def distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distance between two points in kilometers via haversine."""
    # convert from degrees to radians
    lat1 = math.radians(lat1)
    lat2 = math.radians(lat2)
    lon_delta = math.radians(lon2 - lon1)

    # 6371km is the (approximate) radius of Earth
    d = (
        2
        * 6371
        * math.asin(
            math.sqrt(
                hav(lat2 - lat1) + math.cos(lat1) * math.cos(lat2) * hav(lon_delta)
            )
        )
    )
    return round(d, 3)


def hav(theta: float) -> float:
    """Calculate the haversine of an angle."""
    return math.pow(math.sin(theta / 2), 2)


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate the bearing between two coordinates."""
    # convert from degrees to radians
    lat1 = math.radians(lat1)
    lat2 = math.radians(lat2)
    lon_delta = math.radians(lon2 - lon1)

    b = math.atan2(
        math.sin(lon_delta) * math.cos(lat2),
        math.cos(lat1) * math.sin(lat2)
        - math.sin(lat1) * math.cos(lat2) * math.cos(lon_delta),
    )
    if b < 0:
        b = 2 * math.pi + b
    return round(math.degrees(b), 1)


async def save_historical_data(
    nodes: list[SystemInfo], links: list[LinkInfo], stats: HistoricalStats
):
    """Save current node and link data to our time series storage.

    Need to use the database models because we are keying off the database primary keys.
    (And after the session has been flushed.)

    """

    # TODO: Use thread pool to run these asynchronously?
    # (assuming there is need/benefit)

    count: defaultdict[str, int] = defaultdict(int)
    for node in nodes:
        with bound_contextvars(node=node):
            if stats.update_node_stats(node):
                count["Node RRD updates succeeded"] += 1
            else:
                count["Node RRD updates failed"] += 1

    for link in links:
        with bound_contextvars(link=link):
            if stats.update_link_stats(link):
                count["Link RRD updates succeeded"] += 1
            else:
                count["Link RRD updates failed"] += 1

    logger.info("Historical updates completed", summary=dict(count))
