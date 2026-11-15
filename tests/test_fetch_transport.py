"""Adversarial HTTP/DNS fixtures; no test opens a network socket."""
import asyncio
import gzip
import ssl
import zlib
from collections import deque
from dataclasses import dataclass

import httpcore
import pytest

from ai_builder.fetch_transport import ControlledFetcher, FetchError, FetchLimits

PUBLIC = "93.184.216.34"
PUBLIC_OTHER = "1.1.1.1"


@dataclass
class Reply:
    path: str
    body: bytes = b"<html><body>Price $10 monthly</body></html>"
    status: int = 200
    mime: str = "text/html"
    headers: tuple[tuple[str, str], ...] = ()
    peer: str | None = PUBLIC
    delay: float = 0
    chunked: bool = False

    def wire(self):
        headers = [("Content-Type", self.mime), *self.headers]
        if self.chunked:
            headers.append(("Transfer-Encoding", "chunked"))
            body = hex(len(self.body))[2:].encode() + b"\r\n" + self.body + b"\r\n0\r\n\r\n"
        else:
            if not any(key.lower() == "content-length" for key, _ in headers):
                headers.append(("Content-Length", str(len(self.body))))
            body = self.body
        head = f"HTTP/1.1 {self.status} Fixture\r\n" + "".join(f"{key}: {value}\r\n" for key, value in headers)
        return head.encode("ascii") + b"\r\n" + body


class FixtureStream(httpcore.AsyncNetworkStream):
    def __init__(self, reply):
        self.reply, self.data = reply, reply.wire()
        self.sent = bytearray()
        self.closed = False
        self.server_hostname = None
        self.context = None

    async def read(self, max_bytes, timeout=None):
        assert self.sent.startswith(f"GET {self.reply.path} HTTP/1.1\r\n".encode())
        if self.reply.delay:
            await asyncio.sleep(self.reply.delay)
        result, self.data = self.data[:max_bytes], self.data[max_bytes:]
        return result

    async def write(self, buffer, timeout=None):
        self.sent.extend(buffer)

    async def aclose(self):
        self.closed = True

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        self.server_hostname, self.context = server_hostname, ssl_context
        return self

    def get_extra_info(self, info):
        if info == "server_addr":
            return (self.reply.peer, 443) if self.reply.peer else None
        return None


class FixtureNetwork(httpcore.AsyncNetworkBackend):
    def __init__(self, replies):
        self.replies = deque(replies)
        self.streams = []
        self.connections = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        assert self.replies, "unexpected outbound connection"
        stream = FixtureStream(self.replies.popleft())
        self.streams.append(stream)
        self.connections.append((host, port))
        return stream


def robots(body=b"User-agent: *\nDisallow:\n", **kwargs):
    return Reply("/robots.txt", body=body, mime="text/plain", **kwargs)


def fixture_fetcher(*replies, answers=None, **kwargs):
    network = FixtureNetwork(replies)
    dns_calls = []

    async def resolve(host, port):
        dns_calls.append((host, port))
        return answers(host, len(dns_calls)) if answers else [PUBLIC]

    return ControlledFetcher(resolver=resolve, network_backend=network, **kwargs), network, dns_calls


def run_fetch(fetcher, url="https://example.com/pricing"):
    return asyncio.run(fetcher.fetch(url))


def assert_failure(fetcher, code, url="https://example.com/pricing"):
    with pytest.raises(FetchError) as failure:
        run_fetch(fetcher, url)
    assert failure.value.code == code
    return failure.value


