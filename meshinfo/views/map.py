"""Views for creating the interactive network map."""

from __future__ import annotations

from collections.abc import Iterator, Sequence

import attrs
from pyramid.request import Request
from pyramid.view import view_config
from sqlalchemy import sql
from sqlalchemy.orm import Session, aliased

from ..config import AppConfig
from ..models import Link, Node
from ..types import Band, LinkId, LinkStatus, LinkType, NodeStatus

LINK_COLORS = {
    "good": "#006164",
    "ok": "#57c4ad",
    "weak": "#edc656",
    "poor": "#eda247",
    "bad": "#db4325",
}


@attrs.define
class NodeLayer:
    key: str
    description: str
    band: Band
    icon: str
    features: list[GeoNode] = attrs.field(factory=list, init=False)

    def __json__(self, request: Request) -> dict:
        return {
            "key": self.key,
            "description": self.description,
            "band": self.band.value,
            "icon": request.static_url(f"meshinfo:static/img/map/{self.icon}"),
            "geoJSON": {"type": "FeatureCollection", "features": self.features},
        }


@attrs.define
class LinkLayer:
    key: str
    description: str
    type: LinkType | LinkStatus
    active: bool = True
    features: list[GeoLink] = attrs.field(factory=list, init=False)

    def __json__(self, request: Request) -> dict:
        return {
            "key": self.key,
            "description": self.description,
            "active": self.active,
            "geoJSON": {"type": "FeatureCollection", "features": self.features},
        }


# map legend uses the order of the bands here
_NODE_LAYERS = (
    NodeLayer("fiveGHzNodes", "5 GHz Nodes", Band.FIVE_GHZ, "gold-radio-small.png"),
    NodeLayer("threeGHzNodes", "3 GHz Nodes", Band.THREE_GHZ, "blue-radio-small.png"),
    NodeLayer("twoGHzNodes", "2 GHz Nodes", Band.TWO_GHZ, "purple-radio-small.png"),
    NodeLayer(
        "nineHundredMHzNodes",
        "900 MHz Nodes",
        Band.NINE_HUNDRED_MHZ,
        "magenta-radio-small.png",
    ),
    NodeLayer("noRFNodes", "No RF Nodes", Band.OFF, "grey-radio-small.png"),
    # TODO: use a red icon
    NodeLayer("unknownNodes", "Unknown Nodes", Band.UNKNOWN, "red-radio-small.png"),
)

_NODE_BAND_LAYER_MAP = {layer.band: layer for layer in _NODE_LAYERS}

_LINK_LAYERS = (
    LinkLayer("rfLinks", "Radio Links", LinkType.RF),
    LinkLayer("dtdLinks", "DTD Links", LinkType.DTD),
    LinkLayer("wireguardLinks", "Wireguard Links", LinkType.WIREGUARD),
    LinkLayer("tunnelLinks", "Tunnel Links", LinkType.TUN),
    LinkLayer("unknownLinks", "Unknown Links", LinkType.UNKNOWN),
    LinkLayer("recentLinks", "Recent Links", LinkStatus.RECENT, active=False),
)

_LINK_TYPE_LAYER_MAP = {layer.type: layer for layer in _LINK_LAYERS}


@attrs.define
class GeoNode:
    """Node data for rendering to GeoJSON."""

    id: int
    name: str
    band: Band
    latitude: float | None
    longitude: float | None
    layer: NodeLayer

    @classmethod
    def from_model(cls, node: Node) -> GeoNode:
        return cls(
            id=node.id,
            name=node.name,
            band=node.band,
            latitude=node.latitude,
            longitude=node.longitude,
            layer=_NODE_BAND_LAYER_MAP[node.band],
        )

    def __json__(self, request: Request):
        """Called by Pyramid's JSON renderer to dump the object to JSON."""
        return {
            "type": "Feature",
            "geometry": {
                "type": "Point",
                # GeoJSON coordinates are "backwards"
                "coordinates": [self.longitude, self.latitude],
            },
            "properties": {
                "id": str(self.id),
                "name": self.name,
                "band": self.band.value,
                "previewUrl": request.route_url("node-preview", id=self.id),
            },
        }


