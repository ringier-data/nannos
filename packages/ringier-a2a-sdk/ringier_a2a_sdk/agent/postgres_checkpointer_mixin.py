"""PostgreSQL checkpointer mixin for LangGraph agents.

Provides _create_checkpointer() / _setup_checkpointer() / _teardown_checkpointer()
backed by AsyncPostgresSaver (langgraph-checkpoint-postgres ≥ 3.1).

────────────────────────────────────────────────────────────────────────────────
Environment variables
────────────────────────────────────────────────────────────────────────────────

Connection — the checkpointer reuses the service's main POSTGRES_* connection
(same database/user as the document store and A2A task store), following the
repo convention of one shared DB with a per-service user + schema:

    POSTGRES_HOST        Host name or IP.  Gates persistence.
                         When absent the mixin falls back to MemorySaver.
    POSTGRES_PORT        Port.  Default: 5432.
    POSTGRES_DB          Database name.  Default: postgres.
    POSTGRES_USER        Database user.  Default: postgres.
    POSTGRES_PASSWORD    Password.
    POSTGRES_SCHEMA      Schema the checkpoint tables live in.  Optional; when set,
                         the connection search_path is pinned to "<schema>,public".

When POSTGRES_HOST is absent the mixin falls back to MemorySaver, but only in
local development: the fallback is allowed when ENVIRONMENT is unset/"local" or
CHECKPOINT_ALLOW_MEMORY is truthy.  In any deployed environment a missing host is
treated as a misconfiguration and raises RuntimeError rather than silently losing
conversation history on restart.

Schema placement: AsyncPostgresSaver's MIGRATIONS use *unqualified* table names,
so the checkpoint tables land wherever the connection's search_path points — NOT a
hard-coded "public".  Setting POSTGRES_SCHEMA (or a role-level
``ALTER USER … SET search_path``) places them in the service's own schema, so
AsyncPostgresSaver.setup() — itself a versioned, incremental migration runner —
fully owns the checkpoint schema lifecycle.  No separate Rambler migration is
needed as long as the connecting user owns (has DDL on) that schema.

Required grants for POSTGRES_USER on its schema:
    CREATE, SELECT, INSERT, UPDATE, DELETE  (schema owner satisfies all of these)

Connection pool (psycopg AsyncConnectionPool):
    autocommit=True     Required by AsyncPostgresSaver — implicit per-statement
                        transactions; no explicit BEGIN/COMMIT overhead.
    prepare_threshold=0 Disables server-side prepared statements.
                        Required for PgBouncer transaction-mode compatibility.

    CHECKPOINT_POSTGRES_POOL_MIN_SIZE  Minimum idle connections.  Default: 1.
    CHECKPOINT_POSTGRES_POOL_MAX_SIZE  Maximum pool size.  Default: 10.

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
                                offloading is triggered.  Default: 1.

Lifecycle / cleanup: offloaded objects are NOT deleted automatically.  The serde runs
below LangGraph's checkpoint lifecycle and cannot observe row/thread deletion, so
overwrites and TTL cleanups leave orphaned objects behind.  Configure an S3 lifecycle
policy on the ``checkpoints/`` prefix to expire them (pair its expiry with
CHECKPOINT_TTL_DAYS).  Disabling offloading (unsetting CHECKPOINT_S3_BUCKET_NAME) while
``s3ref`` rows still exist will make those rows fail to load — keep the bucket
configured until such rows have aged out.

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

Agents that share a schema also share the checkpoint tables; isolation is achieved
through the existing thread_id naming convention — no changes required:

    orchestrator  →  {context_id}::orchestrator
    voice-agent   →  {context_id}::voice-agent
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
from typing import TYPE_CHECKING, Any

from langgraph.checkpoint.base import BaseCheckpointSaver

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

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

        try:
            ref = json.loads(raw)
            s3_key = ref["s3_key"]
            original_type = ref["original_type"]
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(
                f"Corrupt S3 checkpoint reference (type tag={self._S3_TYPE_TAG!r}): {exc}. "
                "The DB row points at an offloaded blob but the reference cannot be parsed."
            ) from exc

        try:
            response = self._get_s3().get_object(Bucket=self._bucket, Key=s3_key)
            actual_data: bytes = response["Body"].read()
        except Exception as exc:
            raise RuntimeError(
                f"Failed to fetch offloaded checkpoint blob from S3 "
                f"(bucket={self._bucket}, key={s3_key}): {exc}. The blob may have been "
                "removed by an S3 lifecycle policy or manual cleanup, or "
                "CHECKPOINT_S3_BUCKET_NAME may point at the wrong bucket."
            ) from exc

        logger.debug("Fetched checkpoint blob from S3 (key=%s)", s3_key)
        return self._inner.loads_typed((original_type, actual_data))


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
        # The pool uses dict_row (rows keyed by column name), so row[0] would KeyError.
        # Read the single value regardless of the connection's row factory.
        raw = next(iter(row.values())) if isinstance(row, dict) else row[0]
        version_num = int(raw)

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

    Falls back to MemorySaver when POSTGRES_HOST is not set (local dev).

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
            permanent MemorySaver when POSTGRES_HOST is not set.
        """
        from langgraph.checkpoint.memory import MemorySaver

        host = os.getenv("POSTGRES_HOST")
        if not host:
            if not memory_fallback_allowed():
                raise missing_host_error()
            logger.warning(
                "POSTGRES_HOST not set — using in-memory checkpointer.  Conversation history will be lost on restart."
            )
            self._checkpointer_pool = None
            return MemorySaver()

        port = os.getenv("POSTGRES_PORT", "5432")
        db = os.getenv("POSTGRES_DB", "postgres")
        user = os.getenv("POSTGRES_USER", "postgres")
        password = os.getenv("POSTGRES_PASSWORD", "")
        schema = os.getenv("POSTGRES_SCHEMA")

        ttl_days = int(os.getenv("CHECKPOINT_TTL_DAYS", "14"))
        logger.info(
            "CHECKPOINT_TTL_DAYS=%d (note: automatic TTL requires a separate "
            "pg_cron / maintenance job — not enforced by this mixin)",
            ttl_days,
        )

        pool = build_checkpointer_pool(host=host, port=port, db=db, user=user, password=password, schema=schema)
        self._checkpointer_pool = pool

        logger.info(
            "Prepared PostgreSQL checkpointer pool (host=%s, db=%s, schema=%s) — "
            "AsyncPostgresSaver will be created in _setup_checkpointer()",
            host,
            db,
            schema or "<role default>",
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

        await open_pool_if_closed(pool)
        logger.info("Checkpoint connection pool open")

        await _verify_postgres_version(pool)

        serde = _build_serde()

        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        checkpointer = AsyncPostgresSaver(pool, serde=serde)
        await checkpointer.setup()

        # Swap placeholder with the real checkpointer before any requests are served
        self._checkpointer = checkpointer
        logger.info(
            "PostgreSQL checkpointer ready (tables in the connection's search_path schema, s3_offload=%s)",
            bool(serde),
        )

    async def _teardown_checkpointer(self) -> None:
        """Close the connection pool on shutdown."""
        pool = getattr(self, "_checkpointer_pool", None)
        if pool is not None:
            await close_pool_if_open(pool)
            logger.info("Closed checkpoint connection pool")


# ─── Internal helpers ─────────────────────────────────────────────────────────


def _build_serde() -> S3OffloadingSerde | None:
    """Return an S3OffloadingSerde when CHECKPOINT_S3_BUCKET_NAME is set, else None."""
    bucket = os.getenv("CHECKPOINT_S3_BUCKET_NAME")
    if not bucket:
        return None

    threshold_mb = float(os.getenv("CHECKPOINT_S3_THRESHOLD_MB", "1"))
    threshold_bytes = int(threshold_mb * 1024 * 1024)
    logger.info(
        "S3 checkpoint offloading enabled (bucket=%s, threshold=%.1f MB)",
        bucket,
        threshold_mb,
    )
    return S3OffloadingSerde(bucket=bucket, threshold_bytes=threshold_bytes)


# ─── Shared pool / lifecycle helpers (reused by graph_factory and agent-runner) ──


def memory_fallback_allowed() -> bool:
    """Whether an in-memory checkpointer is acceptable when no Postgres host is set.

    Allowed only in local development: when CHECKPOINT_ALLOW_MEMORY is explicitly
    truthy, or when ENVIRONMENT is unset/"local".  In any deployed environment a
    missing POSTGRES_HOST is a misconfiguration that must fail fast rather than
    silently drop conversation history on restart.
    """
    if os.getenv("CHECKPOINT_ALLOW_MEMORY", "").strip().lower() in ("1", "true", "yes"):
        return True
    return os.getenv("ENVIRONMENT", "local").strip().lower() == "local"


def missing_host_error() -> RuntimeError:
    """Build the fail-fast error raised when POSTGRES_HOST is required but unset."""
    return RuntimeError(
        f"POSTGRES_HOST is not set but ENVIRONMENT="
        f"{os.getenv('ENVIRONMENT', 'local')!r} requires a persistent checkpoint store. "
        "Set POSTGRES_HOST, or set CHECKPOINT_ALLOW_MEMORY=true to explicitly opt into "
        "the in-memory checkpointer (development only — conversation history is lost on "
        "every restart)."
    )


def pool_sizes() -> tuple[int, int]:
    """Return (min_size, max_size) for the checkpoint pool from env (defaults 1/10)."""
    min_size = int(os.getenv("CHECKPOINT_POSTGRES_POOL_MIN_SIZE", "1"))
    max_size = int(os.getenv("CHECKPOINT_POSTGRES_POOL_MAX_SIZE", "10"))
    return min_size, max_size


def build_checkpointer_pool(
    *,
    host: str,
    port: str,
    db: str,
    user: str,
    password: str,
    schema: str | None = None,
) -> AsyncConnectionPool:
    """Create a closed AsyncConnectionPool for the checkpoint store.

    The DSN is assembled with psycopg.conninfo.make_conninfo so special characters in
    the user/password (``@ : / ? #`` — common in generated secrets) are escaped
    correctly instead of corrupting an f-string URL.  The pool is explicitly sized
    (psycopg's default min_size=4 would hold idle connections per process) and created
    with open=False so it can be constructed synchronously.  Connection kwargs match
    AsyncPostgresSaver.from_conn_string (autocommit, prepare_threshold=0, dict_row).

    When ``schema`` is given the connection search_path is pinned to
    ``<schema>,public`` so AsyncPostgresSaver's unqualified DDL creates the checkpoint
    tables in the service's own schema rather than public.
    """
    from psycopg.conninfo import make_conninfo
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    conninfo = make_conninfo(host=host, port=port, dbname=db, user=user, password=password)
    conn_kwargs: dict[str, Any] = {
        "autocommit": True,
        "prepare_threshold": 0,
        "row_factory": dict_row,
    }
    if schema:
        conn_kwargs["options"] = f"-c search_path={schema},public"
    min_size, max_size = pool_sizes()
    return AsyncConnectionPool(
        conninfo=conninfo,
        open=False,
        min_size=min_size,
        max_size=max_size,
        kwargs=conn_kwargs,
    )


async def open_pool_if_closed(pool) -> None:
    """Open the pool only when closed (uses the public ``closed`` property).

    psycopg_pool.close() sets ``closed`` back to True but never resets the private
    ``_opened`` flag, so guarding on ``_opened`` would skip re-opening a pool that was
    torn down and set up again.  ``closed`` reflects the real state across cycles.
    """
    if pool.closed:
        await pool.open()


async def close_pool_if_open(pool) -> None:
    """Close the pool only when it is currently open."""
    if not pool.closed:
        await pool.close()
