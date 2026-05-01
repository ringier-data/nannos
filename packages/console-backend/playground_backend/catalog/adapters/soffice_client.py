"""Async HTTP client for the soffice-worker pod.

The soffice-worker service is a separate K8s pod that owns all LibreOffice
operations.  Isolating soffice in its own cgroup means an OOMKill there does
NOT restart the catalog-worker pod — the request simply receives a
connection error, which is converted to ExtractionResourceError so the file
is marked skipped and processing continues.

Configuration
-------------
SOFFICE_WORKER_URL  (required)
    Base URL of the soffice-worker service, e.g.
    ``http://soffice-worker:8080``

SOFFICE_WORKER_TIMEOUT_S  (optional, default 150)
    Per-request timeout in seconds.  soffice itself gets 120 s inside the
    service; add 30 s for upload/response travel.
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx

from .base import ExtractedPage, ExtractionResourceError

logger = logging.getLogger(__name__)

_SOFFICE_WORKER_URL = os.environ.get("SOFFICE_WORKER_URL", "").rstrip("/")
_TIMEOUT = float(os.environ.get("SOFFICE_WORKER_TIMEOUT_S", "150"))

# Retry configuration for transient connection failures (e.g. pod restart).
_CONNECT_RETRY_ATTEMPTS = 3
_CONNECT_RETRY_DELAYS = (2.0, 5.0)  # seconds between attempts 1→2 and 2→3

# Shared client — one instance per process lifetime.  Reusing keeps the
# TCP connection to the soffice-worker service alive between calls.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            base_url=_SOFFICE_WORKER_URL,
            timeout=httpx.Timeout(_TIMEOUT, connect=10.0),
        )
    return _client


def _reset_client() -> httpx.AsyncClient:
    """Close the shared client and create a fresh one.

    Called after a connection failure to flush any stale keep-alive
    connections from the pool before retrying.
    """
    global _client
    if _client is not None and not _client.is_closed:
        try:
            _client.aclose()  # best-effort; not awaited — avoid blocking hot path
        except Exception:
            pass
    _client = None
    return _get_client()


def _check_configured() -> None:
    if not _SOFFICE_WORKER_URL:
        raise ExtractionResourceError("SOFFICE_WORKER_URL is not set — cannot perform PPTX extraction")


async def extract_text(pptx_path: str, file_id: str) -> list[ExtractedPage]:
    """POST a PPTX file to ``/extract-text`` and return per-slide ExtractedPage objects.

    Retries up to ``_CONNECT_RETRY_ATTEMPTS`` times on connection errors
    (e.g. soffice-worker pod restart) with exponential backoff.

    Args:
        pptx_path: Absolute path to the downloaded PPTX on disk.
        file_id:   Drive file ID embedded in each page's ``source_ref``.

    Raises:
        ExtractionResourceError: on any HTTP or transport failure.
    """
    _check_configured()
    last_exc: Exception | None = None
    for attempt in range(1, _CONNECT_RETRY_ATTEMPTS + 1):
        try:
            with open(pptx_path, "rb") as f:
                response = await _get_client().post(
                    "/extract-text",
                    files={"file": ("input.pptx", f, "application/octet-stream")},
                    data={"file_id": file_id},
                )
            if response.status_code != 200:
                raise ExtractionResourceError(
                    f"soffice-worker /extract-text returned HTTP {response.status_code}: {response.text[:300]}"
                )
            slides: list[dict] = response.json()
            logger.info(
                "soffice-worker extract-text: file_id=%s slides=%d",
                file_id,
                len(slides),
            )
            return [ExtractedPage(**s) for s in slides]
        except ExtractionResourceError:
            raise
        except (httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            last_exc = exc
            _reset_client()
            if attempt < _CONNECT_RETRY_ATTEMPTS:
                delay = _CONNECT_RETRY_DELAYS[attempt - 1]
                logger.warning(
                    "soffice-worker /extract-text connection failed (attempt %d/%d), retrying in %.0fs: %s",
                    attempt,
                    _CONNECT_RETRY_ATTEMPTS,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
        except Exception as exc:
            raise ExtractionResourceError(f"soffice-worker /extract-text failed: {exc}") from exc
    raise ExtractionResourceError(
        f"soffice-worker /extract-text failed after {_CONNECT_RETRY_ATTEMPTS} attempts: {last_exc}"
    ) from last_exc


async def convert_pdf(pptx_path: str, output_pdf_path: str) -> None:
    """POST a PPTX file to ``/convert-pdf`` and write the returned PDF to disk.

    Retries up to ``_CONNECT_RETRY_ATTEMPTS`` times on connection errors.

    Args:
        pptx_path:       Absolute path to the downloaded PPTX on disk.
        output_pdf_path: Destination path for the converted PDF.

    Raises:
        ExtractionResourceError: on any HTTP or transport failure.
    """
    _check_configured()
    last_exc: Exception | None = None
    for attempt in range(1, _CONNECT_RETRY_ATTEMPTS + 1):
        try:
            with open(pptx_path, "rb") as f:
                response = await _get_client().post(
                    "/convert-pdf",
                    files={"file": ("input.pptx", f, "application/octet-stream")},
                )
            if response.status_code != 200:
                raise ExtractionResourceError(
                    f"soffice-worker /convert-pdf returned HTTP {response.status_code}: {response.text[:300]}"
                )
            with open(output_pdf_path, "wb") as out:
                out.write(response.content)
            logger.info(
                "soffice-worker convert-pdf: %d bytes written to %s",
                len(response.content),
                output_pdf_path,
            )
            return
        except ExtractionResourceError:
            raise
        except (httpx.ConnectError, httpx.RemoteProtocolError) as exc:
            last_exc = exc
            _reset_client()
            if attempt < _CONNECT_RETRY_ATTEMPTS:
                delay = _CONNECT_RETRY_DELAYS[attempt - 1]
                logger.warning(
                    "soffice-worker /convert-pdf connection failed (attempt %d/%d), retrying in %.0fs: %s",
                    attempt,
                    _CONNECT_RETRY_ATTEMPTS,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
        except Exception as exc:
            raise ExtractionResourceError(f"soffice-worker /convert-pdf failed: {exc}") from exc
    raise ExtractionResourceError(
        f"soffice-worker /convert-pdf failed after {_CONNECT_RETRY_ATTEMPTS} attempts: {last_exc}"
    ) from last_exc