def test_httpcore_pins_connected_ip_but_preserves_host_tls_identity_and_closes_streams(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1234")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1234")
    fetcher, network, calls = fixture_fetcher(robots(), Reply("/pricing"))
    result = run_fetch(fetcher)
    assert result.body == b"<html><body>Price $10 monthly</body></html>"
    assert result.content_type == "text/html"
    assert result.requested_url == result.final_url == "https://example.com/pricing"
    assert result.fetched_at.utcoffset().total_seconds() == 0
    assert result.peer_ips == (PUBLIC,)
    assert calls == [("example.com", 443)] * 2
    assert network.connections == [(PUBLIC, 443)] * 2
    for stream in network.streams:
        assert stream.server_hostname == "example.com"
        assert stream.context.verify_mode == ssl.CERT_REQUIRED
        assert stream.context.check_hostname
        assert b"Host: example.com\r\n" in stream.sent
        assert b"Authorization:" not in stream.sent
        assert b"Cookie:" not in stream.sent
        assert stream.closed


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "ftp://example.com/file", "https://user:secret@example.com/pricing",
    "http://localhost/", "https://foo.localhost/", "http://foo.local/", "http://foo.internal/",
    "http://127.0.0.1/", "http://10.0.0.1/", "http://169.254.169.254/", "http://[::1]/",
    "http://168.63.129.16/", "http://100.100.100.200/",
    "http://[::ffff:127.0.0.1]/", "http://[64:ff9b::7f00:1]/", "http://[2002:7f00:1::]/",
    "http://[fe80::1%25eth0]/", "https://example.com:8443/", "https://example.com:0/", "http://224.0.0.1/",
    "http://example.com\\@127.0.0.1/", " https://example.com/", "https://example.com/\nsecret",
])
def test_hostile_urls_never_resolve_or_connect(url):
    fetcher, network, calls = fixture_fetcher()
    with pytest.raises(FetchError) as failure:
        run_fetch(fetcher, url)
    assert failure.value.code in {"unsafe_url", "unsafe_address", "unsafe_port"}
    assert "secret" not in str(failure.value)
    assert "secret" not in failure.value.requested_url
    assert not calls and not network.connections


@pytest.mark.parametrize("addresses", [[], ["127.0.0.1"], ["169.254.169.254"], ["fc00::1"],
                                        [PUBLIC, "192.168.0.1"], ["not-an-address"]])
def test_all_dns_answers_must_be_public(addresses):
    fetcher, network, _ = fixture_fetcher(answers=lambda host, call: addresses)
    assert_failure(fetcher, "dns_unavailable" if not addresses else "unsafe_address")
    assert not network.connections


def test_rebinding_between_robots_and_document_is_blocked_before_document_connection():
    fetcher, network, _ = fixture_fetcher(robots(), answers=lambda host, call: [PUBLIC if call == 1 else "127.0.0.1"])
    assert_failure(fetcher, "unsafe_address")
    assert len(network.connections) == 1


@pytest.mark.parametrize("peer", ["127.0.0.1", PUBLIC_OTHER, None])
def test_actual_peer_is_verified_before_sending_http(peer):
    fetcher, network, _ = fixture_fetcher(robots(peer=peer))
    with pytest.raises(FetchError) as failure:
        run_fetch(fetcher)
    assert failure.value.code in {"unsafe_address", "peer_mismatch"}
    assert not network.streams[0].sent
    assert network.streams[0].closed


def test_every_redirect_destination_has_dns_and_robots_checked_before_document():
    fetcher, network, _ = fixture_fetcher(
        robots(), Reply("/pricing", status=302, headers=(("Location", "https://other.example/cost"),)),
        robots(b"User-agent: *\nDisallow: /cost\n"),
    )
    error = assert_failure(fetcher, "robots_denied")
    assert error.redirect_chain == ("https://example.com/pricing",)
    assert len(network.connections) == 3


def test_redirect_revalidates_url_and_rejects_metadata_ip():
    fetcher, network, _ = fixture_fetcher(
        robots(), Reply("/pricing", status=302, headers=(("Location", "http://169.254.169.254/latest"),)),
    )
    assert_failure(fetcher, "unsafe_address")
    assert len(network.connections) == 2


def test_robots_redirects_receive_the_same_ssrf_checks():
    fetcher, network, _ = fixture_fetcher(robots(status=302, headers=(("Location", "http://10.0.0.1/robots.txt"),)))
    assert_failure(fetcher, "unsafe_address")
    assert len(network.connections) == 1


def test_redirect_success_records_chain_and_does_not_forward_cookies():
    fetcher, network, _ = fixture_fetcher(
        robots(), Reply("/pricing", status=302, headers=(("Location", "/new#section"), ("Set-Cookie", "secret=yes"))),
        Reply("/new", mime="application/pdf", body=b"%PDF-fixture"),
    )
    result = run_fetch(fetcher)
    assert result.redirect_chain == ("https://example.com/pricing", "https://example.com/new")
    assert result.peer_ips == (PUBLIC, PUBLIC)
    assert result.final_url == "https://example.com/new"
    assert b"Cookie:" not in network.streams[-1].sent


