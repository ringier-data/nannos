"""Shared httpx stubs for the URL → inline-base64 preprocessing tests.

The converter streams downloads (so an oversized body is abandoned rather than
buffered), which means a stub has to implement ``client.stream(...)`` as an async
context manager yielding a response with ``aiter_bytes()`` — not just ``get()``.
"""

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

from ringier_a2a_sdk.utils import bedrock_image_processor


class _StubStreamResponse:
    def __init__(self, payload: bytes, *, headers: dict | None = None, chunk_size: int = 64 * 1024):
        self.content = payload
        self.headers = headers if headers is not None else {"content-length": str(len(payload))}
        self._chunk_size = chunk_size
        self.raise_for_status = MagicMock()

    async def aiter_bytes(self):
        for start in range(0, len(self.content), self._chunk_size):
            yield self.content[start : start + self._chunk_size]


class _StubStreamContext:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc_info):
        return False


def stub_httpx_client(payload: bytes = b"", *, headers: dict | None = None, error: Exception | None = None):
    """Build a stub ``httpx.AsyncClient`` serving *payload* (or raising *error*).

    Pass ``headers={}`` to simulate a response with no ``Content-Length``, or a
    dict with a lying value to exercise the running byte tally.
    """
    response = _StubStreamResponse(payload, headers=headers)

    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    if error is not None:
        client.stream = MagicMock(side_effect=error)
        client.get = AsyncMock(side_effect=error)
    else:
        client.stream = MagicMock(return_value=_StubStreamContext(response))
        client.get = AsyncMock(return_value=response)
    return client


@contextmanager
def allow_all_urls():
    """Neutralize the SSRF guard for tests that stub the transport anyway.

    The guard does a real DNS lookup, which unit tests must not depend on. Tests that
    exercise the guard itself patch it with a raising stub instead.
    """
    with patch.object(bedrock_image_processor, "assert_public_url", AsyncMock(return_value=None)):
        yield
