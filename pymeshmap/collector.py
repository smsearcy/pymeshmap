"""Repeatedly collects data about the network and stores it to the database."""
from __future__ import annotations

import asyncio
import math
import sys
import time
from collections import defaultdict
from operator import attrgetter
from typing import DefaultDict, List, Optional

import pendulum
from loguru import logger
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from . import models
from .aredn import SystemInfo
from .config import AppConfig
from .models import CollectorStat, Link, LinkStatus, Node, NodeStatus
from .poller import LinkInfo, OlsrData, Poller

MODEL_TO_SYSINFO_ATTRS = {
    "name": "node_name",
    "wlan_ip": "wlan_ip_address",
    "description": "description",
    "wlan_mac_address": "wlan_mac_address",
    "up_time": "up_time",
    "load_averages": "load_averages",
    "model": "model",
    "board_id": "board_id",
    "firmware_version": "firmware_version",
    "firmware_manufacturer": "firmware_manufacturer",
    "api_version": "api_version",
    "latitude": "latitude",
    "longitude": "longitude",
    "grid_square": "grid_square",
    "ssid": "ssid",
    "channel": "channel",
    "channel_bandwidth": "channel_bandwidth",
    "band": "band",
    "services": "services_json",
    "tunnel_installed": "tunnel_installed",
    "active_tunnel_count": "active_tunnel_count",
    "system_info": "source_json",
}


def main(app_config: AppConfig, *, run_once: bool = False):
    """Map the network and store information in the database."""

    log_level = app_config.log_level
    logger.remove()
    logger.add(sys.stderr, level=log_level)

    try:
        dbsession_factory = models.get_session_factory(models.get_engine(app_config))
    except Exception as exc:
        logger.error(f"Failed to configure database connection: {exc!r}")
        return "Database configuration failed, review logs for details"

    async_debug = log_level == "DEBUG"
    try:
        asyncio.run(
            service(
                app_config.poller.node,
                Poller.from_config(app_config.poller),
                dbsession_factory,
                polling_period=app_config.collector.period,
                nodes_expire=app_config.collector.node_inactive,
                links_expire=app_config.collector.link_inactive,
                max_retries=app_config.collector.max_retries,
                run_once=run_once,
            ),
            debug=async_debug,
        )
    except ServiceError as exc:
        return str(exc)
    except OperationalError as exc:
        logger.error(repr(exc))
        return "Database error, review logs for details"
    except KeyboardInterrupt:
        pass
    return


class ServiceError(Exception):
    """Custom exception for known issues to report on the command line."""

    pass


async def service(
    local_node: str,
    poller: Poller,
    session_factory,
    *,
    polling_period: int,
    nodes_expire: int,
    links_expire: int,
    run_once: bool = False,
    max_retries: int = 5,
):
    """Service function to repeatedly poll the network and save the data.

    Args:
        local_node: Name of the local node to connect to
        poller: Poller for getting information from the AREDN network
        session_factory: SQLAlchemy session factory
        polling_period: Period (in minutes) between polling the network
        nodes_expire: Number of days before absent nodes are marked inactive
        links_expire: Number of days before absent links are marked inactive
        run_once: Only run the network collection once
        max_retries: Number of consecutive connection failures before aborting

    """

    run_period_seconds = polling_period * 60
    connection_failures = 0
    while True:
        started_at = pendulum.now()
        start_time = time.monotonic()

        try:
            olsr_data = await OlsrData.connect(local_node)
        except RuntimeError as exc:
            connection_failures += 1
            logger.error(f"{exc!s} (tries: {connection_failures})")
            if connection_failures >= max_retries or run_once:
                raise ServiceError(
                    f"Failed to connect to '{local_node}' for network data "
                    f"{connection_failures} times in a row.  Aborting."
                )
            await asyncio.sleep(run_period_seconds)
            continue

        # reset the error counter
        connection_failures = 0

        nodes, links, errors = await poller.network_info(olsr_data)

        poller_finished = time.monotonic()
        poller_elapsed = poller_finished - start_time
        logger.info(
            "Network polling took {:.2f}s ({:.2f}m)",
            poller_elapsed,
            poller_elapsed / 60,
        )

        summary: DefaultDict[str, int] = defaultdict(int)
        with models.session_scope(session_factory) as dbsession:
            save_nodes(nodes, dbsession, count=summary)
            save_links(links, dbsession, count=summary)
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

            logger.info(
                "Database updates took {:.2f}s ({:.2f}m)",
                updates_elapsed,
                updates_elapsed / 60,
            )

            # TODO: fill in "other_stats" with error types and node/link details
            dbsession.add(
                CollectorStat(
                    started_at=started_at,
                    node_count=len(nodes),
                    link_count=len(links),
                    error_count=len(errors),
                    polling_duration=poller_elapsed,
                    total_duration=time.monotonic() - start_time,
                    other_stats=dict(summary),
                )
            )

        total_elapsed = time.monotonic() - start_time
        logger.info("Total time: {:.2f}s ({:.2f}m)", total_elapsed, total_elapsed / 60)

        if run_once:
            break

        remaining_time = run_period_seconds - (total_elapsed % run_period_seconds)
        logger.debug(
            "Sleeping {:.2f}s until next querying network again", remaining_time
        )
        await asyncio.sleep(remaining_time)

    return


