"""Main module for getting information from nodes on an AREDN mesh network.

Provides `asyncio` functions for crawling the network and polling the nodes.
Defines data classes for modeling the network information independent of the database
models because there might be parsed values that are not ready to be stored yet.

Throughout this module there are references to OLSR (Optimized Link State Routing)
but what is really meant is the OLSR daemon that runs on wireless node in the mesh.

"""

from __future__ import annotations

import asyncio
import enum
import json
import re
import time
from asyncio import Lock, StreamReader, StreamWriter
from collections import abc, defaultdict, deque
from typing import Awaitable, NamedTuple

import aiohttp
import attrs
from loguru import logger

from .aredn import LinkInfo, SystemInfo, load_system_info
from .types import LinkType


class OlsrData:
    """Provides asynchronous iterators with node IPs and links.

    Information is generated by parsing OLSR data.
    The ``nodes`` attribute yields node IP addresses as strings.
    The ``links`` attribute yields ``OlsrLink`` dataclasses.

    """

    NODE_REGEX = re.compile(r"^\"(\d{2}\.\d{1,3}\.\d{1,3}\.\d{1,3})\" -> \"\d+")
    LINK_REGEX = re.compile(
        r"^\"(10\.\d{1,3}\.\d{1,3}\.\d{1,3})\" -> "
        r"\"(10\.\d{1,3}\.\d{1,3}\.\d{1,3})\"\[label=\"(.+?)\"\];"
    )

    class LineGenerator:
        def __init__(self, olsr: OlsrData, lock: Lock):
            self._olsr = olsr
            self._lock = lock
            self.queue: deque[str | OlsrLink] = deque()

        def __aiter__(self):
            return self

        async def __anext__(self):
            if len(self.queue) > 0:
                return self.queue.popleft()

            while len(self.queue) == 0 and not self._olsr.finished:
                async with self._lock:
                    await self._olsr._populate_queues()

            if self._olsr.finished:
                raise StopAsyncIteration()

            return self.queue.popleft()

    def __init__(self, reader: StreamReader, writer: StreamWriter):
        self.reader = reader
        self.writer = writer
        self.finished = False
        olsr_lock = Lock()
        self.nodes: abc.AsyncIterator[str] = self.LineGenerator(self, olsr_lock)
        self.links: abc.AsyncIterator[OlsrLink] = self.LineGenerator(self, olsr_lock)
        self.stats: defaultdict[str, int] = defaultdict(int)
        self._nodes_seen: set[str] = set()
        self._links_seen: set[tuple[str, str]] = set()

    @classmethod
    async def connect(
        cls, host_name: str = "localnode.local.mesh", port: int = 2004, timeout: int = 5
    ) -> OlsrData:
        """Connect to an OLSR daemon and create an `OlsrData` wrapper.

        Args:
            host_name: Name of host to connect to OLSR daemon
            port: Port the OLSR daemon is running on
            timeout: Connection timeout in seconds

        """
        logger.trace("Connecting to OLSR daemon {}:{}", host_name, port)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host_name, port), timeout
            )
        except asyncio.TimeoutError:
            logger.error("Timeout attempting to connect to {}:{}", host_name, port)
            raise RuntimeError("Timeout connecting to OLSR daemon")
        except OSError as e:
            # Connection errors subclass `OSError`
            logger.error("Failed to connect to {}:{} ({!s})", host_name, port, e)
            raise RuntimeError("Failed to connect to OLSR daemon")

        return cls(reader, writer)

    async def _populate_queues(self):
        """Read data from OLSR and store for processing nodes and links."""

        if self.finished:
            return

        line_bytes = await self.reader.readline()
        if not line_bytes:
            # All data from OLSR has been processed
            self.finished = True
            self.writer.close()
            await self.writer.wait_closed()

            logger.info("OLSR Data Statistics: {}", dict(self.stats))
            if self.stats["nodes returned"] == 0:
                logger.warning(
                    "Failed to find any nodes in {:,d} lines of OLSR data.",
                    self.stats["lines processed"],
                )
            if self.stats["links returned"] == 0:
                logger.warning(
                    "Failed to find any links in {:,d} lines of OLSR data.",
                    self.stats["lines processed"],
                )
            return

        # TODO: filter until a useful line is present?
        self.stats["lines processed"] += 1
        line_str = line_bytes.decode("utf-8").rstrip()
        logger.trace("OLSR data: {}", line_str)

        if node_address := self._get_address(line_str):
            self.nodes.queue.append(node_address)
        if link := self._get_link(line_str):
            self.links.queue.append(link)

        return

    def _get_address(self, line: str) -> str:
        """Return the IP address of unique nodes from OLSR data lines.

        Based on `wxc_netcat()` in MeshMap the only lines we are interested in
        (when getting the node list)
        are the ones that look (generally) like this
        (sometimes the second address is a CIDR address):

            "10.32.66.190" -> "10.80.213.95"[label="1.000"];

        """
        match = self.NODE_REGEX.match(line)
        if not match:
            return ""

        node_address = match.group(1)
        if node_address in self._nodes_seen:
            self.stats["duplicate node"] += 1
            return ""
        self._nodes_seen.add(node_address)
        self.stats["nodes returned"] += 1
        return node_address

    def _get_link(self, line: str) -> OlsrLink | None:
        """Return the IP addresses and cost of a link from an OLSR data line.

        Based on `wxc_netcat()` in MeshMap the only lines we are interested in
        (when getting the node list)
        are the ones that look like this:

            "10.32.66.190" -> "10.80.213.95"[label="1.000"];

        Records where the second address is in CIDR notation and the label is "HNA"
        should be excluded via a regular expression for the above.

        """
        match = self.LINK_REGEX.match(line)
        if not match:
            return None

        # apparently there have been issues with duplicate links
        # so track the ones that have been returned
        source_node = match.group(1)
        destination_node = match.group(2)
        label = match.group(3)

        link_id = (source_node, destination_node)
        if link_id in self._links_seen:
            self.stats["duplicate link"] += 1
            return None
        self._links_seen.add(link_id)
        self.stats["links returned"] += 1
        return OlsrLink.from_strings(*link_id, label)