@attrs.define
class GeoLink:
    """Link data for rendering to GeoJSON."""

    id: LinkId
    name: str
    type: LinkType
    status: LinkStatus
    cost: float
    start_latitude: float | None
    start_longitude: float | None
    end_latitude: float | None
    end_longitude: float | None
    layer: LinkLayer

    @property
    def color(self) -> str:
        if self.type == LinkType.DTD:
            return "#3388ff"
        if self.type in {LinkType.TUN, LinkType.WIREGUARD}:
            return "#707070"
        if self.cost is None:
            # unknown link cost
            return "#8b0000"
        if self.cost >= 99.99:
            # infinite link cost
            return "#000000"
        if self.cost > 5:
            return LINK_COLORS["bad"]
        if self.cost > 4:
            return LINK_COLORS["poor"]
        if self.cost > 3:
            return LINK_COLORS["weak"]
        if self.cost > 2:
            return LINK_COLORS["ok"]
        return LINK_COLORS["good"]

    @property
    def opacity(self) -> float:
        opacity = 1.0 if self.status == LinkStatus.CURRENT else 0.2
        return opacity

    @property
    def offset(self) -> int:
        if self.type == LinkType.RF:
            return 2
        return 0

    @property
    def dash_array(self) -> str | None:
        if self.cost is not None and self.cost >= 99.99:
            # infinite link cost
            return "4"
        return None

    @classmethod
    def from_model(cls, link: Link) -> GeoLink:
        if link.status == LinkStatus.CURRENT:
            layer = _LINK_TYPE_LAYER_MAP[link.type]
        else:
            layer = _LINK_TYPE_LAYER_MAP[LinkStatus.RECENT]
        return cls(
            id=link.id,
            name=f"{link.source.name} / {link.destination.name} ({link.type})",
            type=link.type,
            status=link.status,
            cost=link.olsr_cost,
            start_latitude=link.source.latitude,
            start_longitude=link.source.longitude,
            end_latitude=link.destination.latitude,
            end_longitude=link.destination.longitude,
            layer=layer,
        )

    def __json__(self, request: Request):
        """Called by Pyramid's JSON renderer to dump the object to JSON."""
        return {
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                # GeoJSON coordinates are "backwards"
                "coordinates": [
                    [self.start_longitude, self.start_latitude],
                    [self.end_longitude, self.end_latitude],
                ],
            },
            "properties": {
                "id": self.id.dump(),
                "name": self.name,
                "type": self.type.name,
                "color": self.color,
                "weight": 2,
                "offset": self.offset,
                "opacity": self.opacity,
                "dashArray": self.dash_array,
                "previewUrl": request.route_url(
                    "link-preview",
                    source=self.id.source,
                    destination=self.id.destination,
                    type=self.type.name.lower(),
                ),
            },
        }


@view_config(route_name="map", renderer="pages/map.jinja2")
def network_map(request: Request):
    """Network map view - basic page to load/define the necessary Javascript.

    Network data is then loaded via Javascript from the `map-data` view.

    """
    # TODO: read starting coordinates/zoom from query string
    # (and/or use https://github.com/mlevans/leaflet-hash)

    config: AppConfig = request.registry.settings["app_config"]

    node_icons = {
        layer.band: request.static_url(f"meshinfo:static/img/map/{layer.icon}")
        for layer in _NODE_LAYERS
    }

    link_colors = {
        "1-2": LINK_COLORS["good"],
        "2-3": LINK_COLORS["ok"],
        "3-4": LINK_COLORS["weak"],
        "4-5": LINK_COLORS["poor"],
        "5+": LINK_COLORS["bad"],
    }

    return {
        "node_icons": node_icons,
        "latitude": config.map.latitude,
        "longitude": config.map.longitude,
        "zoom": config.map.zoom,
        "max_zoom": config.map.max_zoom,
        "tile_url": config.map.tile_url,
        "tile_attribution": config.map.tile_attribution,
        "link_colors": link_colors,
    }


@view_config(route_name="map-data", renderer="json")
def map_data(request: Request):
    """Generate node and link data as GeoJSON to be loaded into Leaflet."""
    dbsession: Session = request.dbsession
    node_query = sql.select(Node).where(
        Node.status != NodeStatus.INACTIVE,
        Node.latitude != sql.null(),
        Node.longitude != sql.null(),
    )
    source_nodes = aliased(Node, node_query.subquery())
    dest_nodes = aliased(Node, node_query.subquery())

    nodes = dbsession.scalars(node_query).all()

    links = dbsession.scalars(
        sql.select(Link)
        .join(source_nodes, Link.source_id == source_nodes.id)
        .join(dest_nodes, Link.destination_id == dest_nodes.id)
        .where(Link.status != LinkStatus.INACTIVE)
    ).all()

    node_layers = {layer.key: layer for layer in _NODE_LAYERS}
    link_layers = {layer.key: layer for layer in _LINK_LAYERS}
    for node in (GeoNode.from_model(node) for node in nodes):
        node_layers[node.layer.key].features.append(node)
    for link in (GeoLink.from_model(link) for link in _dedupe_links(links)):
        link_layers[link.layer.key].features.append(link)

    # return only the layers with features in them
    return {
        "nodeLayers": [
            layer for layer in node_layers.values() if len(layer.features) > 0
        ],
        "linkLayers": [
            layer for layer in link_layers.values() if len(layer.features) > 0
        ],
    }


def _calc_hue(value: float, *, red: float, green: float) -> int:
    """Calculate the hue between red and green, with the median being yellow."""
    range_ = abs(green - red)
    if red < green:
        percent = max(min(value, green) - red, 0) / range_
    else:
        percent = 1 - max(min(value, red) - green, 0) / range_

    # red hue is 0, green is 120, so just multiply the percentage by 120
    return round(120 * percent)


def _dedupe_links(links: Sequence[Link]) -> Iterator[Link]:
    """Filter out redundant tunnels and DTD links."""
    # while it is unlikely that two nodes are connected by both types, this is safer
    seen_tunnels = set()
    seen_dtd = set()
    seen_wireguard = set()

    for link in links:
        if link.type not in {LinkType.DTD, LinkType.TUN, LinkType.WIREGUARD}:
            yield link
            continue
        # reverse the nodes to see if the mirror version was returned
        link_nodes = (link.destination_id, link.source_id)
        if link.type == LinkType.DTD and link_nodes not in seen_dtd:
            seen_dtd.add((link.source_id, link.destination_id))
            yield link
        elif link.type == LinkType.TUN and link_nodes not in seen_tunnels:
            seen_tunnels.add((link.source_id, link.destination_id))
            yield link
        elif link.type == LinkType.WIREGUARD and link_nodes not in seen_wireguard:
            seen_wireguard.add((link.source_id, link.destination_id))
            yield link
