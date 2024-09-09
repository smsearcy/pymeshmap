from __future__ import annotations

import re
from collections import defaultdict

from pyramid.request import Request
from pyramid.view import view_config
from sqlalchemy import sql
from sqlalchemy.orm import Session

from ..models import CollectorStat, Link, Node, NodeError
from ..types import LinkStatus, NodeStatus


@view_config(route_name="home", renderer="pages/home.jinja2")
def overview(request: Request):
    dbsession: Session = request.dbsession

    node_count = dbsession.scalar(
        sql.select(sql.func.count())
        .select_from(Node)
        .where(Node.status != NodeStatus.INACTIVE)
    )
    link_count = dbsession.scalar(
        sql.select(sql.func.count())
        .select_from(Node)
        .where(Link.status != LinkStatus.INACTIVE)
    )

    # Get node counts by firmware version
    firmware_results = dbsession.execute(
        sql.select(
            Node.firmware_manufacturer, Node.firmware_version, sql.func.count(Node.id)
        )
        .where(Node.status == NodeStatus.ACTIVE)
        .group_by(Node.firmware_manufacturer, Node.firmware_version)
    )
    firmware_stats: defaultdict[str, int] = defaultdict(int)
    for manufacturer, version, count in firmware_results:
        if manufacturer.lower() != "aredn":
            firmware_stats["Non-AREDN"] += 1
        elif re.match(r"\d+\.\d+\.\d+\.\d+", version):
            firmware_stats[version] = count
        else:
            firmware_stats["Nightly"] += count

    # Get node counts by API version
    api_results = dbsession.execute(
        sql.select(Node.api_version, sql.func.count(Node.id))
        .where(Node.status == NodeStatus.ACTIVE)
        .group_by(Node.api_version)
    )
    api_version_stats = {version: count for version, count in api_results}

    # Get node counts by band
    band_results = dbsession.execute(
        sql.select(Node.band, sql.func.count(Node.id))
        .where(Node.status == NodeStatus.ACTIVE)
        .group_by(Node.band)
    )
    band_stats = {band: count for band, count in band_results}

    last_run = dbsession.scalars(
        sql.select(CollectorStat).order_by(sql.desc(CollectorStat.started_at)).limit(1)
    ).first()

    node_errors_by_type: dict[str, list[NodeError]] = {}
    if last_run:
        errors = dbsession.scalars(
            sql.select(NodeError).where(NodeError.timestamp == last_run.started_at)
        )
        for error in errors:
            node_errors_by_type.setdefault(str(error.error_type), []).append(error)

    return {
        "api_stats": api_version_stats,
        "band_stats": band_stats,
        "firmware_stats": firmware_stats,
        "last_run": last_run,
        "link_count": link_count,
        "node_count": node_count,
        "node_errors": node_errors_by_type,
    }
