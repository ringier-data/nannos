"""Proxy headers middleware for FastAPI."""

from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware


class ProxyHeadersMiddleware(BaseHTTPMiddleware):
    """Middleware to handle X-Forwarded-* headers from reverse proxies/load balancers.

    This ensures request.url_for generates URLs with the correct scheme (https)
    when the application is running behind a proxy that terminates SSL.

    TODO: we could eventually think about just wrapping url_for using a environment variable
          to indicate the external scheme instead of relying on headers.
          Finally we just need http for local and https for all the rest
    """

    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        """Process the request and update scheme/host from proxy headers."""
        # Check for X-Forwarded-Proto header
        forwarded_proto = request.headers.get('x-forwarded-proto')
        if forwarded_proto:
            # Update the request scope to use the forwarded protocol
            request.scope['scheme'] = forwarded_proto

        # Check for X-Forwarded-Host header
        forwarded_host = request.headers.get('x-forwarded-host')
        if forwarded_host:
            request.scope['server'] = (forwarded_host, None)

        return await call_next(request)
