from operator import attrgetter
from typing import Any

from pyramid.httpexceptions import HTTPNotFound
from pyramid.request import Request
from pyramid.response import Response
from pyramid.settings import asbool
from pyramid.view import view_config, view_defaults
from sqlalchemy import sql
from sqlalchemy.orm import Session, joinedload, load_only

from ..aredn import LinkType, VersionChecker
from ..historical import HistoricalStats
from ..models import Link, Node
from ..types import LinkStatus
from . import schema


@view_config(route_name="node-details", renderer="pages/node-details.jinja2")
def node_detail(request: Request):
    """Detailed view of a single node."""

    node_id = int(request.matchdict["id"])
    dbsession: Session = request.dbsession
    version_checker: VersionChecker = request.find_service(VersionChecker)

    node = dbsession.get(Node, node_id)

    if node is None:
        raise HTTPNotFound("Sorry, the specified node could not be found")

    firmware_status = version_checker.firmware(node.firmware_version)
    api_status = version_checker.api(node.api_version)

    stmt = (
        sql.select(Link)
        .options(joinedload(Link.destination).load_only(Node.display_name))
        .filter(
            Link.source_id == node.id,
            Link.status != LinkStatus.INACTIVE,
        )
    )
    links = dbsession.execute(stmt).scalars()

    graphs_by_link_type = {
        LinkType.RF: ("cost", "quality", "snr"),
        LinkType.DTD: ("cost", "quality"),
        # do tunnels have quality metrics?
        LinkType.TUN: ("cost", "quality"),
        LinkType.UNKNOWN: ("cost",),
    }

    return {
        "node": node,
        "links": links,
        "firmware_status": firmware_status,
        "api_status": api_status,
        "link_graphs": graphs_by_link_type,
    }


@view_config(route_name="node-json", renderer="json")
def node_json(request: Request) -> dict:
    """Dump most recent sysinfo.json for a node."""

    node_id = int(request.matchdict["id"])
    dbsession: Session = request.dbsession

    if not (node := dbsession.get(Node, node_id)):
        raise HTTPNotFound("Sorry, the specified node could not be found")

    return node.system_info


@view_config(
    route_name="node-preview",
    renderer="components/node-preview.jinja2",
    http_cache=120,
)
def node_preview(request: Request):
    """Node preview for map pop-ups."""

    node_id = int(request.matchdict["id"])
    dbsession: Session = request.dbsession

    if not (node := dbsession.get(Node, node_id)):
        raise HTTPNotFound("Sorry, the specified node could not be found")

    current_links = dbsession.scalars(
        sql.select(Link)
        .options(joinedload(Link.destination).load_only(Node.display_name))
        .where(
            Link.source_id == node.id,
            Link.status == LinkStatus.CURRENT,
        )
    ).all()
    recent_links = dbsession.scalars(
        sql.select(Link)
        .options(joinedload(Link.destination).load_only(Node.display_name))
        .where(
            Link.source_id == node.id,
            Link.status == LinkStatus.RECENT,
        )
    ).all()

    return {
        "node": node,
        "current_links": sorted(
            current_links, key=attrgetter("destination.display_name")
        ),
        "recent_links": sorted(
            recent_links, key=attrgetter("destination.display_name")
        ),
    }


@view_config(route_name="node-graphs", renderer="pages/node-graphs.jinja2")
def node_graphs(request: Request) -> dict[str, Any]:
    """Display graphs of particular data for a node over different timeframes."""

    node_id = int(request.matchdict["id"])
    graph = request.matchdict["name"]
    dbsession: Session = request.dbsession

    node = dbsession.get(Node, node_id, options=[load_only(Node.display_name, Node.id)])

    return {
        "node": node,
        "graph": graph,
        "graph_name": graph.title(),  # use a dictionary for more control of the name?
    }


@view_defaults(route_name="node-graph", http_cache=120)
class NodeGraphs:
    """Generate graph image of node data."""

    def __init__(self, request: Request):
        node_id = int(request.matchdict["id"])
        dbsession: Session = request.dbsession

        if not (
            node := dbsession.get(
                Node, node_id, options=[load_only(Node.id, Node.name)]
            )
        ):
            raise HTTPNotFound("Sorry, the specified node could not be found")
        self.node = node

        self.graph_params = schema.graph_params(request.GET)
        self.name_in_title = asbool(request.GET.get("name_in_title", False))

        self.stats: HistoricalStats = request.find_service(HistoricalStats)

    @view_config(match_param="name=links")
    def links(self) -> Response:
        title_parts = (
            self.node.name.lower() if self.name_in_title else "",
            "links",
            self.graph_params.title,
        )
        self.graph_params.title = " - ".join(part for part in title_parts if part)
        return Response(
            self.stats.graph_node_links(self.node, params=self.graph_params),
            status="200 OK",
            content_type="image/png",
        )

    @view_config(match_param="name=load")
    def load(self) -> Response:
        title_parts = (
            self.node.name.lower() if self.name_in_title else "",
            "load",
            self.graph_params.title,
        )
        self.graph_params.title = " - ".join(part for part in title_parts if part)
        return Response(
            self.stats.graph_node_load(self.node, params=self.graph_params),
            status="200 OK",
            content_type="image/png",
        )

    @view_config(match_param="name=uptime")
    def uptime(self) -> Response:
        title_parts = (
            self.node.name.lower() if self.name_in_title else "",
            "uptime",
            self.graph_params.title,
        )
        self.graph_params.title = " - ".join(part for part in title_parts if part)
        return Response(
            self.stats.graph_node_uptime(self.node, params=self.graph_params),
            status="200 OK",
            content_type="image/png",
        )