@attrs.define
class Poller:
    max_connections: int
    timeout: aiohttp.ClientTimeout
    lookup_name: abc.Callable[[str], Awaitable[str]]

    @classmethod
    def create(
        cls,
        lookup_name: abc.Callable[[str], Awaitable[str]],
        max_connections: int = 50,
        connect_timeout: int = 10,
        read_timeout: int = 15,
    ) -> Poller:
        """Initialize a `Poller` object."""
        return Poller(
            lookup_name=lookup_name,
            max_connections=max_connections,
            timeout=aiohttp.ClientTimeout(
                sock_connect=connect_timeout,
                sock_read=read_timeout,
            ),
        )

    async def get_network_info(self, olsr_data: OlsrData) -> NetworkInfo:
        """Gets the node and link information about the network."""

        # start processing the nodes
        node_task = asyncio.create_task(self._poll_nodes(olsr_data.nodes))
        # while that's going, process OLSR links
        olsr_links = [link async for link in olsr_data.links]
        logger.info("OLSR link count: {}", len(olsr_links))
        # wait for the nodes to finish processing
        node_results: list[NodeResult] = await node_task

        # make a dictionary for quick lookups of node name from IP address
        # (for cross-referencing with OLSR data)
        ip_name_map = {
            node.ip_address: node.name
            for node in node_results
            if all((node.name, node.ip_address))
        }

        # make list of OLSR links by source IP (for nodes with older firmware)
        olsr_links_by_ip: dict[str, list[OlsrLink]] = {}
        for link in olsr_links:
            olsr_links_by_ip.setdefault(link.source, []).append(link)

        count: defaultdict[str, int] = defaultdict(int)
        # Collect the list of node `SystemInfo` objects to return
        nodes = []
        # Build list of links for all nodes, using AREDN data, falling back to OLSR
        links: list[LinkInfo] = []
        node_errors = []
        for node in node_results:
            count["node results"] += 1
            if node.error:
                count["errors (totals)"] += 1
                count[f"errors ({node.error.error!s})"] += 1
                node_errors.append(node)
                continue

            sys_info = node.system_info
            if sys_info is None:
                logger.error("Node does not have response or error: {}", node)
                continue
            nodes.append(sys_info)
            if len(sys_info.links) > 0:
                # Use link information from AREDN if we have it (newer firmware)
                count["using link_info json"] += 1
                if sys_info.api_version_tuple < (1, 9):
                    # get the link cost from OLSR (pre-v1.9 API)
                    count["using olsr for link cost"] += 1
                    _populate_cost_from_olsr(
                        sys_info.links, olsr_links_by_ip.get(node.ip_address, [])
                    )
                links.extend(sys_info.links)
                sys_info.link_count = len(sys_info.links)
                continue

            # Create `LinkInfo` objects based on the information in OLSR
            # **This should only necessary for firmware < API 1.7**
            count["using olsr for link data"] += 1
            sys_info.link_count = 0
            try:
                node_olsr_links = olsr_links_by_ip[node.ip_address]
            except KeyError:
                logger.warning(
                    "Failed to find OLSR links for {} ({})", sys_info, node.ip_address
                )
                continue
            for link in node_olsr_links:
                sys_info.link_count += 1
                if link.destination not in ip_name_map:
                    logger.warning(
                        "OLSR IP not found in node information, skipping: {}", link
                    )
                    continue
                links.append(
                    LinkInfo(
                        source=sys_info.node_name,
                        destination=ip_name_map[link.destination],
                        destination_ip=link.destination,
                        type=LinkType.UNKNOWN,
                        interface="unknown",
                        olsr_cost=link.cost,
                    )
                )

        logger.info("Network Info Summary: {}", dict(count))

        return NetworkInfo(nodes, links, node_errors)

    async def _poll_nodes(self, addresses: abc.AsyncIterable[str]) -> list[NodeResult]:
        """Get information about all the nodes in the network."""
        start_time = time.monotonic()

        tasks = []
        connector = aiohttp.TCPConnector(limit=self.max_connections)
        async with aiohttp.ClientSession(
            timeout=self.timeout, connector=connector
        ) as session:
            async for address in addresses:
                logger.debug("Creating task to poll {}", address)
                task = asyncio.create_task(self._poll_node(address, session=session))
                tasks.append(task)

            # collect all the results in a single list, dropping any exceptions
            node_results = []
            for result in await asyncio.gather(*tasks, return_exceptions=True):
                if isinstance(result, Exception):
                    logger.error("Unexpected exception polling nodes: {!r}", result)
                    continue
                node_results.append(result)

        crawler_finished = time.monotonic()
        logger.info("Querying nodes took {:.2f} seconds", crawler_finished - start_time)
        return node_results

    async def _poll_node(
        self, ip_address: str, *, session: aiohttp.ClientSession
    ) -> NodeResult:
        """Query a node via HTTP to get the information about that node.

        Args:
            session: aiohttp session object
                (docs recommend to pass around single object)
            ip_address: IP address of the node to query

        Returns:
            Named tuple with the IP address,
            result of either `SystemInfo` or `NodeError`,
            and the raw response string.

        """

        logger.debug("{} begin polling...", ip_address)

        params = {"services_local": 1, "link_info": 1}

        try:
            async with session.get(
                f"http://{ip_address}:8080/cgi-bin/sysinfo.json", params=params
            ) as resp:
                status = resp.status
                response = await resp.read()
                # copy and pasting Unicode seems to create an invalid description
                # example we had was b"\xb0" for a degree symbol
                response_text = response.decode("utf-8", "replace")
        except Exception as exc:
            return await self._handle_connection_error(ip_address, exc)

        if status != 200:
            message = f"{status}: {response_text}"
            return await self._handle_response_error(
                ip_address, PollingError.HTTP_ERROR, message
            )

        try:
            json_data = json.loads(response_text)
        except json.JSONDecodeError as exc:
            return await self._handle_response_error(
                ip_address, PollingError.INVALID_RESPONSE, response_text, exc
            )

        try:
            node_info = load_system_info(json_data)
        except Exception as exc:
            return await self._handle_response_error(
                ip_address, PollingError.PARSE_ERROR, response_text, exc
            )

        logger.success("Finished polling {}", node_info)
        return NodeResult(
            ip_address=ip_address,
            name=node_info.node_name,
            system_info=node_info,
        )

    async def _handle_connection_error(
        self, ip_address: str, exc: Exception
    ) -> NodeResult:
        result = NodeResult(
            ip_address=ip_address,
            name=await self.lookup_name(ip_address),
        )

        # py3.10 - use match operator?
        if isinstance(exc, asyncio.TimeoutError):
            # catch this first, because some exceptions use multiple inheritance
            logger.error("{}: {}", result.label, exc)
            result.error = NodeError(PollingError.TIMEOUT_ERROR, "Timeout error")
        elif isinstance(exc, aiohttp.ClientError):
            logger.error("{}: {}", result.label, exc)
            result.error = NodeError(PollingError.CONNECTION_ERROR, str(exc))
        else:
            logger.error("{}: Unknown error connecting: {!r}", result.label, exc)
            result.error = NodeError(PollingError.CONNECTION_ERROR, str(exc))

        return result

    async def _handle_response_error(
        self, ip_address: str, error: PollingError, response: str, exc: Exception = None
    ) -> NodeResult:
        result = NodeResult(
            ip_address=ip_address,
            name=await self.lookup_name(ip_address),
        )

        # py3.10 - use match operator?
        if error == PollingError.HTTP_ERROR:
            logger.error("{}: HTTP error {}", result.label, response)
            result.error = NodeError(PollingError.HTTP_ERROR, response)
        elif error == PollingError.INVALID_RESPONSE:
            logger.error("{}: Invalid JSON response: {}", result.label, exc)
            result.error = NodeError(PollingError.INVALID_RESPONSE, response)
        elif error == PollingError.PARSE_ERROR:
            logger.error("{}: Parsing node information failed: {}", result.label, exc)
            result.error = NodeError(PollingError.PARSE_ERROR, response)

        return result