def expire_data(
    dbsession: Session,
    *,
    nodes_expire: int,
    links_expire: int,
    count: DefaultDict[str, int] = None,
):
    """Update the status of nodes/links that have not been seen recently.

    Args:
        dbsession: SQLAlchemy database session
        nodes_expire: Number of days a node is not seen before marked inactive
        links_expire: Number of days a link is not seen before marked inactive

    """

    timestamp = pendulum.now()

    if count is None:
        count = defaultdict(int)
    inactive_cutoff = timestamp.subtract(days=links_expire)
    count["expired: links"] = (
        dbsession.query(Link)
        .filter(
            Link.status == LinkStatus.RECENT,
            Link.last_seen < inactive_cutoff,
        )
        .update({Link.status: LinkStatus.INACTIVE})
    )
    logger.info(
        "Marked {:,d} links inactive that have not been seen since {}",
        count["expired: links"],
        inactive_cutoff,
    )

    inactive_cutoff = timestamp.subtract(days=nodes_expire)
    count["expired: nodes"] = (
        dbsession.query(Node)
        .filter(
            Node.status == NodeStatus.ACTIVE,
            Node.last_seen < inactive_cutoff,
        )
        .update({Node.status: NodeStatus.INACTIVE})
    )
    logger.info(
        "Marked {:,d} nodes inactive that have not been seen since {}",
        count["expired: nodes"],
        inactive_cutoff,
    )
    return


def save_nodes(
    nodes: List[SystemInfo], dbsession: Session, *, count: DefaultDict[str, int] = None
):
    """Saves node information to the database.

    Looks for existing nodes by WLAN MAC address and name.

    """
    if count is None:
        count = defaultdict(int)
    for node in nodes:
        count["nodes: total"] += 1
        # check to see if node exists in database by name and WLAN MAC address

        model = get_db_model(dbsession, node)

        if model is None:
            # create new database model
            logger.debug("Saving {} to database", node)
            count["nodes: added"] += 1
            model = Node()
            dbsession.add(model)
        else:
            # update database model
            logger.debug("Updating {} in database with {}", model, node)
            count["nodes: updated"] += 1

        model.last_seen = pendulum.now()
        model.status = NodeStatus.ACTIVE

        for model_attr, node_attr in MODEL_TO_SYSINFO_ATTRS.items():
            setattr(model, model_attr, getattr(node, node_attr))

    logger.success("Nodes written to database: {}", dict(count))
    return


def get_db_model(dbsession: Session, node: SystemInfo) -> Optional[Node]:
    """Get the best match database record for this node."""
    # Find the most recently seen node that matches both name and MAC address
    query = dbsession.query(Node).filter(
        Node.wlan_mac_address == node.wlan_mac_address,
        Node.name == node.node_name,
    )
    model = _get_most_recent(query.all())
    if model:
        return model

    # Find active node with same hardware
    query = dbsession.query(Node).filter(
        Node.wlan_mac_address == node.wlan_mac_address, Node.status == NodeStatus.ACTIVE
    )
    model = _get_most_recent(query.all())
    if model:
        return model

    # Find active node with same name
    query = dbsession.query(Node).filter(
        Node.name == node.node_name, Node.status == NodeStatus.ACTIVE
    )
    model = _get_most_recent(query.all())
    if model:
        return model

    # Nothing found, treat as a new node
    return None


def _get_most_recent(results: List[Node]) -> Optional[Node]:
    """Get the most recently seen node, marking the others inactive."""
    if len(results) == 0:
        return None

    results = sorted(results, key=attrgetter("last_seen"), reverse=True)
    for model in results[1:]:
        if model.status == NodeStatus.ACTIVE:
            logger.debug("Marking older match inactive: {}", model)
            model.status = NodeStatus.INACTIVE

    return results[0]


def save_links(
    links: List[LinkInfo], dbsession: Session, *, count: DefaultDict[str, int] = None
):
    """Saves link data to the database.

    This implements the bearing/distance functionality in Python
    rather than using SQL triggers,
    thus the MeshMap triggers will need to be deleted/disabled.

    """
    if count is None:
        count = defaultdict(int)

    # Downgrade all "current" links to "recent" so that only ones updated are "current"
    dbsession.query(Link).filter(Link.status == LinkStatus.CURRENT).update(
        {Link.status: LinkStatus.RECENT}
    )

    for link in links:
        count["links: total"] += 1
        source: Node = (
            dbsession.query(Node)
            .filter(Node.wlan_ip == link.source, Node.status == NodeStatus.ACTIVE)
            .one_or_none()
        )
        destination: Node = (
            dbsession.query(Node)
            .filter(Node.wlan_ip == link.destination, Node.status == NodeStatus.ACTIVE)
            .one_or_none()
        )
        if source is None or destination is None:
            logger.warning(
                "Failed to save link {} -> {}, node missing from database",
                link.source,
                link.destination,
            )
            count["links: errors"] += 1
            continue
        model = (
            dbsession.query(Link)
            .filter(
                Link.source == source,
                Link.destination == destination,
            )
            .one_or_none()
        )

        if model is None:
            count["links: new"] += 1
            model = Link(source=source, destination=destination)
            dbsession.add(model)
        else:
            count["links: updated"] += 1

        model.olsr_cost = link.cost
        model.status = LinkStatus.CURRENT
        model.last_seen = pendulum.now()

        for attribute in [
            "type",
            "signal",
            "noise",
            "tx_rate",
            "rx_rate",
            "quality",
            "neighbor_quality",
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

    logger.success("Links written to database: {}", dict(count))
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

    return round(math.degrees(b), 1)
