"""Capture historical data and display it in graphs via RRDtool."""

from __future__ import annotations

import enum
from collections.abc import Iterable
from itertools import cycle
from pathlib import Path
from typing import Any

import attr
import pendulum
import rrdtool
import structlog
from structlog.contextvars import bound_contextvars

from .models import Link, Node

logger = structlog.get_logger()

# Archives based on defaults in Munin's `UpdateWorker.pm`
ARCHIVES = [
    "RRA:AVERAGE:0.5:1:576",  # resolution 5 minutes
    "RRA:MIN:0.5:1:576",
    "RRA:MAX:0.5:1:576",
    "RRA:AVERAGE:0.5:6:432",  # 9 days, resolution 30 minutes
    "RRA:MIN:0.5:6:432",
    "RRA:MAX:0.5:6:432",
    "RRA:AVERAGE:0.5:24:540",  # 45 days, resolution 2 hours
    "RRA:MIN:0.5:24:540",
    "RRA:MAX:0.5:24:540",
    "RRA:AVERAGE:0.5:288:450",  # 450 days, resolution 1 day
    "RRA:MIN:0.5:288:450",
    "RRA:MAX:0.5:288:450",
]

# Colors taken from Munin's default colors
COLORS = (
    "#00CC00",  # Green
    "#0066B3",  # Blue
    "#FF8000",  # Orange
    "#FFCC00",  # Dark Yellow
    "#330099",  # Dark Blue
    "#990099",  # Purple
    "#CCFF00",  # Lime
    "#FF0000",  # Red
    "#808080",  # Gray
)


class Period(enum.Enum):
    DAY = enum.auto()
    WEEK = enum.auto()
    MONTH = enum.auto()
    YEAR = enum.auto


@attr.s(auto_attribs=True, slots=True)
class GraphParams:
    period: Period | None = None
    start: pendulum.DateTime | None = None
    end: pendulum.DateTime | None = None
    title: str = ""

    def as_dict(self):
        """Convert parameters to dictionary for `Graph()`."""
        params = {
            "title": self.title,
        }
        if self.period:
            params["start"] = PERIOD_START_MAP[self.period]
        else:
            params["start"] = str(self.start.int_timestamp)
            params["end"] = str(self.end.int_timestamp)
        return params


PERIOD_START_MAP = {
    # Use 400 x RRA step, so that there is 1px per RRA sample
    # (based on Munin's `Graph.pm`)
    Period.DAY: "end-2000m",
    Period.WEEK: "end-12000m",
    Period.MONTH: "end-48000m",
    Period.YEAR: "end-400d",
}


