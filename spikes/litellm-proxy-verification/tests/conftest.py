"""Shared fixtures/helpers for the gateway spike. Throwaway."""

import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import pytest
from openai import AsyncOpenAI

# Make the spike root importable (attribution_hook.py lives there).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PROXY_URL = "http://localhost:4000"
MASTER_KEY = "sk-spike-master"
CAPTURE_PATH = Path(__file__).resolve().parent.parent / "captured" / "events.jsonl"


@pytest.fixture
def aclient() -> AsyncOpenAI:
    """OpenAI SDK pointed at the proxy (master key).

    max_retries=0: the SDK otherwise retries 5xx twice, so a clean 5s proxy
    stream_timeout reads as ~16s of stacked attempts.
    """
    return AsyncOpenAI(base_url=PROXY_URL, api_key=MASTER_KEY, max_retries=0)


def clear_captures() -> None:
    CAPTURE_PATH.parent.mkdir(exist_ok=True)
    CAPTURE_PATH.write_text("")


def read_captures() -> list[dict]:
    if not CAPTURE_PATH.exists():
        return []
    return [json.loads(line) for line in CAPTURE_PATH.read_text().splitlines() if line.strip()]


async def wait_for_captures(count: int, timeout: float = 30.0) -> list[dict]:
    """Poll the capture file until at least `count` records appear."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        recs = read_captures()
        if len(recs) >= count:
            return recs
        await asyncio.sleep(0.25)
    return read_captures()


async def fetch_spend_logs(timeout: float = 20.0) -> list[dict]:
    """Read persisted SpendLogs via the proxy management API (reads the DB table)."""
    async with httpx.AsyncClient(timeout=timeout) as c:
        r = await c.get(f"{PROXY_URL}/spend/logs", headers={"Authorization": f"Bearer {MASTER_KEY}"})
        r.raise_for_status()
        return r.json()


def _psql(sql: str) -> str:
    """Query the SpendLogs Postgres directly (definitive persistence check)."""
    import subprocess

    out = subprocess.run(
        ["docker", "compose", "exec", "-T", "postgres", "psql", "-U", "litellm", "-d", "litellm", "-tAc", sql],
        cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True,
        text=True,
        timeout=30,
    )
    return out.stdout.strip()


async def wait_for_persisted_user_sub(user_sub: str, timeout: float = 40.0) -> bool:
    """Poll Postgres until a SpendLogs row carries spend_logs_metadata.user_sub == user_sub.

    SpendLogs are batch-flushed, so this can lag the in-memory CustomLogger capture.
    """
    sql = (
        "select count(*) from \"LiteLLM_SpendLogs\" "
        f"where metadata->'spend_logs_metadata'->>'user_sub' = '{user_sub}';"
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if (_psql(sql) or "0").isdigit() and int(_psql(sql)) > 0:
                return True
        except Exception:
            pass
        await asyncio.sleep(2)
    return False


@pytest.fixture
def captures():
    """Clear capture file before a test; expose the helpers."""
    clear_captures()

    class _C:
        clear = staticmethod(clear_captures)
        read = staticmethod(read_captures)
        wait = staticmethod(wait_for_captures)
        spend_logs = staticmethod(fetch_spend_logs)
        wait_persisted = staticmethod(wait_for_persisted_user_sub)

    return _C()
