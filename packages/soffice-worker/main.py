"""soffice-worker: isolated LibreOffice conversion service.

Exposes two endpoints:
  POST /extract-text  — PPTX → ODP → per-slide JSON (text extraction)
  POST /convert-pdf   — PPTX → PDF bytes (thumbnail pipeline)
  GET  /health        — readiness probe

All soffice operations run inside this pod's cgroup, fully isolated from the
catalog-worker. A single asyncio.Semaphore serialises conversions — soffice
itself is single-threaded and the isolation benefit requires that at most one
conversion runs at a time per replica.

Each soffice subprocess receives a unique HOME directory to avoid the
~/.config/libreoffice profile lock that causes silent serialisation / crashes
when two soffice processes share the same HOME.

Request bodies are streamed straight to a tmpfile — never buffered in the
Python heap — so a 100 MB PPTX does not spike the service's RSS before even
calling soffice.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("soffice_worker")

# One conversion at a time per replica.  Raise SOFFICE_CONCURRENCY to allow
# parallel conversions (requires more RAM in the pod limit).
_CONCURRENCY = int(os.environ.get("SOFFICE_CONCURRENCY", "1"))
_SEMAPHORE_TIMEOUT = float(os.environ.get("SOFFICE_SEMAPHORE_TIMEOUT_S", "150"))
_sema: asyncio.Semaphore | None = None

_ODP_NS = {
    "draw":         "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "text":         "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "presentation": "urn:oasis:names:tc:opendocument:xmlns:presentation:1.0",
    "office":       "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _sema
    _sema = asyncio.Semaphore(_CONCURRENCY)
    soffice = _find_soffice()
    if not soffice:
        logger.error("LibreOffice (soffice) binary not found — conversions will fail")
    else:
        logger.info("soffice found at %s; concurrency=%d", soffice, _CONCURRENCY)
    yield


app = FastAPI(title="soffice-worker", version="0.1.0", lifespan=lifespan)


# ─── soffice discovery ───────────────────────────────────────────────────────

_SOFFICE_PATH: str | None = None
_SOFFICE_CHECKED: bool = False


def _find_soffice() -> str | None:
    global _SOFFICE_PATH, _SOFFICE_CHECKED
    if _SOFFICE_CHECKED:
        return _SOFFICE_PATH
    _SOFFICE_PATH = shutil.which("soffice") or shutil.which("libreoffice")
    _SOFFICE_CHECKED = True
    return _SOFFICE_PATH


# ─── Helpers ─────────────────────────────────────────────────────────────────

async def _acquire_semaphore() -> None:
    """Acquire the concurrency semaphore, raising 503 on timeout."""
    try:
        await asyncio.wait_for(_sema.acquire(), timeout=_SEMAPHORE_TIMEOUT)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=503,
            detail=f"soffice-worker busy — could not acquire slot within {_SEMAPHORE_TIMEOUT:.0f}s",
        )


async def _stream_upload_to_file(upload: UploadFile, dest: str) -> None:
    """Stream multipart upload body to *dest* without buffering in heap."""
    with open(dest, "wb") as f:
        while True:
            chunk = await upload.read(65536)
            if not chunk:
                break
            f.write(chunk)


def _run_soffice(args: list[str], tmpdir: str) -> None:
    """Run soffice with an isolated HOME to avoid profile lock contention."""
    home_dir = os.path.join(tmpdir, "home")
    os.makedirs(home_dir, exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = home_dir

    result = subprocess.run(
        args,
        capture_output=True,
        timeout=120,
        env=env,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")[:600]
        raise RuntimeError(f"soffice exited {result.returncode}: {stderr}")


# ─── ODP parser (stdlib only — no lxml) ──────────────────────────────────────

def _text_of(element: ET.Element) -> str:
    parts: list[str] = []
    for p in element.iter(f"{{{_ODP_NS['text']}}}p"):
        t = "".join(p.itertext()).strip()
        if t:
            parts.append(t)
    return "\n".join(parts)


def _parse_odp(odp_path: str, file_id: str) -> list[dict[str, Any]]:
    with zipfile.ZipFile(odp_path) as zf:
        with zf.open("content.xml") as f:
            tree = ET.parse(f)

    root = tree.getroot()
    body = root.find(f"{{{_ODP_NS['office']}}}body")
    if body is None:
        return []
    presentation_el = body.find(f"{{{_ODP_NS['office']}}}presentation")
    if presentation_el is None:
        return []

    slides: list[dict[str, Any]] = []
    visible_idx = 0
    total_pages = 0

    for slide in presentation_el.findall(f"{{{_ODP_NS['draw']}}}page"):
        total_pages += 1
        if slide.get(f"{{{_ODP_NS['presentation']}}}visibility") == "hidden":
            continue
        visible_idx += 1

        title = ""
        body_parts: list[str] = []

        for frame in slide.findall(f"{{{_ODP_NS['draw']}}}frame"):
            pclass = frame.get(f"{{{_ODP_NS['presentation']}}}class", "")
            text = _text_of(frame)
            if not text:
                continue
            if pclass == "title":
                title = text
            else:
                body_parts.append(text)

        notes = ""
        notes_el = slide.find(f"{{{_ODP_NS['presentation']}}}notes")
        if notes_el is not None:
            notes = _text_of(notes_el)

        slides.append({
            "page_number": visible_idx,
            "title": title or f"Slide {visible_idx}",
            "text_content": "\n\n".join(body_parts),
            "speaker_notes": notes,
            "source_ref": {
                "type": "pptx",
                "file_id": file_id,
                "slide_index": visible_idx - 1,
            },
        })

    return slides


# ─── Endpoints ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/extract-text")
async def extract_text(file: UploadFile, file_id: str = Form("")) -> JSONResponse:
    """PPTX → ODP → per-slide JSON.

    Accepts ``multipart/form-data`` with:
      - ``file``    — the PPTX binary
      - ``file_id`` — opaque identifier embedded in ``source_ref``

    Returns a JSON array of slide dicts.
    """
    soffice = _find_soffice()
    if not soffice:
        raise HTTPException(status_code=503, detail="LibreOffice not available on this pod")

    await _acquire_semaphore()
    tmpdir = tempfile.mkdtemp(prefix="soffice-ext-")
    try:
        pptx_path = os.path.join(tmpdir, "input.pptx")
        await _stream_upload_to_file(file, pptx_path)

        _run_soffice(
            [soffice, "--headless", "--norestore", "--convert-to", "odp", "--outdir", tmpdir, pptx_path],
            tmpdir,
        )

        base = os.path.splitext(os.path.basename(pptx_path))[0]
        odp_path = os.path.join(tmpdir, f"{base}.odp")
        if not os.path.exists(odp_path):
            raise HTTPException(status_code=422, detail="soffice did not produce ODP output")

        slides = _parse_odp(odp_path, file_id)
        logger.info("extract-text: file_id=%s slides=%d", file_id, len(slides))
        return JSONResponse(content=slides)

    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        logger.error("extract-text failed: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc))
    finally:
        _sema.release()
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.post("/convert-pdf")
async def convert_pdf(file: UploadFile) -> Response:
    """PPTX → PDF bytes.

    Accepts ``multipart/form-data`` with:
      - ``file`` — the PPTX binary

    Returns ``application/pdf`` bytes.
    """
    soffice = _find_soffice()
    if not soffice:
        raise HTTPException(status_code=503, detail="LibreOffice not available on this pod")

    await _acquire_semaphore()
    tmpdir = tempfile.mkdtemp(prefix="soffice-pdf-")
    try:
        pptx_path = os.path.join(tmpdir, "input.pptx")
        await _stream_upload_to_file(file, pptx_path)

        _run_soffice(
            [soffice, "--headless", "--norestore", "--convert-to", "pdf", "--outdir", tmpdir, pptx_path],
            tmpdir,
        )

        base = os.path.splitext(os.path.basename(pptx_path))[0]
        pdf_path = os.path.join(tmpdir, f"{base}.pdf")
        if not os.path.exists(pdf_path):
            raise HTTPException(status_code=422, detail="soffice did not produce PDF output")

        with open(pdf_path, "rb") as f:
            pdf_bytes = f.read()

        logger.info("convert-pdf: size=%d bytes", len(pdf_bytes))
        return Response(content=pdf_bytes, media_type="application/pdf")

    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        logger.error("convert-pdf failed: %s", exc)
        raise HTTPException(status_code=422, detail=str(exc))
    finally:
        _sema.release()
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