@attr.s(auto_attribs=True)
class HistoricalStats:
    """Class to wrap historical statistics/data functionality.

    This gives an easy way to persist some basic configuration.  It also means
    we can wrap/abstract some of the common functionality.

    """

    data_path: Path

    def update_node_stats(self, node: Node) -> bool:
        # switch to async after testing!
        rrd_file = self._node_filename(node)
        timestamp = node.last_seen.int_timestamp

        with bound_contextvars(rrd_file=rrd_file):
            # create RRD file if it doesn't exist
            if not rrd_file.exists():
                _create_node_rrd_file(rrd_file, start=timestamp)

            values = ":".join(
                _dump(v)
                for v in [
                    node.link_count,
                    len(node.services),
                    node.up_time_seconds,
                    node.load_averages[0] if node.load_averages is not None else None,
                    node.radio_link_count,
                    node.dtd_link_count,
                    node.tunnel_link_count,
                ]
            )
            try:
                rrdtool.update(
                    str(rrd_file),
                    "--template",
                    "link_count:service_count:uptime:load:radio_links:dtd_links:tunnel_links",  # noqa
                    f"{timestamp}:{values}",
                )
            except rrdtool.OperationalError as exc:
                # TODO: detect errors due to new data points and rebuild data file?
                logger.exception(
                    "Failed to update node RRD file", values=values, error=exc
                )
                return False
        return True

    def graph_network_stats(
        self,
        *,
        params: GraphParams,
    ) -> bytes:
        """Graph network poller statistics."""
        rrd_file = self._network_filename()
        colors = cycle(COLORS)
        graph = Graph(
            vertical_label="count",
            **params.as_dict(),
        )

        for ds in ("node_count", "link_count", "error_count"):
            graph.add_summarized_ds(
                definition=f"DEF:{ds}={rrd_file}:{ds}:AVERAGE",
                v_name=ds,
                color=next(colors),
                style="LINE1",
                legend=ds.replace("_count", "s"),
            )

        return graph.render()

    def graph_poller_stats(self, *, params: GraphParams) -> bytes:
        """Graph network info."""
        rrd_file = self._network_filename()
        colors = cycle(COLORS)

        graph = Graph(
            vertical_label="time (seconds)",
            **params.as_dict(),
        )

        for ds in ("poller_time", "total_time"):
            graph.add_summarized_ds(
                definition=f"DEF:{ds}={rrd_file}:{ds}:AVERAGE",
                v_name=ds,
                color=next(colors),
                style="LINE1",
                legend=ds.replace("_time", ""),
            )
        return graph.render()

    def graph_node_uptime(
        self,
        node: Node,
        *,
        params: GraphParams,
    ) -> bytes:
        """Graph node uptime."""
        rrd_file = self._node_filename(node)
        graph = Graph(
            vertical_label="uptime in days",
            lower_bound=0,
            **params.as_dict(),
        )
        graph.add_summarized_ds(
            definition=f"DEF:uptime={rrd_file!s}:uptime:AVERAGE",
            calculation="CDEF:days=uptime,86400,/",
            v_name="days",
            color="#33cc33",
            style="AREA",
            legend="uptime",
        )
        return graph.render()

    def graph_node_load(
        self,
        node: Node,
        *,
        params: GraphParams,
    ) -> bytes:
        """Graph node uptime."""
        rrd_file = self._node_filename(node)
        graph = Graph(
            vertical_label="load",
            lower_bound=0,
            **params.as_dict(),
        )
        graph.options.extend(("-X", "0"))
        graph.add_summarized_ds(
            definition=f"DEF:load={rrd_file!s}:load:AVERAGE",
            v_name="load",
            color="#33cc33",
            style="LINE1",
            legend="load",
        )
        return graph.render()

    def graph_node_links(
        self,
        node: Node,
        *,
        params: GraphParams,
    ) -> bytes:
        """Graph node links."""
        rrd_file = self._node_filename(node)
        colors = cycle(COLORS)
        graph = Graph(
            vertical_label="count",
            **params.as_dict(),
        )
        graph.add_summarized_ds(
            definition=f"DEF:total={rrd_file!s}:link_count:AVERAGE",
            v_name="total",
            color=next(colors),
            style="LINE1",
        )
        graph.add_summarized_ds(
            definition=f"DEF:radio={rrd_file!s}:radio_links:AVERAGE",
            v_name="radio",
            color=next(colors),
            style="LINE1",
        )
        graph.add_summarized_ds(
            definition=f"DEF:dtd={rrd_file!s}:dtd_links:AVERAGE",
            v_name="dtd",
            color=next(colors),
            style="LINE1",
        )
        graph.add_summarized_ds(
            definition=f"DEF:tunnel={rrd_file!s}:tunnel_links:AVERAGE",
            v_name="tunnel",
            color=next(colors),
            style="LINE1",
        )

        return graph.render()

    def graph_link_cost(
        self,
        link: Link,
        *,
        params: GraphParams,
    ) -> bytes:
        """Graph link routing cost."""

        rrd_file = self._link_filename(link)
        graph = Graph(
            vertical_label="cost",
            **params.as_dict(),
        )
        graph.add_summarized_ds(
            definition=f"DEF:cost={rrd_file!s}:olsr_cost:AVERAGE",
            v_name="cost",
            color=COLORS[1],
            style="LINE1",
            legend="route cost",
        )
        return graph.render()

    def graph_link_snr(
        self,
        link: Link,
        *,
        params: GraphParams,
    ) -> bytes:
        """Graph node uptime."""
        # TODO: add a black line at 0 so it stands out
        rrd_file = self._link_filename(link)
        graph = Graph(
            vertical_label="db",
            **params.as_dict(),
        )
        graph.add_summarized_ds(
            definitions=(
                f"DEF:signal={rrd_file!s}:signal:AVERAGE",
                f"DEF:noise={rrd_file!s}:noise:AVERAGE",
            ),
            calculation="CDEF:snr=signal,noise,-",
            v_name="snr",
            color=COLORS[0],
            style="LINE1",
            legend="snr",
        )
        graph.add_summarized_ds(
            v_name="signal",
            color=COLORS[1],
            style="LINE1",
            legend="signal",
        )
        graph.add_summarized_ds(
            v_name="noise",
            color=COLORS[2],
            style="LINE1",
            legend="noise",
        )
        return graph.render()

    def graph_link_quality(
        self,
        link: Link,
        *,
        params: GraphParams,
    ) -> bytes:
        """Graph link quality and neighbor quality."""

        rrd_file = self._link_filename(link)
        graph = Graph(
            vertical_label="percent",
            **params.as_dict(),
        )
        graph.add_summarized_ds(
            definition=f"DEF:quality={rrd_file!s}:quality:AVERAGE",
            calculation="CDEF:lq=quality,100,*",
            v_name="lq",
            color=COLORS[0],
            style="LINE1",
            legend="local",
        )
        graph.add_summarized_ds(
            definition=f"DEF:neighbor_quality={rrd_file!s}:neighbor_quality:AVERAGE",
            calculation="CDEF:nlq=neighbor_quality,100,*",
            v_name="nlq",
            color=COLORS[1],
            style="LINE1",
            legend="neighbor",
        )
        return graph.render()

    def update_link_stats(self, link: Link) -> bool:
        rrd_file = self._link_filename(link)
        timestamp = link.last_seen.int_timestamp

        with bound_contextvars(rrd_file=rrd_file):
            # create RRD file if it doesn't exist
            if not rrd_file.exists():
                _create_link_rrd_file(rrd_file, start=timestamp)

            values = ":".join(
                _dump(v)
                for v in [
                    link.olsr_cost,
                    link.signal,
                    link.noise,
                    link.tx_rate,
                    link.rx_rate,
                    link.quality,
                    link.neighbor_quality,
                ]
            )
            try:
                rrdtool.update(
                    str(rrd_file),
                    "--template",
                    "olsr_cost:signal:noise:tx_rate:rx_rate:quality:neighbor_quality",
                    f"{timestamp}:{values}",
                )
            except rrdtool.OperationalError as exc:
                # TODO: detect errors due to new data points and rebuild data file?
                logger.exception(
                    "Failed to update link RRD file", value=values, error=exc
                )
                return False
        return True

    def update_network_stats(
        self,
        *,
        node_count: int,
        link_count: int,
        error_count: int,
        poller_time: float,
        total_time: float,
    ) -> bool:
        rrd_file = self._network_filename()
        timestamp = pendulum.now().int_timestamp

        with bound_contextvars(rrd_file=rrd_file):
            # create RRD file if it doesn't exist
            if not rrd_file.exists():
                _create_network_rrd_file(rrd_file, start=timestamp)
            values = ":".join(
                _dump(v)
                for v in [
                    node_count,
                    link_count,
                    error_count,
                    poller_time,
                    total_time,
                ]
            )
            try:
                rrdtool.update(
                    str(rrd_file),
                    "--template",
                    "node_count:link_count:error_count:poller_time:total_time",
                    f"{timestamp}:{values}",
                )
            except rrdtool.OperationalError as exc:
                # TODO: detect errors due to new data points and rebuild data file?
                logger.exception(
                    "Failed to update network RRD file", values=values, error=exc
                )
                return False
        return True

    def delete_node_data(self, node: Node):
        """Delete node data file."""
        self._node_filename(node).unlink()

    def delete_link_data(self, link: Link):
        """Delete link data file."""
        self._link_filename(link).unlink()

    def _network_filename(self) -> Path:
        return self.data_path / "network.rrd"

    def _node_filename(self, node: Node) -> Path:
        return self.data_path / f"node-{node.id}.rrd"

    def _link_filename(self, link: Link) -> Path:
        return self.data_path / f"link-{link.id.dump()}.rrd"