def test_redirect_limit_and_loop():
    fetcher, _, _ = fixture_fetcher(robots(), Reply("/pricing", status=302, headers=(("Location", "/next"),)),
                                    limits=FetchLimits(max_redirects=0))
    assert_failure(fetcher, "redirect_limit")
    fetcher, _, _ = fixture_fetcher(robots(), Reply("/pricing", status=302, headers=(("Location", "/pricing"),)))
    assert_failure(fetcher, "redirect_loop")


@pytest.mark.parametrize("status", [401, 403, 429, 500, 503])
def test_inaccessible_robots_fails_closed(status):
    fetcher, network, _ = fixture_fetcher(robots(status=status))
    assert_failure(fetcher, "robots_unavailable")
    assert len(network.connections) == 1


@pytest.mark.parametrize("status", [404, 410])
def test_missing_robots_is_allowed(status):
    fetcher, _, _ = fixture_fetcher(robots(status=status), Reply("/pricing"))
    assert run_fetch(fetcher).status_code == 200


@pytest.mark.parametrize("policy,path,allowed", [
    ("User-agent: *\nDisallow: /", "/pricing", False),
    ("User-agent: *\nDisallow: /private\nAllow: /private/public", "/private/public/article", True),
    ("User-agent: *\nDisallow: /p\nAllow: /p", "/pricing", True),
    ("User-agent: *\nDisallow: /*?*secret=", "/pricing?secret=x", False),
    ("User-agent: *\nDisallow: /*.pdf$", "/report.pdf", False),
    ("User-agent: *\nDisallow: /*.pdf$", "/report.pdf?x=1", True),
    ("User-agent: *\nDisallow: /pr%69cing", "/pricing", False),
    ("User-agent: *\nDisallow: /path%2fsecret", "/path%2Fsecret", False),
    ("User-agent: *\nDisallow: /path%2Fsecret", "/path/secret", True),
    ("User-agent: *\nAllow: /page\nDisallow: /*.htm", "/page.htm", False),
    ("User-agent: *\nAllow: /page\nDisallow: /*.ph", "/page.php5", True),
    ("User-agent: *\nDisallow: /\nUser-agent: AIBusinessBuilder\nAllow: /", "/pricing", True),
    ("User-agent: AIBusinessBuilder\nDisallow: /private\nUser-agent: AIBusinessBuilder\nDisallow: /pricing", "/pricing", False),
])
def test_robots_specificity_wildcards_encoding_and_product_groups(policy, path, allowed):
    replies = [robots(policy.encode())] + ([Reply(path)] if allowed else [])
    fetcher, _, _ = fixture_fetcher(*replies)
    if allowed:
        assert run_fetch(fetcher, "https://example.com" + path).status_code == 200
    else:
        assert_failure(fetcher, "robots_denied", "https://example.com" + path)


@pytest.mark.parametrize("body,code", [
    (b"<html>Access denied</html>", "robots_invalid"),
    (b"User-agent: *\nDisallow: /\xff", "robots_invalid"),
    (b"User-agent: *\nCrawl-delay: 10", "robots_unsupported_policy"),
    (b"User-agent: *\nRequest-rate: 1/60", "robots_unsupported_policy"),
    (b"User-agent: AIBusinessBuilder*\nDisallow: /", "robots_invalid"),
])
def test_unreadable_or_unsupported_robots_policy_is_not_ignored(body, code):
    fetcher, network, _ = fixture_fetcher(robots(body))
    assert_failure(fetcher, code)
    assert len(network.connections) == 1


def test_site_policy_applies_before_any_request_and_to_redirects():
    fetcher, network, _ = fixture_fetcher(site_policy=lambda url: False)
    assert_failure(fetcher, "site_policy_denied")
    assert not network.connections
    fetcher, network, _ = fixture_fetcher(
        robots(), Reply("/pricing", status=302, headers=(("Location", "https://blocked.example/page"),)),
        site_policy=lambda url: "blocked.example" not in url,
    )
    assert_failure(fetcher, "site_policy_denied")
    assert len(network.connections) == 2


