from pyramid.httpexceptions import HTTPNotFound
from pyramid.request import Request
from pyramid.response import Response
from pyramid.settings import asbool
from pyramid.view import view_config, view_defaults
from sqlalchemy.orm import Session, joinedload, load_only

from ..historical import HistoricalStats
from ..models import Link, Node
from ..types import LinkType
from . import schema


@view_config(
    route_name="link-preview",
    renderer="components/link-preview.jinja2",
    http_cache=120,
)
def link_preview(request: Request):
    """Link snippet/preview for map pop-ups."""

    source_id = int(request.matchdict["source"])
    destination_id = int(request.matchdict["destination"])
    type_ = getattr(LinkType, request.matchdict["type"].upper())
    dbsession: Session = request.dbsession

    link = dbsession.get(
        Link,
        (source_id, destination_id, type_),
        options=[
            joinedload(Link.source).load_only(Node.name),
            joinedload(Link.destination).load_only(Node.name),
        ],
    )
    if link is None:
        raise HTTPNotFound("Sorry, the specified link could not be found")

    # TODO: add tabs with graph(s) to the link preview
    # py310: use match operator
    if link.type == LinkType.RF:
        graph = "snr"
    elif link.type in {LinkType.DTD, LinkType.TUN}:
        graph = "quality"
    else:
        graph = "cost"

    return {
        "link": link,
        "graph": graph,
    }


@view_config(route_name="link-graphs", renderer="pages/link-graphs.jinja2")
def link_graphs(request: Request):
    """Page displaying graphs of particular data over different timeframes."""

    source_id = int(request.matchdict["source"])
    destination_id = int(request.matchdict["destination"])
    type_ = getattr(LinkType, request.matchdict["type"].upper())
    dbsession: Session = request.dbsession
    graph = request.matchdict["name"]

    link = dbsession.get(
        Link,
        (source_id, destination_id, type_),
        options=[
            load_only(Link.source_id, Link.destination_id),
            joinedload(Link.destination).load_only(Node.name),
        ],
    )
    if link is None:
        raise HTTPNotFound("Sorry, the specified link could not be found")

    return {
        "link": link,
        "graph": graph,
        "graph_name": graph.title(),  # use a dictionary for more control of the name?
    }


@view_defaults(route_name="link-graph", http_cache=120)
class LinkGraphs:
    """Generate graph image of link data."""

    def __init__(self, request: Request):
        source_id = int(request.matchdict["source"])
        destination_id = int(request.matchdict["destination"])
        type_ = getattr(LinkType, request.matchdict["type"].upper())
        dbsession: Session = request.dbsession

        link = dbsession.get(
            Link,
            (source_id, destination_id, type_),
            options=[
                load_only(Link.source_id, Link.destination_id, Link.type),
                joinedload(Link.destination).load_only(Node.name),
            ],
        )
        if link is None:
            raise HTTPNotFound("Sorry, the specified link could not be found")

        self.link = link
        self.name_in_title = asbool(request.GET.get("name_in_title", False))
        self.graph_params = schema.graph_params(request.GET)
        self.stats: HistoricalStats = request.find_service(HistoricalStats)

    @view_config(match_param="name=cost")
    def cost_graph(self):
        title_parts = (
            self.link.destination.name.lower() if self.name_in_title else "",
            "route cost",
            self.graph_params.title,
        )
        self.graph_params.title = " - ".join(part for part in title_parts if part)
        return Response(
            self.stats.graph_link_cost(self.link, params=self.graph_params),
            status="200 OK",
            content_type="image/png",
        )

    @view_config(match_param="name=snr")
    def snr_graph(self):
        title_parts = (
            self.link.destination.name.lower() if self.name_in_title else "",
            "snr",
            self.graph_params.title,
        )
        self.graph_params.title = " - ".join(part for part in title_parts if part)
        return Response(
            self.stats.graph_link_snr(self.link, params=self.graph_params),
            status="200 OK",
            content_type="image/png",
        )

    @view_config(match_param="name=quality")
    def quality_graph(self):
        title_parts = (
            self.link.destination.name.lower() if self.name_in_title else "",
            "link quality",
            self.graph_params.title,
        )
        self.graph_params.title = " - ".join(part for part in title_parts if part)
        return Response(
            self.stats.graph_link_quality(self.link, params=self.graph_params),
            status="200 OK",
            content_type="image/png",
        )
