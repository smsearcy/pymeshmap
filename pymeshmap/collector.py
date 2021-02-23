"""Maps the network, storing the result in the database."""
from __future__ import annotations

import asyncio
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from operator import attrgetter
from typing import DefaultDict, List, Optional

from loguru import logger
from sqlalchemy.orm import Session

from . import models
from .aredn import SystemInfo
from .config import AppConfig
from .models import Link, LinkStatus, Node, NodeStatus
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


def main(app_config: AppConfig):
    """Map the network and store information in the database."""

    log_level = app_config.log_level
    logger.remove()
    logger.add(sys.stderr, level=log_level)

    try:
        dbsession_factory = models.get_session_factory(models.get_engine(app_config))
    except Exception as exc:
        logger.exception("Failed to connect to database")
        return f"Failed to connect to database: {exc!s}"

    async_debug = log_level == "DEBUG"
    asyncio.run(
        service(
            app_config.poller.node,
            Poller.from_config(app_config.poller),
            dbsession_factory,
            polling_period=app_config.collector.period,
            nodes_expire=app_config.collector.node_inactive,
            links_expire=app_config.collector.link_inactive,
        ),
        debug=async_debug,
    )
    return


async def service(
    local_node: str,
    poller: Poller,
    session_factory,
    *,
    polling_period: int,
    nodes_expire: int,
    links_expire: int,
    run_once: bool = False,
):
    """

    Args:
        local_node: Name of the local node to connect to
        poller: Poller for getting information from the AREDN network
        session_factory: SQLAlchemy session factory
        polling_period: Period (in minutes) between polling the network
        nodes_expire: Number of days before absent nodes are marked inactive
        links_expire: Number of days before absent links are marked inactive
        run_once: Only run the network collection once

    """

    run_period_seconds = polling_period * 60
    while True:
        start_time = time.monotonic()

        with models.session_scope(session_factory) as dbsession:
            expire_data(dbsession, nodes_expire=nodes_expire, links_expire=links_expire)

        olsr_data = await OlsrData.connect(local_node)
        nodes, links, errors = await poller.network_info(olsr_data)

        poller_finished = time.monotonic()
        poller_elapsed = poller_finished - start_time
        logger.info(
            "Network polling took {:.2f}s ({:.2f}m)",
            poller_elapsed,
            poller_elapsed / 60,
        )

        with models.session_scope(session_factory) as dbsession:
            save_nodes(nodes, dbsession)
            save_links(links, dbsession)

        updates_finished = time.monotonic()
        updates_elapsed = updates_finished - poller_finished

        logger.info(
            "Database updates took {:.2f}s ({:.2f}m)",
            updates_elapsed,
            updates_elapsed / 60,
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


def expire_data(dbsession: Session, *, nodes_expire: int, links_expire: int):
    """Update the status of nodes/links that have not been seen recently.

    Args:
        dbsession: SQLAlchemy database session
        nodes_expire: Number of days a node is not seen before marked inactive
        links_expire: Number of days a link is not seen before marked inactive

    """

    inactive_cutoff = datetime.utcnow() - timedelta(days=links_expire)
    count = (
        dbsession.query(Link)
        .filter(
            Link.status == LinkStatus.RECENT,
            Link.last_seen < inactive_cutoff,
        )
        .update({Link.status: LinkStatus.INACTIVE})
    )
    logger.info(
        "Marked {:,d} links inactive that have not been seen since {}",
        count,
        inactive_cutoff,
    )

    inactive_cutoff = datetime.utcnow() - timedelta(days=nodes_expire)
    count = (
        dbsession.query(Node)
        .filter(
            Node.status == NodeStatus.ACTIVE,
            Node.last_seen < inactive_cutoff,
        )
        .update({Node.status: NodeStatus.INACTIVE})
    )
    logger.info(
        "Marked {:,d} nodes inactive that have not been seen since {}",
        count,
        inactive_cutoff,
    )
    return


def save_nodes(nodes: List[SystemInfo], dbsession: Session):
    """Saves node information to the database.

    Looks for existing nodes by WLAN MAC address and name.

    """
    count: DefaultDict[str, int] = defaultdict(int)
    for node in nodes:
        count["total"] += 1
        # check to see if node exists in database by name and WLAN MAC address

        model = get_db_model(dbsession, node)

        if model is None:
            # create new database model
            logger.debug("Saving {} to database", node)
            count["added"] += 1
            model = Node()
            dbsession.add(model)
        else:
            # update database model
            logger.debug("Updating {} in database with {}", model, node)
            count["updated"] += 1

        model.last_seen = datetime.utcnow()
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
    """Get the most recently seen node, optionally marking the others inactive."""
    if len(results) == 0:
        return None

    results = sorted(results, key=attrgetter("last_seen"), reverse=True)
    for model in results[1:]:
        if model.status == NodeStatus.ACTIVE:
            logger.debug("Marking older match inactive: {}", model)
            model.status = NodeStatus.INACTIVE

    return results[0]


def save_links(links: List[LinkInfo], dbsession: Session):
    """Saves link data to the database.

    This implements the bearing/distance functionality in Python
    rather than using SQL triggers,
    thus the MeshMap triggers will need to be deleted/disabled.

    """
    count: DefaultDict[str, int] = defaultdict(int)

    # Downgrade all "current" links to "recent" so that only ones updated are "current"
    dbsession.query(Link).filter(Link.status == LinkStatus.CURRENT).update(
        {Link.status: LinkStatus.RECENT}
    )

    for link in links:
        count["total"] += 1
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
            count["errors"] += 1
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
            count["new"] += 1
            model = Link(source=source, destination=destination)
            dbsession.add(model)
        else:
            count["updated"] += 1

        model.olsr_cost = link.cost
        model.status = LinkStatus.CURRENT
        model.last_seen = datetime.utcnow()

        if (
            source.longitude is None
            or source.latitude is None
            or destination.longitude is None
            or destination.latitude is None
        ):
            count["missing location info"] += 1
            model.distance = None
            model.bearing = None
        else:
            count["location calculated"] += 1
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
