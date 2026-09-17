"""MCP tool discovery for scheduled runs: shared catalogue + call-time bearer tokens.

Before this module the runner exchanged the user's token once per audience, baked the
results into ``StreamableHttpConnection.headers`` and ran ``MultiServerMCPClient.get_tools()``
over the whole gateway — a full SDK parse of ~500 tools into pydantic objects on every run,
with credentials frozen into every tool at discovery time.

Now a run has the same shape as an orchestrator sub-agent (see ``agent_common``):

* **Catalogue** — each server the whitelist needs is listed with a stateless JSON-RPC
  ``tools/list`` (:func:`fetch_catalogue_stateless`, no handshake, no pydantic) and the SDK
  session (:func:`fetch_catalogue_mcp`) only as fallback. Either way the result is flattened
  to bytes; no ``mcp.types.Tool`` survives discovery.
* **Tools** — :class:`LazyMcpTool` per whitelisted name, so only the tools the run binds
  pay for schema decoding.
* **Credentials** — a per-run :class:`UserTokenProvider`; tool connections carry no
  ``Authorization`` header, :func:`bearer_interceptor` mints one per call (memoised until
  ``MCP_TOKEN_LEEWAY_SECONDS`` before ``exp``), so a token expiring mid-run is re-minted.

"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Iterable
from datetime import timedelta
from typing import Any

import httpx
from agent_common.agents.dynamic_agent import is_console_backend_tool
from agent_common.core.catalogue_ingest import fetch_catalogue
from agent_common.core.token_provider import UserTokenProvider, bearer_interceptor
from agent_common.core.tool_catalogue import ServerCatalogue, make_lazy_tool
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from ringier_a2a_sdk.utils.mcp_errors import is_retryable_mcp_error

logger = logging.getLogger(__name__)

#: How many times a ``tools/list`` is attempted before the run gives up on a server.
#: Three matches the orchestrator. Past that a gateway is not blipping, it is down, and
#: a scheduled run waiting longer only delays the report.
_LIST_MAX_ATTEMPTS = 3

#: First backoff, doubled per attempt (1s, 2s). Short on purpose: this sits on the run's
#: critical path, and the errors it covers — a rolling gateway deploy, a pod restart —
#: resolve in seconds or not at all.
_RETRY_BASE_DELAY_SECONDS = 1.0

#: How many ``tools/list`` fetches this PROCESS may hold open at once, across all runs.
#:
#: A single run only ever lists two servers, so this is not about one run's fan-out. It is
#: about the process: the scheduler claims up to ``claim_limit`` jobs per tick and
#: dispatches them concurrently, so the runs overlap, and agent-runner is the service that
#: gets OOMKilled. Listing the two servers concurrently doubled what one run holds open at
#: the peak (1 → 2), which is a good trade for latency only if the total stays bounded.
#:
#: Same env var and default as the orchestrator, which learned this the hard way: an
#: unbounded fan-out reached ~31 concurrent fetches and OOMKilled the pod (2026-08-15).
#: Process-wide rather than per-run for the reason given there — a per-call limit still
#: allows ``limit x N``.
_DISCOVERY_CONCURRENCY = max(1, int(os.getenv("MCP_DISCOVERY_CONCURRENCY", "5")))

#: Created lazily: a module-level ``asyncio.Semaphore`` would bind to whatever loop
#: imported this module, which is not necessarily the one serving requests.
_DISCOVERY_SEMAPHORE: asyncio.Semaphore | None = None


def _discovery_semaphore() -> asyncio.Semaphore:
    """The process-wide cap on concurrent listings. See :data:`_DISCOVERY_CONCURRENCY`."""
    global _DISCOVERY_SEMAPHORE
    if _DISCOVERY_SEMAPHORE is None:
        _DISCOVERY_SEMAPHORE = asyncio.Semaphore(_DISCOVERY_CONCURRENCY)
    return _DISCOVERY_SEMAPHORE


GATEWAY_SERVER = "gateway"
CONSOLE_SERVER = "console"

# Console-backend tools (``console_*``, ``scheduler_*``) go to the console MCP; the rest to the gateway.
is_console_tool = is_console_backend_tool

# A ``tools/list`` is a per-user view (a Gatana profile can hide tools of a server from one
# user and not another), so every run lists with its own token; nothing is shared between runs
# except the per-URL "does this endpoint serve stateless requests" memo in catalogue_ingest.


class McpToolResolver:
    """Resolve a scheduled run's tool whitelist to token-free :class:`LazyMcpTool` instances.

    One instance per run: it owns that run's :class:`UserTokenProvider` and the two
    possible connections (gateway, console).
    """

    def __init__(
        self,
        *,
        token_provider: UserTokenProvider,
        gateway_url: str,
        gateway_client_id: str,
        console_mcp_url: str,
        console_client_id: str,
        timeout: timedelta,
        stateless_list: bool = True,
    ) -> None:
        self.token_provider = token_provider
        self.gateway_client_id = gateway_client_id
        self.console_client_id = console_client_id
        self.stateless_list = stateless_list
        self._urls = {GATEWAY_SERVER: gateway_url, CONSOLE_SERVER: console_mcp_url}
        self._timeout = timeout
        # Discovery statistics for the last resolve(); logged and surfaced for measurements.
        self.stats: dict[str, Any] = {}

    # -- audiences / connections -------------------------------------------------------
    def audience_for(self, server_name: str) -> str:
        return self.console_client_id if server_name == CONSOLE_SERVER else self.gateway_client_id

    def _connection(self, server_name: str, *, bearer: str | None = None) -> StreamableHttpConnection:
        """A connection for ``server_name``; token-free unless ``bearer`` is given (listing only)."""
        connection = StreamableHttpConnection(
            transport="streamable_http",
            url=self._urls[server_name],
            timeout=self._timeout,
            sse_read_timeout=self._timeout,
        )
        if bearer is not None:
            connection["headers"] = {"Authorization": f"Bearer {bearer}"}
        return connection

    # -- listing -------------------------------------------------------------------------
    async def _list_server_with_retry(
        self, server_name: str, http_client: httpx.AsyncClient
    ) -> ServerCatalogue:
        """``_list_server`` with exponential backoff on transient gateway errors.

        Mirrors the orchestrator's ``_get_catalogue_with_retry``: retried only when
        ``is_retryable_mcp_error`` says the failure is transient (a 502/503/504 or a
        timeout, unwrapped from the anyio ``ExceptionGroup`` the MCP client raises), and
        a 4xx or a refused connection fails immediately — retrying a rejected token just
        delays the auth error the run needs to report.

        The unit retried is the *whole* listing of one server, not the stateless POST
        alone: ``fetch_catalogue`` already answers a failed stateless attempt by falling
        back to the SDK session and only raises once both have failed. So a gateway 503
        reaches here as the SDK path's error, and one attempt here is one full
        stateless-then-SDK cycle.

        Without this, a scheduled run had no tolerance at all for a gateway that was
        briefly unreachable — and because nobody is watching a scheduled run, that
        arrived as the job's failure rather than as the infrastructure's.
        """
        delay = _RETRY_BASE_DELAY_SECONDS
        for attempt in range(_LIST_MAX_ATTEMPTS):
            try:
                # The slot covers the fetch only, never the backoff sleep below: holding it
                # while waiting would let one flaky server idle away a slot that a healthy
                # listing — possibly another run's — could be using.
                async with _discovery_semaphore():
                    catalogue = await self._list_server(server_name, http_client)
                if attempt:
                    logger.info(
                        "Listed %s on attempt %d/%d", server_name, attempt + 1, _LIST_MAX_ATTEMPTS
                    )
                return catalogue
            except Exception as exc:
                last = attempt >= _LIST_MAX_ATTEMPTS - 1
                if last or not is_retryable_mcp_error(exc):
                    raise
                logger.warning(
                    "tools/list for %s failed (attempt %d/%d), retrying in %.1fs: %s",
                    server_name,
                    attempt + 1,
                    _LIST_MAX_ATTEMPTS,
                    delay,
                    exc,
                )
                await asyncio.sleep(delay)
                delay *= 2
        raise AssertionError("unreachable")  # pragma: no cover - loop either returns or raises

    async def _list_server(self, server_name: str, http_client: httpx.AsyncClient) -> ServerCatalogue:
        """``tools/list`` for one server via ``catalogue_ingest.fetch_catalogue`` (stateless → SDK).

        The listing itself needs a bearer (the gateway filters the catalogue per user), so
        it asks the provider for one — the only place discovery touches a token.
        """
        bearer = await self.token_provider.get(self.audience_for(server_name))
        client = MultiServerMCPClient({server_name: self._connection(server_name, bearer=bearer)})
        catalogue = await fetch_catalogue(
            server_slug=server_name,
            url=self._urls[server_name],
            headers={"Authorization": f"Bearer {bearer}"},
            http_client=http_client,
            session_factory=lambda: client.session(server_name),
            stateless=self.stateless_list,
        )
        self.stats["source"][server_name] = catalogue.source
        return catalogue

    # -- resolution ----------------------------------------------------------------------
    async def resolve_all(self) -> list[BaseTool]:
        """Every tool this user is offered, across both servers.

        What an EMPTY whitelist means for a full-catalogue agent. The general-purpose
        agent is configured with no tool list at all, and in a conversation that means
        "everything" — the orchestrator hands it the whole registry as a lazy catalog.
        A scheduled run of the same agent used to read the empty list as "nothing" and
        run with no MCP tools whatsoever, so the agent would report that the tools it
        was asked to use do not exist. Same agent, same configuration, opposite
        capability depending on who started it.

        Returned lazily (:class:`LazyMcpTool`), so nothing decodes a schema until it is
        called: the caller passes these as a *catalog* the model searches, never as
        tools bound into the graph — binding a whole gateway is what OOM-killed the
        orchestrator before catalog mode existed.
        """
        return await self._resolve(None)

    async def resolve(self, wanted: Iterable[str]) -> list[BaseTool]:
        """Tools for ``wanted`` names: one ``tools/list`` per server the whitelist needs.

        Always listed with this run's own token, so the run only ever binds tools this user
        is offered; names no server lists are logged and skipped (the whitelist filter the
        runner always applied).
        """
        return await self._resolve(set(wanted))

    async def _resolve(self, wanted: set[str] | None) -> list[BaseTool]:
        """Shared listing. ``wanted is None`` means "everything this user is offered"."""
        started = time.monotonic()
        names = wanted if wanted is not None else set()
        list_everything = wanted is None
        self.stats = {"source": {}}
        interceptors = [bearer_interceptor(self.token_provider, self.audience_for)]
        connections = {
            server: self._connection(server)
            for server, predicate in (
                (GATEWAY_SERVER, lambda n: not is_console_tool(n)),
                (CONSOLE_SERVER, is_console_tool),
            )
            if list_everything or any(predicate(n) for n in names)
        }

        def _server_for(name: str) -> str:
            return CONSOLE_SERVER if is_console_tool(name) else GATEWAY_SERVER

        # Exchange up front for every audience the run needs: discovery used to do this, and
        # a user token that is expired or revoked must fail the run here, not surface as a
        # run that "succeeded" without ever being able to call a tool.
        #
        # Doing it here also keeps the token work serialised ahead of the concurrent listing
        # below: by then every audience is memoised in the provider, so the gathered listings
        # only ever read it rather than racing to mint the same token twice.
        for server in connections:
            await self.token_provider.get(self.audience_for(server))

        tools: list[BaseTool] = []
        # follow_redirects: console-backend's ``/mcp`` mount answers ``307 → /mcp/``.
        async with httpx.AsyncClient(timeout=self._timeout.total_seconds(), follow_redirects=True) as http_client:
            # Both servers at once. They are independent endpoints with their own audience
            # and their own token, so the sequential loop this replaced paid the slower of
            # the two plus the faster one on the run's critical path for nothing. Listing
            # was already measured as the largest contributor to time-to-first-token in the
            # orchestrator, which gathers for the same reason.
            #
            # Not return_exceptions: a partial catalogue is not a smaller catalogue, it is a
            # silently less capable run. Fail-don't-degrade for discovery is ADR-0009's
            # stated policy — the first failure propagates and the run reports it.
            listed = await asyncio.gather(
                *(self._list_server_with_retry(server, http_client) for server in connections)
            )
            catalogues = dict(zip(connections, listed, strict=True))
            for server, connection in connections.items():
                catalogue = catalogues[server]
                server_names = (
                    sorted(catalogue.tools)
                    if list_everything
                    else sorted(n for n in names if _server_for(n) == server)
                )
                for name in server_names:
                    entry = catalogue.tools.get(name)
                    if entry is None:
                        continue  # not offered to this user by this server
                    tools.append(
                        make_lazy_tool(
                            entry,
                            server_name=server,
                            connection=connection,
                            tool_interceptors=interceptors,
                        )
                    )

        unresolved = set() if list_everything else names - {t.name for t in tools}
        self.stats["unresolved"] = sorted(unresolved)
        self.stats["seconds"] = round(time.monotonic() - started, 3)
        logger.info(
            "Resolved %d/%s MCP tools in %.3fs via tools/list (%s)%s",
            len(tools),
            "all" if list_everything else len(names),
            self.stats["seconds"],
            ", ".join(f"{s}={src}" for s, src in self.stats["source"].items()) or "no listing",
            f"; not offered by any server: {sorted(unresolved)}" if unresolved else "",
        )
        return tools
