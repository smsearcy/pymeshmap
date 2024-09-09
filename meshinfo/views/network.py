import pendulum
from pyramid.httpexceptions import HTTPBadRequest, HTTPNotFound
from pyramid.request import Request
from pyramid.response import Response
from pyramid.view import view_config, view_defaults
from sqlalchemy import sql
from sqlalchemy.orm import Session, subqueryload

from ..historical import HistoricalStats
from ..models import CollectorStat
from . import schema


@view_config(route_name="network-graphs", renderer="pages/network-graphs.jinja2")
def network_graphs(request: Request):
    """Page displaying graphs of particular data over different timeframes."""
    graph = request.matchdict["name"]
    return {
        "graph": graph,
        "graph_name": graph.title(),  # use a dictionary for more control of the name?
    }


@view_defaults(route_name="network-graph", http_cache=120)
class NetworkGraphs:
    def __init__(self, request: Request):
        self.graph_params = schema.graph_params(request.GET)
        self.stats: HistoricalStats = request.find_service(HistoricalStats)

    @view_config(match_param="name=info")
    def info(self):
        title_parts = (
            "network info",
            self.graph_params.title,
        )
        self.graph_params.title = " - ".join(part for part in title_parts if part)

        return Response(
            self.stats.graph_network_stats(params=self.graph_params),
            status="200 OK",
            content_type="image/png",
        )

    @view_config(match_param="name=poller")
    def poller(self):
        title_parts = (
            "poller stats",
            self.graph_params.title,
        )
        self.graph_params.title = " - ".join(part for part in title_parts if part)

        return Response(
            self.stats.graph_poller_stats(params=self.graph_params),
            status="200 OK",
            content_type="image/png",
        )


@view_config(route_name="network-errors", renderer="pages/network-errors.jinja2")
def network_errors(request: Request):
    dbsession: Session = request.dbsession
    try:
        timestamp = pendulum.from_format(request.matchdict["timestamp"], "X")
    except Exception as exc:
        raise HTTPBadRequest("Invalid timestamp") from exc

    marked_row = request.GET.get("highlight")

    collector = dbsession.execute(
        sql.select(CollectorStat)
        .options(subqueryload(CollectorStat.node_errors))
        .where(CollectorStat.started_at == timestamp)
    ).scalar_one_or_none()
    if collector is None:
        raise HTTPNotFound(f"No collection statistics available for {timestamp}")

    return {
        "marked_ip": marked_row,
        "node_errors": collector.node_errors,
        "stats": collector,
    }
