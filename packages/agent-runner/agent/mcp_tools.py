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
from agent_common.core.catalogue_ingest import fetch_catalogue, fetch_with_retry
from agent_common.core.token_provider import UserTokenProvider, bearer_interceptor
from agent_common.core.tool_catalogue import ServerCatalogue, make_lazy_tool
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection

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
#: Same env var and default as the orchestrator — the cap itself now lives in
#: ``agent_common.core.catalogue_ingest`` and is shared with it.
_DISCOVERY_CONCURRENCY = max(1, int(os.getenv("MCP_DISCOVERY_CONCURRENCY", "5")))


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
        """``_list_server`` under the shared retry + concurrency policy.

        The policy itself (which failures are transient, how long to back off, and the
        process-wide cap on fetches held open at once) is
        ``agent_common.core.catalogue_ingest.fetch_with_retry``, shared with the
        orchestrator so the two cannot drift.

        What is NOT shared is what a failure means: the orchestrator tolerates a partial
        catalogue, and a scheduled run does not (fail-don't-degrade, ADR-0009), so the
        gather at the call site below has no ``return_exceptions``.

        Note the retried unit is the *whole* listing of one server, not the stateless POST
        alone: ``fetch_catalogue`` already answers a failed stateless attempt by falling
        back to the SDK session and only raises once both have failed. So a gateway 503
        reaches the policy as the SDK path's error, and one attempt is one full
        stateless-then-SDK cycle.
        """
        return await fetch_with_retry(
            lambda: self._list_server(server_name, http_client),
            server_slug=server_name,
            concurrency=_DISCOVERY_CONCURRENCY,
            max_attempts=_LIST_MAX_ATTEMPTS,
            initial_delay=_RETRY_BASE_DELAY_SECONDS,
        )

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
            # ``return_exceptions`` here is about AWAITING, not about tolerating. A partial
            # catalogue is still not a smaller catalogue but a silently less capable run,
            # so the first failure is re-raised below and fail-don't-degrade (ADR-0009) is
            # unchanged.
            #
            # What it fixes is that a bare ``gather`` does not cancel the siblings when one
            # raises: the other listing kept running, outlived the ``async with`` that
            # closes the HTTP client under it, retried against a closed client, and ended
            # with nobody retrieving its exception ("Task exception was never retrieved").
            # Awaiting every task before propagating means none of them outlives the client.
            #
            # The cost is that a failure now waits for the slower listing instead of
            # returning at the faster one's error. With two tasks, each already bounded by
            # the same timeout and awaited together on the success path anyway, that is
            # cheaper than a TaskGroup — which cancels siblings but would wrap what
            # propagates in an ExceptionGroup, changing the error a caller reports.
            results = await asyncio.gather(
                *(self._list_server_with_retry(server, http_client) for server in connections),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, BaseException):
                    raise result
            catalogues = dict(zip(connections, results, strict=True))
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