@pytest.mark.parametrize("tag,allowed", [("noarchive", False), ("nosnippet", False), ("noai", False),
                                        ("max-snippet: 0", False), ("noindex, nofollow", True)])
def test_publisher_snapshot_restrictions_in_headers(tag, allowed):
    fetcher, _, _ = fixture_fetcher(robots(), Reply("/pricing", headers=(("X-Robots-Tag", tag),)))
    if allowed:
        assert run_fetch(fetcher).body
    else:
        assert_failure(fetcher, "site_policy_denied")


@pytest.mark.parametrize("mime", ["text/plain", "application/octet-stream", "image/png", ""])
def test_only_document_mime_allowlist(mime):
    fetcher, _, _ = fixture_fetcher(robots(), Reply("/pricing", mime=mime))
    assert_failure(fetcher, "unsupported_content_type")


def test_raw_size_cap_applies_without_content_length():
    fetcher, _, _ = fixture_fetcher(robots(), Reply("/pricing", body=b"a" * 101, chunked=True), limits=FetchLimits(max_bytes=100))
    assert_failure(fetcher, "document_too_large")


def test_content_length_over_limit_is_rejected_before_body_read():
    fetcher, network, _ = fixture_fetcher(robots(), Reply("/pricing", body=b"", headers=(("Content-Length", "5000001"),)))
    assert_failure(fetcher, "document_too_large")
    assert network.streams[-1].closed


@pytest.mark.parametrize("encoding,compress", [("gzip", gzip.compress), ("deflate", zlib.compress)])
def test_decoded_size_cap_blocks_compression_bombs(encoding, compress):
    fetcher, _, _ = fixture_fetcher(robots(), Reply("/pricing", body=compress(b"x" * 100_000),
                                                   headers=(("Content-Encoding", encoding),)),
                                   limits=FetchLimits(max_bytes=1000))
    assert_failure(fetcher, "document_too_large")


def test_valid_gzip_is_decoded_and_exact_size_limit_is_accepted():
    body = b"<html>" + b"x" * 87 + b"</html>"
    assert len(body) == 100
    fetcher, _, _ = fixture_fetcher(robots(), Reply("/pricing", body=gzip.compress(body), headers=(("Content-Encoding", "gzip"),)),
                                   limits=FetchLimits(max_bytes=100))
    assert run_fetch(fetcher).body == body


@pytest.mark.parametrize("body,encoding", [(b"bad", "gzip"), (gzip.compress(b"body")[:-2], "gzip"),
                                           (gzip.compress(b"one") + gzip.compress(b"two"), "gzip"),
                                           (b"bytes", "br"), (b"bytes", "gzip, deflate")])
def test_invalid_or_unsupported_compression_cannot_produce_partial_success(body, encoding):
    fetcher, _, _ = fixture_fetcher(robots(), Reply("/pricing", body=body, headers=(("Content-Encoding", encoding),)))
    assert_failure(fetcher, "unsupported_content_encoding" if encoding in {"br", "gzip, deflate"} else "invalid_content_encoding")


def test_one_deadline_covers_robots_and_document_and_closes_on_timeout():
    fetcher, network, _ = fixture_fetcher(robots(delay=0.04), Reply("/pricing", delay=0.04),
                                         limits=FetchLimits(timeout_seconds=0.07))
    assert_failure(fetcher, "fetch_timeout")
    assert all(stream.closed for stream in network.streams)


def test_dns_is_inside_total_deadline():
    network = FixtureNetwork([])

    async def slow_resolve(host, port):
        await asyncio.sleep(1)
        return [PUBLIC]

    fetcher = ControlledFetcher(resolver=slow_resolve, network_backend=network, limits=FetchLimits(timeout_seconds=0.01))
    assert_failure(fetcher, "fetch_timeout")
    assert not network.connections


def test_limits_cannot_weaken_approved_security_policy():
    for kwargs in ({"timeout_seconds": 21}, {"max_bytes": 5_000_001}, {"max_redirects": 6},
                   {"max_robots_bytes": 512_001}, {"max_bytes": 2.5}, {"max_redirects": True}):
        with pytest.raises(ValueError):
            FetchLimits(**kwargs)
