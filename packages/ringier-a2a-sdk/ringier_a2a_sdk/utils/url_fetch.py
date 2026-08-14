"""Guarded server-side URL fetching.

Anything that downloads a URL from inside a pod needs the same two protections, so
they live here — the lowest shared layer — rather than in whichever agent needed
them first:

- **SSRF guard** (``assert_public_url``): the URL may be attacker-influenced, and an
  unrestricted fetch can reach cluster-internal services and the cloud-metadata
  endpoint.
- **Size cap** (``fetch_bytes_capped``): a plain ``client.get()`` buffers the entire
  body before any check can run, so a URL pointing at a huge object exhausts memory
  before a post-hoc length check would fire.

Callers: the orchestrator's file-analyzer (user-supplied URLs) and the multi-modal
URL → inline-base64 converter in ``bedrock_image_processor`` (attachment URLs).
"""

import asyncio
import ipaddress
import logging
import socket
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


class SSRFError(ValueError):
    """Raised when a URL targets a non-public address (SSRF guard)."""


class PayloadTooLarge(ValueError):
    """Raised when a download exceeds the caller's byte cap.

    Subclasses ``ValueError`` so callers that only care about "the fetch failed with a
    user-facing message" keep working; ``size`` carries the declared byte count when the
    ``Content-Length`` header gave one, else ``None`` (the stream was abandoned early, so
    the true size is unknown).
    """

    def __init__(self, message: str, size: int | None = None):
        super().__init__(message)
        self.size = size


def is_blocked_address(ip: str) -> bool:
    """True if an IP is loopback/private/link-local/reserved (i.e. not publicly routable)."""
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local  # blocks 169.254.0.0/16, incl. the cloud metadata endpoint
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


async def assert_public_url(url: str) -> None:
    """Reject URLs whose host resolves to a non-public address.

    Server-side fetches of caller-supplied URLs run from inside the pod and return (or
    inline) their body, so an unrestricted fetch is an SSRF that can reach
    cluster-internal services and cloud-metadata endpoints. Resolve the host and refuse
    any URL that maps to a loopback/private/link-local/reserved address.

    Note: this validates at resolution time and does not pin the connection to the
    resolved IP, so it does not defend against active DNS rebinding; combine with an
    egress NetworkPolicy for defense-in-depth. Callers must also keep redirects off,
    or a 302 can bounce a guarded URL to an unguarded one.

    Raises:
        SSRFError: The scheme is not http(s), the host is missing/unresolvable, or any
            resolved address is non-public.
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise SSRFError(f"Unsupported URL scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise SSRFError("URL has no host")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise SSRFError(f"Could not resolve host {host!r}: {e}")

    resolved = {info[4][0] for info in infos}
    if not resolved:
        raise SSRFError(f"Host {host!r} did not resolve")
    for ip in resolved:
        if is_blocked_address(ip):
            raise SSRFError(f"Refusing to fetch a URL that resolves to a non-public address ({ip})")


async def fetch_bytes_capped(
    client: httpx.AsyncClient, url: str, max_bytes: int, too_large_msg: str
) -> bytes:
    """GET ``url`` and return its body, aborting as soon as it exceeds ``max_bytes``.

    Streams the response so an oversized (or malicious) URL is never fully buffered before the
    limit is enforced: a declared ``Content-Length`` over the cap is rejected before reading the
    body, and the incremental read still caps memory at ~``max_bytes`` when the header is absent
    or lies.

    Raises:
        PayloadTooLarge: The body is (or grows) past ``max_bytes``.
    """
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        cl = response.headers.get("content-length")
        if cl and cl.strip().isdigit() and int(cl) > max_bytes:
            raise PayloadTooLarge(too_large_msg, int(cl))
        buf = bytearray()
        async for chunk in response.aiter_bytes():
            buf.extend(chunk)
            if len(buf) > max_bytes:
                # Leaving the stream context closes the connection, so the rest of the
                # body is never pulled down.
                raise PayloadTooLarge(too_large_msg)
        return bytes(buf)
