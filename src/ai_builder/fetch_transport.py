"""Bounded, public-network-only document acquisition, without parser downloads.

DNS validation is enforced at the transport boundary: a fresh connection receives
only a validated numeric address while HTTP Host and TLS identity retain the URL
hostname. No proxies, ambient credentials, cookies, retries or redirect following
are delegated to a client. Robots and each document hop share one deadline.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import ssl
import zlib
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import quote, urljoin, urlsplit

import httpcore
import httpx

USER_AGENT = "AIBusinessBuilder/0.1"
PRODUCT_TOKEN = "aibusinessbuilder"
DOCUMENT_TYPES = frozenset({"text/html", "application/xhtml+xml", "application/pdf"})
REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


@dataclass(frozen=True)
class SourceResponse:
    requested_url: str
    final_url: str
    fetched_at: datetime
    content_type: str
    body: bytes
    status_code: int = 200
    redirect_chain: tuple[str, ...] = ()
    peer_ips: tuple[str, ...] = ()
    policy_status: str = "validated"
    acquisition_method: str = "controlled_http"


class FetchError(Exception):
    """Stable acquisition failure; never includes response bodies or credentials."""

    def __init__(self, code: str, requested_url: str = "", redirect_chain: tuple[str, ...] = ()):
        self.code = code
        self.requested_url = requested_url
        self.redirect_chain = redirect_chain
        super().__init__(code)


@dataclass(frozen=True)
class FetchLimits:
    timeout_seconds: float = 20.0
    max_bytes: int = 5_000_000
    max_redirects: int = 5
    max_robots_bytes: int = 512_000

    def __post_init__(self) -> None:
        if (type(self.timeout_seconds) not in {int, float} or not 0 < self.timeout_seconds <= 20
                or type(self.max_bytes) is not int or not 0 < self.max_bytes <= 5_000_000):
            raise ValueError("fetch limits must not exceed the approved policy")
        if (type(self.max_redirects) is not int or not 0 <= self.max_redirects <= 5
                or type(self.max_robots_bytes) is not int or not 0 < self.max_robots_bytes <= 512_000):
            raise ValueError("invalid robots or redirect limits")


Resolver = Callable[[str, int], Awaitable[Iterable[str]]]
SitePolicy = Callable[[str], bool]


async def _system_resolve(host: str, port: int) -> Iterable[str]:
    answers = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(answer[4][0] for answer in answers)


def _public_ip(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise FetchError("unsafe_address") from None
    if (str(address) == "168.63.129.16"  # Azure platform virtual address / WireServer.
            or not address.is_global or address.is_multicast or address.is_reserved
            or address.is_loopback or address.is_link_local or address.is_unspecified):
        raise FetchError("unsafe_address")
    if isinstance(address, ipaddress.IPv6Address):
        # Do not allow IPv4-mapped, transition or NAT64 addresses to turn an
        # apparently public IPv6 destination into a private IPv4 connection.
        if address.ipv4_mapped or address.sixtofour or address.teredo or any(
            address in ipaddress.IPv6Network(network)
            for network in ("64:ff9b::/96", "64:ff9b:1::/48")
        ):
            raise FetchError("unsafe_address")
    return str(address)


def _validated_url(value: str) -> httpx.URL:
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 33 or ord(c) == 127 for c in value) or "\\" in value:
        raise FetchError("unsafe_url")
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in {"http", "https"} or parts.username is not None or parts.password is not None:
            raise FetchError("unsafe_url")
        if not parts.hostname or "%" in parts.hostname:
            raise FetchError("unsafe_url")
        url = httpx.URL(value).copy_with(fragment=None)
        host = url.raw_host.decode("ascii").rstrip(".").lower()
        if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise FetchError("unsafe_url")
        if (url.port if url.port is not None else (443 if url.scheme == "https" else 80)) not in {80, 443}:
            raise FetchError("unsafe_port")
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            _public_ip(host)
        return url
    except (ValueError, httpx.InvalidURL, UnicodeError):
        raise FetchError("unsafe_url") from None


class _PinnedBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, host: str, port: int, ip: str, delegate: httpcore.AsyncNetworkBackend):
        self.host, self.port, self.ip, self.delegate = host, port, ip, delegate

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        if host != self.host or port != self.port or local_address is not None:
            raise FetchError("unexpected_connection")
        stream = await self.delegate.connect_tcp(
            self.ip, port, timeout=timeout, socket_options=socket_options,
        )
        try:
            peer = stream.get_extra_info("server_addr")
            if not peer or _public_ip(peer[0]) != self.ip:
                raise FetchError("peer_mismatch")
        except BaseException:
            await stream.aclose()
            raise
        return stream

    async def connect_unix_socket(self, *args, **kwargs):
        raise FetchError("unexpected_connection")

    async def sleep(self, seconds):
        await asyncio.sleep(seconds)


class _CoreStream(httpx.AsyncByteStream):
    def __init__(self, stream):
        self.stream = stream

    async def __aiter__(self):
        async for chunk in self.stream:
            yield chunk

    async def aclose(self):
        await self.stream.aclose()


class _PinnedTransport(httpx.AsyncBaseTransport):
    def __init__(self, backend: _PinnedBackend):
        self.pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl.create_default_context(), network_backend=backend,
            max_connections=1, max_keepalive_connections=0, retries=0, http2=False,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.pool.handle_async_request(httpcore.Request(
            method=request.method,
            url=httpcore.URL(scheme=request.url.raw_scheme, host=request.url.raw_host,
                             port=request.url.port, target=request.url.raw_path),
            headers=request.headers.raw, content=request.stream, extensions=request.extensions,
        ))
        return httpx.Response(status_code=response.status, headers=response.headers,
                              stream=_CoreStream(response.stream), extensions=response.extensions)

    async def aclose(self):
        await self.pool.aclose()


def _normalize_path(value: str) -> str:
    """RFC9309 matching: decode only unreserved octets, preserve encoded '/'."""
    value = quote(value, safe="/%?:@!$&'()*+,;=-._~")
    output, i = [], 0
    while i < len(value):
        if value[i] == "%":
            if i + 2 >= len(value):
                raise FetchError("robots_invalid")
            try:
                octet = int(value[i + 1:i + 3], 16)
            except ValueError:
                raise FetchError("robots_invalid") from None
            char = chr(octet)
            output.append(char if char.isascii() and (char.isalnum() or char in "-._~") else f"%{octet:02X}")
            i += 3
        else:
            output.append(value[i])
            i += 1
    return "".join(output)


def _glob_prefix(pattern: str, path: str) -> bool:
    """Wildcard/prefix matching without a backtracking regular expression."""
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    pieces = pattern.split("*")
    if not path.startswith(pieces[0]):
        return False
    offset = len(pieces[0])
    if len(pieces) == 1:
        return not anchored or offset == len(path)
    for piece in pieces[1:-1]:
        position = path.find(piece, offset)
        if position < 0:
            return False
        offset = position + len(piece)
    last = pieces[-1]
    if anchored:
        return path.endswith(last) and len(path) - len(last) >= offset
    return path.find(last, offset) >= 0


@dataclass(frozen=True)
class _Robots:
    rules: tuple[tuple[bool, str], ...] = ()
    unsupported_policy: bool = False

    def permits(self, url: httpx.URL) -> bool:
        if self.unsupported_policy:
            raise FetchError("robots_unsupported_policy")
        path = _normalize_path(url.raw_path.decode("ascii"))
        best = (-1, True)
        for allow, pattern in self.rules:
            if _glob_prefix(pattern, path):
                # Most specific matching octet count wins; Allow breaks ties.
                candidate = (len(pattern.encode("ascii")), allow)
                best = max(best, candidate)
        return best[1]


def _parse_robots(body: bytes) -> _Robots:
    try:
        text = body.decode("utf-8-sig")
    except UnicodeError:
        raise FetchError("robots_invalid") from None
    groups: list[tuple[list[str], list[tuple[bool, str]], bool]] = []
    agents: list[str] = []
    rules: list[tuple[bool, str]] = []
    unsupported, rules_started = False, False
    directive_count = 0
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        directive_count += 1
        if directive_count > 4096:
            raise FetchError("robots_invalid")
        if len(line) > 4096 or ":" not in line:
            raise FetchError("robots_invalid")
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()
        if key == "user-agent":
            if rules_started:
                groups.append((agents, rules, unsupported))
                agents, rules, unsupported, rules_started = [], [], False, False
            if (not value or len(value) > 128 or ("*" in value and value != "*")
                    or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_-*" for c in value)):
                raise FetchError("robots_invalid")
            agents.append(value.lower())
        elif key in {"allow", "disallow"}:
            if not agents:
                continue  # RFC9309: ignore rules before the first group.
            rules_started = True
            if not value:
                continue
            if not value.startswith("/") or len(value) > 2048:
                raise FetchError("robots_invalid")
            rules.append((key == "allow", _normalize_path(value)))
        elif key not in {"sitemap"} and agents:
            # Crawl-delay/request-rate and vendor restrictions are not silently
            # ignored: an unsupported applicable policy leaves an evidence gap.
            rules_started = True
            unsupported = True
        elif key not in {"sitemap"}:
            raise FetchError("robots_invalid")
        if len(groups) + len(agents) + len(rules) > 4096:
            raise FetchError("robots_invalid")
    if agents:
        groups.append((agents, rules, unsupported))
    selected, maximum = [], -1
    for group_agents, group_rules, blocked in groups:
        matching = [len(agent) if agent != "*" else 0 for agent in group_agents
                    if agent == "*" or agent in PRODUCT_TOKEN]
        if not matching:
            continue
        score = max(matching)
        if score > maximum:
            selected, maximum = [], score
        if score == maximum:
            selected.append((group_rules, blocked))
    return _Robots(tuple(rule for rules, _ in selected for rule in rules),
                   any(blocked for _, blocked in selected))


async def _read_bounded(response: httpx.Response, limit: int) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length is not None:
        if not content_length.isdecimal():
            raise FetchError("invalid_content_length")
        if int(content_length) > limit:
            raise FetchError("document_too_large")
    encoding = response.headers.get("content-encoding", "identity").strip().lower()
    if encoding not in {"identity", "gzip", "deflate"}:
        raise FetchError("unsupported_content_encoding")
    decoder = zlib.decompressobj(31 if encoding == "gzip" else 15) if encoding != "identity" else None
    result = bytearray()
    raw_size = 0
    try:
        async for chunk in response.aiter_raw(chunk_size=65536):
            raw_size += len(chunk)
            if raw_size > limit:
                raise FetchError("document_too_large")
            decoded = decoder.decompress(chunk, limit - len(result) + 1) if decoder else chunk
            result.extend(decoded)
            if len(result) > limit or (decoder and decoder.unconsumed_tail):
                raise FetchError("document_too_large")
            if decoder and decoder.unused_data:
                raise FetchError("invalid_content_encoding")
            await asyncio.sleep(0)
        if decoder and not decoder.eof:
            raise FetchError("invalid_content_encoding")
    except zlib.error:
        raise FetchError("invalid_content_encoding") from None
    return bytes(result)


class ControlledFetcher:
    """Fetch one source; injectable DNS/socket boundaries support offline tests.

    site_policy is a synchronous allow/deny predicate for known publisher rules.
    It is applied to robots and document destinations, including redirects.
    Unsupported robots restrictions fail closed. No cross-run robots cache is
    retained, and each invocation checks current robots before document access.
    """

    def __init__(self, *, limits: FetchLimits = FetchLimits(), resolver: Resolver = _system_resolve,
                 network_backend: httpcore.AsyncNetworkBackend | None = None,
                 site_policy: SitePolicy | None = None):
        self.limits, self.resolver = limits, resolver
        self.network_backend = network_backend or httpcore.AnyIOBackend()
        self.site_policy = site_policy or (lambda _: True)

    async def _request(self, url: httpx.URL, *, robots: bool):
        canonical = str(url)
        if not self.site_policy(canonical):
            raise FetchError("site_policy_denied")
        host = url.raw_host.decode("ascii")
        port = url.port if url.port is not None else (443 if url.scheme == "https" else 80)
        answers = tuple(dict.fromkeys(_public_ip(ip) for ip in await self.resolver(host, port)))
        if not answers:
            raise FetchError("dns_unavailable")
        ip = answers[0]
        transport = _PinnedTransport(_PinnedBackend(host, port, ip, self.network_backend))
        async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False,
                                     timeout=self.limits.timeout_seconds, headers={
                                         "User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate",
                                         "Accept": "text/plain" if robots else ", ".join(sorted(DOCUMENT_TYPES)),
                                     }) as client:
            async with client.stream("GET", url) as response:
                status = response.status_code
                if status in REDIRECT_CODES:
                    location = response.headers.get("location")
                    if not location:
                        raise FetchError("invalid_redirect")
                    return status, location, "", b"", ip
                if robots and status in {404, 410}:
                    return status, None, "", b"", ip
                if status != 200:
                    raise FetchError("robots_unavailable" if robots else f"http_status_{status}")
                mime = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if mime not in ({"text/plain"} if robots else DOCUMENT_TYPES):
                    raise FetchError("robots_invalid" if robots else "unsupported_content_type")
                if not robots:
                    tags = response.headers.get("x-robots-tag", "").lower()
                    restricted = {"noai", "noarchive", "nosnippet"}
                    words = set(tags.replace(":", " ").replace(",", " ").split())
                    if words & restricted or "max-snippet:0" in tags.replace(" ", ""):
                        raise FetchError("site_policy_denied")
                body = await _read_bounded(response, self.limits.max_robots_bytes if robots else self.limits.max_bytes)
                return status, None, mime, body, ip

    async def _robots(self, url: httpx.URL) -> _Robots:
        current = url.copy_with(path="/robots.txt", query=None, fragment=None)
        seen: set[str] = set()
        for hop in range(self.limits.max_redirects + 1):
            if str(current) in seen:
                raise FetchError("robots_redirect_loop")
            seen.add(str(current))
            status, location, _, body, _ = await self._request(current, robots=True)
            if location is None:
                return _Robots() if status in {404, 410} else _parse_robots(body)
            if hop == self.limits.max_redirects:
                raise FetchError("robots_redirect_limit")
            current = _validated_url(urljoin(str(current), location))
        raise AssertionError("unreachable")

    async def fetch(self, url: str) -> SourceResponse:
        # Validate before preserving a URL in structured errors: credentials in
        # rejected URLs must not become logs or evidence metadata.
        current = _validated_url(url)
        requested = str(current)
        chain: list[str] = []
        peers: list[str] = []
        robots_cache: dict[tuple[str, str, int | None], _Robots] = {}
        try:
            async with asyncio.timeout(self.limits.timeout_seconds):
                for hop in range(self.limits.max_redirects + 1):
                    if str(current) in chain:
                        raise FetchError("redirect_loop")
                    origin = (current.scheme, current.host, current.port)
                    if not self.site_policy(str(current)):
                        raise FetchError("site_policy_denied")
                    if origin not in robots_cache:
                        robots_cache[origin] = await self._robots(current)
                    if not robots_cache[origin].permits(current):
                        raise FetchError("robots_denied")
                    chain.append(str(current))
                    status, location, mime, body, ip = await self._request(current, robots=False)
                    peers.append(ip)
                    if location is None:
                        return SourceResponse(requested, str(current), datetime.now(timezone.utc),
                                              mime, body, status, tuple(chain), tuple(peers))
                    if hop == self.limits.max_redirects:
                        raise FetchError("redirect_limit")
                    current = _validated_url(urljoin(str(current), location))
        except FetchError as exc:
            raise FetchError(exc.code, requested, tuple(chain)) from None
        except (TimeoutError, httpcore.TimeoutException, httpx.TimeoutException):
            raise FetchError("fetch_timeout", requested, tuple(chain)) from None
        except (OSError, httpcore.NetworkError, httpcore.ProtocolError, httpx.HTTPError):
            raise FetchError("network_error", requested, tuple(chain)) from None
        raise AssertionError("unreachable")