def _create_node_rrd_file(filename: Path, *, start: int | None = None) -> bool:
    """Create RRD file to track node statistics."""

    args = [str(filename)]
    if start:
        args.extend(("--start", str(start - 10)))
    args.extend(
        [
            "--step",
            "300",
            "DS:link_count:GAUGE:600:0:U",
            "DS:service_count:GAUGE:600:0:U",
            "DS:uptime:GAUGE:600:0:U",
            "DS:load:GAUGE:600:0:U",
            "DS:radio_links:GAUGE:600:0:U",
            "DS:dtd_links:GAUGE:600:0:U",
            "DS:tunnel_links:GAUGE:600:0:U",
        ]
    )
    args.extend(ARCHIVES)
    try:
        rrdtool.create(*args)
    except rrdtool.OperationalError as exc:
        logger.exception("Failed to create node RRD file", error=exc)
        return False
    return True


def _create_link_rrd_file(filename: Path, *, start: int | None = None) -> bool:
    """Create RRD file to track link statistics."""

    args = [str(filename)]
    if start:
        args.extend(("--start", str(start - 10)))
    args.extend(
        [
            "--step",
            "300",
            "DS:olsr_cost:GAUGE:600:0:U",
            "DS:signal:GAUGE:600:U:0",
            "DS:noise:GAUGE:600:U:0",
            "DS:tx_rate:GAUGE:600:0:U",
            "DS:rx_rate:GAUGE:600:0:U",
            "DS:quality:GAUGE:600:0:1",
            "DS:neighbor_quality:GAUGE:600:0:1",
        ]
    )
    args.extend(ARCHIVES)
    try:
        rrdtool.create(*args)
    except rrdtool.OperationalError as exc:
        logger.exception("Failed to create link RRD file", error=exc)
        return False
    return True