def _populate_cost_from_olsr(links: list[LinkInfo], olsr_links: list[OlsrLink]):
    """Populate the link cost from the OLSR data."""
    if len(olsr_links) == 0:
        logger.warning("No OLSR link data found for {}", links[0].source)
        return
    cost_by_destination = {link.destination: link.cost for link in olsr_links}
    for link in links:
        if link.destination_ip not in cost_by_destination:
            continue
        link.olsr_cost = cost_by_destination[link.destination_ip]


class NetworkInfo(NamedTuple):
    """Combined results of querying the nodes and links on the network.

    Errors are stored as a dictionary, indexed by the IP address and storing the error
    and any message in a tuple.

    """

    nodes: list[SystemInfo]
    links: list[LinkInfo]
    errors: list[NodeResult]


@attrs.define
class NodeError:
    error: PollingError
    response: str

    def __str__(self):
        return f"{self.error} ('{self.response[10:]}...')"


class PollingError(enum.Enum):
    """Enumerates possible errors when polling a node."""

    INVALID_RESPONSE = enum.auto()
    PARSE_ERROR = enum.auto()
    CONNECTION_ERROR = enum.auto()
    HTTP_ERROR = enum.auto()
    TIMEOUT_ERROR = enum.auto()

    def __str__(self):
        if "HTTP" in self.name:
            # keep the acronym all uppercase
            return "HTTP Error"
        return self.name.replace("_", " ").title()


@attrs.define
class NodeResult:
    ip_address: str
    name: str
    system_info: SystemInfo | None = None
    error: NodeError | None = None

    @property
    def label(self) -> str:
        return f"{self.name or 'name unknown'} ({self.ip_address})"


@attrs.define
class OlsrLink:
    """OLSR link information measuring the cost between nodes.

    The `source` and `destination` attributes are the IP address from

    """

    source: str
    destination: str
    cost: float

    @classmethod
    def from_strings(cls, source: str, destination: str, label: str) -> OlsrLink:
        cost = 99.99 if label == "INFINITE" else float(label)
        return cls(source=source, destination=destination, cost=cost)

    def __str__(self):
        return f"{self.source} -> {self.destination} ({self.cost})"
