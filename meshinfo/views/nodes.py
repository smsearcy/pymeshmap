import csv
import io
from operator import attrgetter

from pyramid.request import Request, Response
from pyramid.view import view_config, view_defaults
from sqlalchemy import sql
from sqlalchemy.orm import Session

from ..models import Node
from ..types import NodeStatus


@view_defaults(route_name="nodes")
class NodeListViews:
    def __init__(self, request: Request):
        dbsession: Session = request.dbsession

        # TODO: parameters to determine which nodes to return
        nodes = dbsession.scalars(
            sql.select(Node).where(Node.status != NodeStatus.INACTIVE)
        ).all()
        self.nodes: list[Node] = sorted(nodes, key=attrgetter("name"))
        self.request = request

    @view_config(match_param="view=table", renderer="pages/nodes.jinja2")
    def table(self):
        return {"nodes": self.nodes}

    @view_config(match_param="view=csv")
    def csv(self) -> Response:
        output = io.StringIO(newline="")
        csv_out = csv.writer(output)
        csv_out.writerow(
            (
                "Name",
                "IP Address",
                "Status",
                "Band",
                "Channel",
                "Channel Bandwidth",
                "Link Count",
                "Active Tunnel Count",
                "Firmware",
                "API Version",
                "Last Seen",
            )
        )
        for node in self.nodes:
            csv_out.writerow(
                (
                    node.name,
                    node.ip_address,
                    node.status,
                    node.band,
                    node.channel,
                    node.channel_bandwidth,
                    node.link_count,
                    node.active_tunnel_count,
                    node.firmware_version,
                    node.api_version,
                    node.last_seen,
                )
            )

        output.seek(0)
        response: Response = self.request.response
        response.text = output.read()
        response.content_type = "text/csv"
        response.content_disposition = "attachment; filename=node-export.csv"

        return response
