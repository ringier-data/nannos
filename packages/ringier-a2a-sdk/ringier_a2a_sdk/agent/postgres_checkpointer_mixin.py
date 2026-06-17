"""PostgreSQL checkpointer mixin for LangGraph agents.

Provides _create_checkpointer() / _setup_checkpointer() / _teardown_checkpointer()
backed by AsyncPostgresSaver (langgraph-checkpoint-postgres ≥ 3.1).

────────────────────────────────────────────────────────────────────────────────
Environment variables
────────────────────────────────────────────────────────────────────────────────

Connection (CHECKPOINT_POSTGRES_* — separate from the main app POSTGRES_* vars
so the checkpoint DB can live on a different host/user):

    CHECKPOINT_POSTGRES_HOST        Host name or IP.  Required.
                                    When absent the mixin falls back to MemorySaver
                                    (local development only).
    CHECKPOINT_POSTGRES_PORT        Port.  Default: 5432.
    CHECKPOINT_POSTGRES_DB          Database name.  Default: console.
    CHECKPOINT_POSTGRES_USER        Database user.  Default: postgres.
    CHECKPOINT_POSTGRES_PASSWORD    Password.  Required in production.

Tables are created in the public schema (AsyncPostgresSaver v3.x does not
support custom schema names).

The resulting DSN:
    postgresql://<user>:<password>@<host>:<port>/<db>

Required grants for CHECKPOINT_POSTGRES_USER:
    CREATE, SELECT, INSERT, UPDATE, DELETE on all tables in public

Connection pool (psycopg AsyncConnectionPool):
    autocommit=True     Required by AsyncPostgresSaver — implicit per-statement
                        transactions; no explicit BEGIN/COMMIT overhead.
    prepare_threshold=0 Disables server-side prepared statements.
                        Required for PgBouncer transaction-mode compatibility.

────────────────────────────────────────────────────────────────────────────────
Optional S3 offloading
────────────────────────────────────────────────────────────────────────────────

AsyncPostgresSaver v3.x does not expose a built-in storage_mode/S3 parameter.
Offloading is implemented here via a custom serde wrapper (S3OffloadingSerde)
that intercepts blob serialisation transparently:

  • On write: if the serialised blob exceeds CHECKPOINT_S3_THRESHOLD_MB, the
    raw bytes are uploaded to S3 under checkpoints/<uuid> and a compact JSON
    reference {"s3_key": "...", "original_type": "..."} is stored in the DB
    instead (type tag = "s3ref").

  • On read: type tag "s3ref" causes the bytes to be fetched from S3 before
    being passed to the inner deserialiser.

Relevant env vars:
    CHECKPOINT_S3_BUCKET_NAME   S3 bucket name.  When absent, S3 offloading is
                                disabled and all blobs are stored in PostgreSQL.
    CHECKPOINT_S3_THRESHOLD_MB  Blob size threshold in megabytes above which
                                offloading is triggered.  Default: 10.

Note: S3 serialisation/deserialisation is called inside asyncio.to_thread() by
AsyncPostgresSaver, so synchronous boto3 is safe to use here.

────────────────────────────────────────────────────────────────────────────────
TTL (CHECKPOINT_TTL_DAYS)
────────────────────────────────────────────────────────────────────────────────

    CHECKPOINT_TTL_DAYS     Intended retention period in days.  Default: 14.

The LangGraph checkpoints table has no created_at column, so automatic row
expiry is not enforced by this mixin.  Set up a pg_cron or external maintenance
job to periodically DELETE rows that are no longer needed.  This variable is
read and logged at startup so it is visible in configuration audits.

────────────────────────────────────────────────────────────────────────────────
Thread-ID isolation (unchanged from DynamoDB era)
────────────────────────────────────────────────────────────────────────────────

All agents share the same PostgreSQL table.  Isolation is achieved through the
existing thread_id naming convention — no changes required:

    orchestrator  →  {context_id}::orchestrator
    agent-creator →  {context_id}::agent-creator
    alloy-agent   →  {context_id}::alloy-agent
    dynamic agent →  {context_id}::dynamic-{agent_name}

────────────────────────────────────────────────────────────────────────────────
PostgreSQL version requirement
────────────────────────────────────────────────────────────────────────────────

PostgreSQL 11 or later is required.  The ON CONFLICT … DO UPDATE expressions
used in checkpoint_blobs upserts require PG 11+.  The mixin checks the server
version during _setup_checkpointer() and raises RuntimeError with a clear
message on failure.

────────────────────────────────────────────────────────────────────────────────
Example usage
────────────────────────────────────────────────────────────────────────────────

    class MyBedrockAgent(PostgreSQLCheckpointerMixin, LangGraphBedrockAgent):
        def _create_model(self): ...
        def _get_mcp_connections(self): ...

    # FastAPI lifespan:
    await agent.startup()   # opens pool → verifies PG ≥ 11 → runs setup()
    ...
    await agent.shutdown()  # closes pool gracefully
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver

logger = logging.getLogger(__name__)

_MIN_PG_VERSION = 110000  # server_version_num for PostgreSQL 11.0


# ─── S3 offloading serde wrapper ─────────────────────────────────────────────


class S3OffloadingSerde:
    """Serde wrapper that transparently offloads large blobs to S3.

    Wraps the default JsonPlusSerializer.  Blobs whose serialised size exceeds
    ``threshold_bytes`` are uploaded to ``bucket`` and replaced in the DB by a
    compact JSON reference.  Read-path fetches them back from S3 on demand.

    All S3 calls use synchronous boto3 because AsyncPostgresSaver invokes
    dumps_typed / loads_typed inside asyncio.to_thread().
    """

    _S3_TYPE_TAG = "s3ref"

    def __init__(self, bucket: str, threshold_bytes: int) -> None:
        from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

        self._inner = JsonPlusSerializer()
        self._bucket = bucket
        self._threshold = threshold_bytes
        self._s3 = None  # lazy-initialised boto3 client

    def _get_s3(self):
        if self._s3 is None:
            import boto3
            self._s3 = boto3.client("s3")
        return self._s3

    def dumps_typed(self, obj: Any) -> tuple[str, bytes]:
        type_tag, data = self._inner.dumps_typed(obj)
        if len(data) <= self._threshold:
            return type_tag, data

        key = f"checkpoints/{uuid.uuid4()}"
        self._get_s3().put_object(Bucket=self._bucket, Key=key, Body=data)
        reference = json.dumps({"s3_key": key, "original_type": type_tag}).encode()
        logger.debug(
            "Offloaded checkpoint blob to S3 (bucket=%s, key=%s, size=%d bytes)",
            self._bucket,
            key,
            len(data),
        )
        return self._S3_TYPE_TAG, reference

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        type_tag, raw = data
        if type_tag != self._S3_TYPE_TAG:
            return self._inner.loads_typed((type_tag, raw))

        ref = json.loads(raw)
        response = self._get_s3().get_object(Bucket=self._bucket, Key=ref["s3_key"])
        actual_data: bytes = response["Body"].read()
        logger.debug("Fetched checkpoint blob from S3 (key=%s)", ref["s3_key"])
        return self._inner.loads_typed((ref["original_type"], actual_data))


# ─── Version check helper ─────────────────────────────────────────────────────


async def _verify_postgres_version(pool) -> None:
    """Query server_version_num and raise RuntimeError when below PG 11.

    Args:
        pool: An open AsyncConnectionPool instance.

    Raises:
        RuntimeError: When the connected PostgreSQL version is older than 11.
    """
    async with pool.connection() as conn:
        result = await conn.execute("SHOW server_version_num")
        row = await result.fetchone()
        version_num = int(row[0])

    if version_num < _MIN_PG_VERSION:
        major = version_num // 10000
        minor = (version_num % 10000) // 100
        raise RuntimeError(
            f"PostgreSQL {major}.{minor} is not supported for checkpoint storage. "
            f"Upgrade to PostgreSQL 11 or later.  "
            f"(server_version_num={version_num}, minimum required={_MIN_PG_VERSION})"
        )

    major = version_num // 10000
    minor = (version_num % 10000) // 100
    logger.info(
        "PostgreSQL version check passed: %d.%d (server_version_num=%d)",
        major,
        minor,
        version_num,
    )


# ─── Mixin ────────────────────────────────────────────────────────────────────


class PostgreSQLCheckpointerMixin:
    """Mixin that implements _create_checkpointer() using AsyncPostgresSaver.

    _create_checkpointer() is synchronous (called from __init__) and creates the
    saver with a *closed* connection pool.  The pool is opened, the server version
    is verified (≥ PG 11), and the schema is initialised in the async
    _setup_checkpointer() call, which LangGraphAgent.startup() invokes automatically.

    Falls back to MemorySaver when CHECKPOINT_POSTGRES_HOST is not set (local dev).

    See module docstring for full configuration reference.
    """

    def _create_checkpointer(self) -> BaseCheckpointSaver:
        """Store connection pool config and return a MemorySaver placeholder.

        AsyncPostgresSaver.__init__ calls asyncio.get_running_loop() and therefore
        cannot be instantiated in a synchronous __init__.  This method creates the
        AsyncConnectionPool (open=False, safe to construct sync) and stores it on
        self._checkpointer_pool.  _setup_checkpointer() — called from the async
        startup() hook — instantiates AsyncPostgresSaver and replaces self._checkpointer.

        Returns:
            MemorySaver placeholder (replaced by AsyncPostgresSaver at startup), or a
            permanent MemorySaver when CHECKPOINT_POSTGRES_HOST is not set.
        """
        from langgraph.checkpoint.memory import MemorySaver

        host = os.getenv("CHECKPOINT_POSTGRES_HOST")
        if not host:
            logger.warning(
                "CHECKPOINT_POSTGRES_HOST not set — using in-memory checkpointer.  "
                "Conversation history will be lost on restart."
            )
            self._checkpointer_pool = None
            return MemorySaver()

        from psycopg_pool import AsyncConnectionPool

        port = os.getenv("CHECKPOINT_POSTGRES_PORT", "5432")
        db = os.getenv("CHECKPOINT_POSTGRES_DB", "checkpointer")
        user = os.getenv("CHECKPOINT_POSTGRES_USER", "postgres")
        password = os.getenv("CHECKPOINT_POSTGRES_PASSWORD", "")

        ttl_days = int(os.getenv("CHECKPOINT_TTL_DAYS", "14"))
        logger.info(
            "CHECKPOINT_TTL_DAYS=%d (note: automatic TTL requires a separate "
            "pg_cron / maintenance job — not enforced by this mixin)",
            ttl_days,
        )

        conn_string = f"postgresql://{user}:{password}@{host}:{port}/{db}"

        # autocommit=True  — required by AsyncPostgresSaver
        # prepare_threshold=0 — disables server-side prepared statements (PgBouncer compat)
        pool = AsyncConnectionPool(
            conninfo=conn_string,
            open=False,
            kwargs={"autocommit": True, "prepare_threshold": 0},
        )
        self._checkpointer_pool = pool

        logger.info(
            "Prepared PostgreSQL checkpointer pool (host=%s, db=%s) — "
            "AsyncPostgresSaver will be created in _setup_checkpointer()",
            host,
            db,
        )
        # Placeholder: replaced by AsyncPostgresSaver in _setup_checkpointer()
        return MemorySaver()

    async def _setup_checkpointer(self) -> None:
        """Instantiate AsyncPostgresSaver, open pool, verify PG ≥ 11, run migrations.

        Called automatically by LangGraphAgent.startup().  Replaces the MemorySaver
        placeholder in self._checkpointer with the real AsyncPostgresSaver.

        Raises:
            RuntimeError: When the connected PostgreSQL server is older than PG 11.
        """
        pool = getattr(self, "_checkpointer_pool", None)
        if pool is None:
            return  # permanent MemorySaver — nothing to do

        if not getattr(pool, "_opened", False):
            await pool.open()
            logger.info("Opened AsyncConnectionPool for checkpoint store")

        await _verify_postgres_version(pool)

        serde = _build_serde()

        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        checkpointer = AsyncPostgresSaver(pool, serde=serde)
        await checkpointer.setup()

        # Swap placeholder with the real checkpointer before any requests are served
        self._checkpointer = checkpointer
        logger.info(
            "PostgreSQL checkpointer ready (tables in public schema, s3_offload=%s)", bool(serde)
        )

    async def _teardown_checkpointer(self) -> None:
        """Close the connection pool on shutdown."""
        pool = getattr(self, "_checkpointer_pool", None)
        if pool is not None and getattr(pool, "_opened", False):
            await pool.close()
            logger.info("Closed checkpoint connection pool")


# ─── Internal helpers ─────────────────────────────────────────────────────────


def _build_serde() -> S3OffloadingSerde | None:
    """Return an S3OffloadingSerde when CHECKPOINT_S3_BUCKET_NAME is set, else None."""
    bucket = os.getenv("CHECKPOINT_S3_BUCKET_NAME")
    if not bucket:
        return None

    threshold_mb = float(os.getenv("CHECKPOINT_S3_THRESHOLD_MB", "10"))
    threshold_bytes = int(threshold_mb * 1024 * 1024)
    logger.info(
        "S3 checkpoint offloading enabled (bucket=%s, threshold=%.1f MB)",
        bucket,
        threshold_mb,
    )
    return S3OffloadingSerde(bucket=bucket, threshold_bytes=threshold_bytes)