def _create_network_rrd_file(filename: Path, *, start: int | None = None) -> bool:
    """Create RRD file to track network and poller statistics."""

    args = [str(filename)]
    if start:
        args.extend(("--start", str(start - 10)))
    args.extend(
        [
            "--step",
            "300",
            "DS:node_count:GAUGE:600:0:U",
            "DS:link_count:GAUGE:600:0:U",
            "DS:error_count:GAUGE:600:0:U",
            "DS:poller_time:GAUGE:600:0:U",
            "DS:total_time:GAUGE:600:0:U",
        ]
    )
    args.extend(ARCHIVES)
    try:
        rrdtool.create(*args)
    except rrdtool.OperationalError as exc:
        logger.exception("Failed to create network RRD file", error=exc)
        return False
    return True


def _dump(value: Any) -> str:
    """Dump values for RRDtool."""

    if isinstance(value, str):
        return value
    elif value is None:
        return "U"
    else:
        return str(value)


@attr.s(auto_attribs=True)
class Graph:
    """Helper class to simplify some common stuff when creating a graph."""

    start: str
    end: str = ""
    title: str = ""
    vertical_label: str = ""
    lower_bound: float | None = None
    options: list[str] = attr.Factory(list)
    data_definitions: list[str] = attr.Factory(list)
    data_calculations: list[str] = attr.Factory(list)
    variable_definitions: list[str] = attr.Factory(list)
    elements: list[str] = attr.Factory(list)

    _common_stats = {
        "lst": "LAST",
        "min": "MINIMUM",
        "avg": "AVERAGE",
        "max": "MAXIMUM",
    }

    def add_summarized_ds(
        self,
        *,
        v_name: str,
        color: str,
        style: str,
        definition: str = "",
        definitions: Iterable[str] | None = None,
        calculation: str = "",
        legend: str = "",
        fmt: str = "%10.2lf",
    ):
        """Add a data source to graph and print some basic stats."""
        if legend == "":
            legend = v_name
        if definition:
            self.data_definitions.append(definition)
        if definitions:
            self.data_definitions.extend(definitions)
        if calculation:
            self.data_calculations.append(calculation)
        self.variable_definitions.extend(
            f"VDEF:{stat}_{v_name}={v_name},{func}"
            for stat, func in self._common_stats.items()
        )
        if len(self.elements) == 0:
            self.elements.append(
                # FIXME: base the columns on common stats
                r"COMMENT:{:14s}{:>12s}{:>12s}{:>12s}{:>12s}\l".format(
                    "", "Last", "Minimum", "Average", "Maximum"
                )
            )
        self.elements.append(f"{style}:{v_name}{color}:{legend:12s}")
        self.elements.extend(
            f"GPRINT:{stat}_{v_name}:{fmt}" for stat in self._common_stats.keys()
        )
        self.elements[-1] += r"\l"

    def render(self) -> bytes:
        """Draw the graph via RRDtool."""

        self.options.extend(("--start", self.start))
        if self.end:
            self.options.extend(("--end", self.end))
        if self.vertical_label:
            self.options.extend(("--vertical-label", self.vertical_label))
        if self.title:
            self.options.extend(("--title", self.title))
        if self.lower_bound is not None:
            self.options.extend(("--lower-limit", str(self.lower_bound)))
        graphv_args = (
            "-",
            "--width",
            "400",
            "--height",
            "175",
            *self.options,
            *self.data_definitions,
            *self.data_calculations,
            *self.variable_definitions,
            *self.elements,
        )
        graph_info = rrdtool.graphv(*graphv_args)
        return graph_info["image"]
