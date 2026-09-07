"""Agent discovery services for dynamic sub-agent and tool discovery.

This module handles the discovery of available sub-agents and tools,
including caching and error handling.
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import httpx
from a2a.types import AgentCard
from google.protobuf.json_format import ParseDict
from agent_common.a2a.config import A2AClientConfig
from agent_common.a2a.factory import make_a2a_async_runnable
from deepagents import CompiledSubAgent
from langchain_core.tools import BaseTool
from langchain_mcp_adapters.callbacks import Callbacks
from langchain_mcp_adapters.client import MultiServerMCPClient
from langchain_mcp_adapters.sessions import StreamableHttpConnection
from ringier_a2a_sdk.cost_tracking.attribution import context_header
from ringier_a2a_sdk.oauth import OidcOAuth2Client
from ringier_a2a_sdk.utils.mcp_errors import (
    format_mcp_error,
    get_mcp_http_status_error,
    is_retryable_mcp_error,
    log_mcp_gateway_error,
)
from ringier_a2a_sdk.utils.mcp_progress import on_mcp_progress

from ..models.config import AgentSettings
from agent_common.core.catalogue_ingest import STATELESS_TIMEOUT_S, fetch_catalogue
from agent_common.core.token_provider import UserTokenProvider, bearer_interceptor
from agent_common.core.tool_catalogue import ServerCatalogue, build_lazy_tools

logger = logging.getLogger(__name__)

# Process-wide bound on concurrent MCP tool-catalogue fetches; see
# ToolDiscoveryService._discovery_semaphore for why this is not per-call.
# Created lazily so the configured limit is read at first use rather than import.
_DISCOVERY_SEMAPHORE: asyncio.Semaphore | None = None
_DISCOVERY_SEMAPHORE_LIMIT: int | None = None


async def _console_attribution_interceptor(request, handler):
    """Stamp the caller's cost-attribution (user_sub, conversation_id, sub_agent_id, …) on every
    console MCP tool call as the dedicated ``x-nannos-context`` header.

    Why a tool-call interceptor and not a static header / tool param / httpx event hook: the console
    MCP client is memoized per-user and shared across conversations, so conversation_id can't be
    baked in at connection time. The interceptor runs in the tool-call task — exactly where the
    request-scoped attribution ContextVars are live (set per turn in executor.set_attribution) —
    so it reads the *current* conversation deterministically, unlike a transport-level httpx hook
    that fires in the streamable-HTTP transport's own task. It rides as a header, never a tool param,
    so conversation_id stays out of MCP tool discovery; console-backend reads it for request context
    (a bug report's conversation_id) or forwards it onto a gateway sub-call (console_web_search).
    Scoped to the ``console`` server only — never the external gateway connections.
    """
    if request.server_name == "console":
        ctx = context_header()  # {"x-nannos-context": "..."} from the current attribution, or {}
        if ctx:
            request = request.override(headers={**(request.headers or {}), **ctx})
    return await handler(request)


class AgentDiscoveryService:
    """Service for discovering available sub-agents and tools.

    Handles fetching agent cards, registering streaming runnables,
    and caching discovery results.
    """

    def __init__(
        self,
        config: AgentSettings,
        oauth2_client: OidcOAuth2Client | None = None,
    ):
        """Initialize the discovery service.

        Args:
            config: AgentSettings instance containing configuration
            oauth2_client: OAuth2 client for agent-to-agent auth (None in local dev mode)
        """
        self.config = config
        self.oauth2_client = oauth2_client

    async def register_agents(
        self,
        agent_metadata: dict[str, dict[str, Any]],
        token: str,
    ) -> List[CompiledSubAgent]:
        """Discover available sub-agents by fetching their agent cards.

        Args:
            agent_metadata: Metadata map from agent_url -> {sub_agent_id, name, description}
            token: User's access token for authentication and token exchange

        Returns:
            List of discovered sub-agents
        """

        logger.debug("Starting agent discovery...")

        sub_agents = []
        for base_url in agent_metadata.keys():
            try:
                # Get metadata for this agent URL
                metadata = agent_metadata.get(base_url, {})
                sub_agent_id = metadata.get("sub_agent_id")

                agent = await self._discover_single_agent(
                    base_url,
                    token,
                    sub_agent_id=sub_agent_id,  # Pass sub_agent_id to discovery
                )
                if agent:
                    sub_agents.append(agent)
            except Exception as e:
                logger.warning(f"Failed to discover agent at {base_url}: {type(e).__name__}: {e}")
                self._log_discovery_error(e, base_url)

        logger.debug(f"Agent discovery complete. Found {len(sub_agents)} agents")

        return sub_agents

    async def _discover_single_agent(
        self,
        base_url: str,
        user_token: Optional[str] = None,
        sub_agent_id: Optional[int] = None,
    ) -> Optional[CompiledSubAgent]:
        """Discover a single agent from the given URL.

        Args:
            base_url: Base URL of the agent
            user_token: User's access token for authentication

        Returns:
            CompiledSubAgent if discovery succeeds, None otherwise
        """
        logger.debug(f"Fetching agent card from: {base_url}")

        agent_card_url = f"{base_url.rstrip('/')}/.well-known/agent-card.json"
        logger.debug(f"Agent card URL: {agent_card_url}")

        async with httpx.AsyncClient(verify=False, timeout=5.0) as client:
            response = await client.get(agent_card_url)
            response.raise_for_status()
            agent_card_data = response.json()
            logger.debug(f"Agent card data: {agent_card_data}")

            # Create AgentCard from the fetched ProtoJSON (A2A v1.0+ uses protobuf types)
            agent_card = ParseDict(agent_card_data, AgentCard(), ignore_unknown_fields=True)
            card_url = agent_card.supported_interfaces[0].url if agent_card.supported_interfaces else ""
            logger.debug(f"Agent card parsed: name={agent_card.name}, url={card_url}")

        # Create the A2A runnable with the proper agent card and authentication
        # Pass sub_agent_id via config for cost tracking attribution
        config = A2AClientConfig(sub_agent_id=sub_agent_id)
        base_runnable = make_a2a_async_runnable(
            agent_card,
            self.oauth2_client,
            user_token=user_token,
            config=config,
        )
        logger.debug(f"A2A runnable created successfully for {card_url} with sub_agent_id={sub_agent_id}")

        # Create the sub-agent (middleware will be applied by create_deep_agent)
        # Registry key = tracking key: the same label must reach the `task` tool
        # enum, the a2a_tracking writers, and _extract_tracking_ids' reader.
        agent_name = base_runnable.tracking_key

        agent = CompiledSubAgent(
            name=agent_name,
            description=base_runnable.description,
            runnable=base_runnable,
        )
        # Attach console-backend integer ID for feedback attribution
        if sub_agent_id is not None:
            agent["sub_agent_id"] = sub_agent_id  # type: ignore[typeddict-unknown-key]
        logger.debug(f"Sub-agent created: name={agent['name']}, description={agent['description']}")

        return agent

    def _log_discovery_error(self, error: Exception, base_url: str) -> None:
        """Log appropriate warning message based on error type.

        Args:
            error: The exception that occurred
            base_url: URL where the error occurred
        """
        if isinstance(error, httpx.ConnectError):
            logger.warning(f"Agent at {base_url} is not reachable (connection refused). The agent may be offline.")
        elif isinstance(error, httpx.TimeoutException):
            logger.warning(f"Agent at {base_url} timed out. The agent may be slow or offline.")
        elif isinstance(error, httpx.HTTPStatusError):
            logger.warning(f"Agent at {base_url} returned HTTP error: {error.response.status_code}")
        elif isinstance(error, httpx.ReadError):
            logger.warning(
                f"Agent at {base_url} connection was interrupted (ReadError). The agent may have crashed or be offline."
            )
        else:
            # Only show full traceback for unexpected errors
            import traceback

            logger.debug(f"Traceback: {traceback.format_exc()}")


class ToolDiscoveryService:
    """Service for discovering available MCP tools.

    Handles connecting to MCP servers and retrieving available tools.
    """

    def __init__(self, config: AgentSettings, oauth2_client: OidcOAuth2Client | None = None):
        """Initialize the tool discovery service.

        Args:
            config: AgentSettings instance containing configuration
            oauth2_client: OAuth2 client for MCP gateway auth (None in local dev mode)
        """
        self.config = config
        self.oauth2_client = oauth2_client

    def _gateway_base_url(self) -> str:
        # Strip the trailing slash first: removesuffix("/mcp") is a no-op on ".../mcp/".
        return self.config.MCP_GATEWAY_URL.rstrip("/").removesuffix("/mcp")

    async def fetch_available_servers(self, token: str) -> List[Dict[str, Any]]:
        """Fetch list of available MCP servers from the gateway.

        Args:
            token: User's access token for authentication

        Returns:
            List of server metadata dicts (slug, description,
            isOutputCompressionEnabled, isOutputCompressionTransformEnabled, etc.)

        Raises:
            httpx.HTTPError: If API request fails
        """
        logger.debug("Fetching available MCP servers from gateway")
        try:
            # Call the MCP gateway API to get server list
            servers_url = f"{self._gateway_base_url()}/api/v1/mcp-servers"

            logger.debug(f"Fetching servers from: {servers_url}")

            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(
                    servers_url,
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()
                data = response.json()

                servers = data.get("servers", [])
                logger.debug(f"Discovered {len(servers)} MCP servers")
                return servers

        except Exception as e:
            # A traceback (exc_info) and the gateway response body are both
            # only logged once between them: the body line already restates
            # the status/URL that exc_info's own str(e) would repeat, so pick
            # whichever one actually adds information for this error.
            if get_mcp_http_status_error(e) is not None:
                log_mcp_gateway_error(logger, e, context="Failed to fetch MCP servers ")
            else:
                logger.error(f"Failed to fetch MCP servers: {e}", exc_info=True)
            return []

    def _discovery_semaphore(self) -> asyncio.Semaphore:
        """Process-wide bound on concurrent MCP catalogue fetches.

        Deliberately module-level, NOT per-call: discovery runs per user on cache
        miss, so a per-invocation semaphore would still allow limit x N concurrent
        fetches when N users hit a cold cache together — with a limit of 5, seven
        simultaneous users already exceed the ~31 that OOMKilled the pod. What has
        to be capped is what the *process* holds open at once.

        The cost is that unrelated users' cold discoveries queue behind each other.
        That is the intended trade: bounded memory over parallel cold starts.
        """
        global _DISCOVERY_SEMAPHORE, _DISCOVERY_SEMAPHORE_LIMIT
        limit = self.config.MCP_DISCOVERY_CONCURRENCY
        if _DISCOVERY_SEMAPHORE is None or _DISCOVERY_SEMAPHORE_LIMIT != limit:
            _DISCOVERY_SEMAPHORE = asyncio.Semaphore(limit)
            _DISCOVERY_SEMAPHORE_LIMIT = limit
        return _DISCOVERY_SEMAPHORE

    async def _ingest_server(
        self,
        server_name: str,
        client: MultiServerMCPClient,
        connection: StreamableHttpConnection,
        *,
        http_client: httpx.AsyncClient | None,
    ) -> ServerCatalogue:
        """Fetch one server's catalogue (``agent_common.core.catalogue_ingest.fetch_catalogue``).

        The catalogue is this user's view (the gateway lists per bearer, profiles filter per
        user) and is owned by this user's discovery-cache entry — never shared with another
        user's.
        """
        return await fetch_catalogue(
            server_slug=server_name,
            url=connection["url"],
            headers=connection.get("headers"),
            http_client=http_client,
            session_factory=lambda: client.session(server_name),
            stateless=self.config.MCP_CATALOGUE_STATELESS_LIST,
        )

    async def _get_catalogue_with_retry(
        self,
        client: MultiServerMCPClient,
        server_name: str,
        connection: StreamableHttpConnection,
        *,
        http_client: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        initial_delay: float = 1.0,
    ) -> ServerCatalogue:
        """Fetch a server's catalogue with exponential backoff retry for transient errors.

        Retries on HTTP 502, 503, 504 errors with exponential backoff.
        Non-retryable errors (4xx, connection refused, etc.) fail immediately.

        Args:
            client: MultiServerMCPClient instance (owns the per-server MCP connections)
            server_name: Name of the server to get tools from
            connection: The server's MCP connection (URL + auth headers)
            http_client: Shared httpx client for the stateless fast path (None = SDK only)
            max_retries: Maximum number of retry attempts (default: 3)
            initial_delay: Initial delay between retries in seconds (default: 1.0)

        Returns:
            The server's catalogue (bytes + cards)

        Raises:
            Exception: If all retries are exhausted or a non-retryable error occurs
        """
        last_error = None
        delay = initial_delay

        for attempt in range(max_retries):
            try:
                # Hold a slot only for the fetch itself. Keeping it across the
                # backoff sleep below would let a few flaky servers idle away the
                # whole budget while healthy ones wait on a user-facing path.
                async with self._discovery_semaphore():
                    catalogue = await self._ingest_server(server_name, client, connection, http_client=http_client)

                if attempt > 0:
                    logger.info(f"Successfully loaded MCP tools from {server_name} on attempt {attempt + 1}")
                return catalogue

            except Exception as e:
                last_error = e

                # Check if this is a retryable error
                is_retryable = is_retryable_mcp_error(e)

                if not is_retryable or attempt >= max_retries - 1:
                    # Non-retryable error or exhausted retries
                    if is_retryable:
                        error_msg = format_mcp_error(e)
                        logger.error(
                            f"Failed to load MCP tools from {server_name} after {attempt + 1} attempts: {error_msg}"
                        )
                    else:
                        logger.error(f"Non-retryable error loading MCP tools from {server_name}: {e}")
                    raise

                # Retryable error - wait and retry
                logger.warning(
                    f"Transient error loading MCP tools from {server_name} (attempt {attempt + 1}/{max_retries}): {e}. "
                    f"Retrying in {delay:.1f}s..."
                )
                await asyncio.sleep(delay)
                delay *= 2  # Exponential backoff

        # Should never reach here, but just in case
        raise last_error or Exception(f"Failed to load MCP tools from {server_name}")

    def make_token_provider(self, token: str) -> UserTokenProvider:
        """A per-user provider of exchanged MCP bearer tokens (see agent_common.core.token_provider).

        ``MCP_TOKEN_LEEWAY_SECONDS`` sets how much validity a memoised token must keep to be
        reused. Setting it above the exchanged tokens' lifetime forces a fresh exchange on
        *every* call — a QA lever to watch the call-time path work without waiting for expiry.
        """
        return UserTokenProvider(
            token, self.oauth2_client.exchange_token, leeway_seconds=self.config.MCP_TOKEN_LEEWAY_SECONDS
        )

    def _audience_for_server(self, server_name: str) -> str:
        """OAuth audience a server's calls must carry: console-backend's client id for the
        console MCP server, the gateway client for everything else."""
        return self.config.CONSOLE_BACKEND_CLIENT_ID if server_name == "console" else "gatana"

    def _direct_servers(self) -> List[Dict[str, Any]]:
        """Parse MCP_DIRECT_SERVERS (JSON array of {slug, url, headers?}).

        Invalid JSON or malformed entries are logged and skipped — a broken
        direct-server config must never take down gateway/console discovery.
        """
        raw = getattr(self.config, "MCP_DIRECT_SERVERS", "") or ""
        if not raw.strip():
            return []
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.error(f"MCP_DIRECT_SERVERS is not valid JSON ({e}); ignoring")
            return []
        if not isinstance(parsed, list):
            logger.error("MCP_DIRECT_SERVERS must be a JSON array; ignoring")
            return []
        servers = []
        for entry in parsed:
            if not isinstance(entry, dict) or not entry.get("slug") or not entry.get("url"):
                logger.error(f"Skipping malformed MCP_DIRECT_SERVERS entry: {entry!r}")
                continue
            headers = entry.get("headers")
            if headers is not None and not isinstance(headers, dict):
                logger.error(f"Skipping MCP_DIRECT_SERVERS entry with non-object headers: {entry.get('slug')}")
                continue
            servers.append({"slug": str(entry["slug"]), "url": str(entry["url"]), "headers": headers})
        return servers

    async def discover_tools(
        self,
        token: str,
        white_list: Optional[List[str]] = None,
        include_server_slugs: Optional[List[str]] = None,
        token_provider: UserTokenProvider | None = None,
    ) -> List[BaseTool]:
        """Discover available MCP tools with optional server filtering.

        Tokens: the user's token is exchanged for the gateway / console audiences through a
        per-user :class:`UserTokenProvider`. The exchanged token is used to *list* tools now;
        the tools themselves are built on token-free connections and mint a fresh bearer on
        every call via an interceptor, so a tool's credential is never older than the
        provider's leeway — however long the tool object lives (discovery cache, sub-agent
        hand-over).

        Args:
            token: User's access token from the orchestrator
            white_list: Optional list of tool names to filter to (post-discovery filtering)
            include_server_slugs: Optional list of server slugs to include tools from
            token_provider: The user's provider (created here when not given; the executor
                passes the one it keeps across turns)

        Returns:
            List of discovered tools with server_name in metadata
        """
        logger.debug("Discovering tools for orchestrator deep agent")
        try:
            provider = token_provider or self.make_token_provider(token)
            # `connections` carry the credential for *listing* (this request);
            # `call_connections` are what the built tools keep — token-free for
            # gateway/console (a bearer is minted per call via the interceptors),
            # the static headers for direct servers.
            connections: dict[str, Any] = {}
            call_connections: dict[str, Any] = {}
            compression_server_slugs: set[str] = set()

            # --- Gateway (Gatana) servers via per-user token exchange. Non-fatal:
            # console + direct servers below must survive a missing/unreachable
            # gateway (e.g. local dev without Gatana).
            try:
                # Exchange user token for gatana token
                # The target client is 'gatana' in the same Keycloak realm
                mcp_gateway_token = await provider.get("gatana")
                logger.info("Successfully exchanged token for gatana")

                # Fetch available servers to create per-server connections
                servers = await self.fetch_available_servers(mcp_gateway_token)

                # Identify compression-enabled servers (Gatana-specific, always active).
                # Gateway returns isOutputCompressionEnabled and isOutputCompressionTransformEnabled
                # per server; we consider a server compression-enabled when both flags are true.
                compression_server_slugs = {
                    s["slug"]
                    for s in servers
                    if s.get("slug")
                    and s.get("isOutputCompressionEnabled")
                }
                if compression_server_slugs:
                    logger.info(f"Compression-enabled servers: {compression_server_slugs}")

                # Filter servers if include_server_slugs is provided
                if include_server_slugs:
                    servers = [s for s in servers if s.get("slug") in include_server_slugs]
                    logger.debug(f"Filtered to {len(servers)} servers: {[s.get('slug') for s in servers]}")

                # Create one connection per MCP server
                # This allows MultiServerMCPClient to naturally track which tools come from which server.
                # `connections` carry the bearer for *listing* (this request); `call_connections` are the
                # same URLs without credentials — the tools built on them mint a bearer per call.
                for server in servers:
                    server_slug = server.get("slug")
                    if not server_slug:
                        continue

                    # Each connection uses the gateway URL but filtered to one server
                    url = f"{self.config.MCP_GATEWAY_URL}?includeOnlyServerSlugs={server_slug}"
                    connections[server_slug] = StreamableHttpConnection(
                        transport="streamable_http",
                        url=url,
                        headers={"Authorization": f"Bearer {mcp_gateway_token}"},
                    )
                    call_connections[server_slug] = StreamableHttpConnection(transport="streamable_http", url=url)
            except Exception as gateway_error:
                # Only tolerable in the dev shape (direct servers configured, no
                # Gatana). In production a swallowed gateway failure would return a
                # console-only toolset that the executor caches for the discovery
                # TTL — every turn in that window silently loses its gateway tools.
                # Re-raise there so the outer handler returns [] loudly, as before.
                if not self._direct_servers():
                    raise
                logger.warning(
                    f"MCP gateway discovery unavailable ({gateway_error}); continuing with direct/console servers only"
                )

            # --- Direct MCP servers (no gateway, static headers) from config.
            # Local dev + Embedded Nannos hosts not fronted by Gatana. The static
            # bearer is a service identity — per-user exchange is the production
            # path (ADR-0002), so keep these to dev/spike scopes.
            direct_slugs: set[str] = set()
            for direct in self._direct_servers():
                slug = direct["slug"]
                if include_server_slugs and slug not in include_server_slugs:
                    continue
                direct_connection = StreamableHttpConnection(
                    transport="streamable_http",
                    url=direct["url"],
                    headers=direct.get("headers") or {},
                )
                connections[slug] = direct_connection
                # The static header IS the credential: the built tools keep it (no
                # per-call bearer exchange — see tool_interceptors below).
                call_connections[slug] = direct_connection
                direct_slugs.add(slug)
                logger.info(f"Added direct MCP connection '{slug}': {direct['url']}")

            # Add agent-console backend as an additional MCP server
            # Exchange token for agent-console audience (separate from gatana)
            if self.config.CONSOLE_BACKEND_URL:
                console_mcp_url = f"{self.config.CONSOLE_BACKEND_URL}/mcp"
                console_token = await provider.get(self.config.CONSOLE_BACKEND_CLIENT_ID)
                connections["console"] = StreamableHttpConnection(
                    transport="streamable_http",
                    url=console_mcp_url,
                    headers={"Authorization": f"Bearer {console_token}"},
                )
                call_connections["console"] = StreamableHttpConnection(transport="streamable_http", url=console_mcp_url)
                logger.debug(f"Added agent-console MCP connection: {console_mcp_url}")

            if not connections:
                logger.warning("No MCP server connections available, returning empty tool list")
                return []

            logger.debug(f"Created {len(connections)} MCP server connections: {list(connections.keys())}")

            # Create client with per-server connections
            # Discover tools per-server in parallel with retry logic
            # (langchain_mcp_adapters does NOT store server_name
            # in tool.metadata automatically — it only captures it in call_tool closures)
            client = MultiServerMCPClient(
                connections=connections,
                callbacks=Callbacks(on_progress=on_mcp_progress),
            )
            # Per-call interceptors for the tools: a fresh bearer for the server's audience, then
            # x-nannos-context (conversation_id, …) on console tool calls so console-backend tools
            # that hit the gateway (console_web_search) bill to the right conversation instead of
            # "Direct API Calls" (scoped to the console server inside).
            tool_interceptors = [
                bearer_interceptor(provider, self._audience_for_server),
                _console_attribution_interceptor,
            ]

            # Gather tools from all servers with retry logic.
            # Use asyncio.gather with return_exceptions=True to handle partial failures gracefully.
            #
            # Each fetch takes a slot from a process-wide semaphore (see
            # _get_tools_with_retry), so the number of MCP sessions and response
            # bodies held open AT ONCE no longer scales with the size of the
            # gateway catalogue. On prod an unbounded fan-out reached ~31
            # concurrent fetches and OOMKilled the pod (2026-08-15).
            #
            # Note this bounds only what is IN FLIGHT. The tools that have already
            # been fetched are retained for the whole call, so total retained
            # memory still scales with the catalogue; it is the concurrent peak
            # that is capped. Results stay positionally aligned with `connections`
            # below, so the zip() that follows is unaffected.
            logger.debug(
                f"Discovering tools from {len(connections)} MCP servers "
                f"(max {self.config.MCP_DISCOVERY_CONCURRENCY} concurrent, process-wide)"
            )
            # follow_redirects: a mount such as console-backend's ``/mcp`` answers ``307 → /mcp/``;
            # the SDK transport follows it, so the stateless path must too or it falls back for
            # the wrong reason.
            async with httpx.AsyncClient(timeout=STATELESS_TIMEOUT_S, follow_redirects=True) as http_client:
                results = await asyncio.gather(
                    *[
                        self._get_catalogue_with_retry(client, slug, connection, http_client=http_client)
                        for slug, connection in connections.items()
                    ],
                    return_exceptions=True,
                )

            # Process results - filter out exceptions and log failures. Each catalogue
            # is wrapped into per-user LazyMcpTools bound to this user's connection
            # (token-free: a bearer is minted per call via the interceptors); the catalogue bytes themselves are
            # shared across users via the store.
            tools: list[BaseTool] = []
            failed_servers = []
            source_by_server: dict[str, str] = {}
            for slug, result in zip(connections.keys(), results):
                if isinstance(result, BaseException):
                    error_msg = format_mcp_error(result) if isinstance(result, Exception) else str(result)
                    logger.error(f"Failed to discover tools from server '{slug}': {error_msg}")
                    failed_servers.append(slug)
                elif isinstance(result, ServerCatalogue):
                    source_by_server[slug] = result.source
                    extra_metadata: dict[str, Any] = {}
                    if compression_server_slugs and slug in compression_server_slugs:
                        extra_metadata["compression_enabled"] = True
                    # Tag direct-server tools: they have no per-user registry entry,
                    # so the orchestrator auto-whitelists them (see build_runtime_context).
                    if slug in direct_slugs:
                        extra_metadata["direct_server"] = True
                    tools.extend(
                        build_lazy_tools(
                            result,
                            connection=call_connections[slug],
                            callbacks=client.callbacks,
                            # Direct servers authenticate with the static headers on their
                            # connection — the bearer interceptor would overwrite them.
                            tool_interceptors=(
                                [_console_attribution_interceptor] if slug in direct_slugs else tool_interceptors
                            ),
                            extra_metadata=extra_metadata or None,
                        )
                    )

            # Which ingest path each server took must be visible: a silent fall-back to
            # the slow path would look like the optimisation simply not working.
            fast = sorted(s for s, src in source_by_server.items() if src == "stateless")
            slow = sorted(s for s, src in source_by_server.items() if src == "mcp")
            logger.info(
                "Tool catalogue ingest: %d server(s) via stateless tools/list %s, %d via SDK session %s",
                len(fast),
                fast,
                len(slow),
                slow,
            )

            if failed_servers:
                logger.warning(
                    f"Tool discovery completed with failures from {len(failed_servers)} server(s): {failed_servers}"
                )

            logger.debug(
                f"Discovered {len(tools)} MCP tools from {len(connections) - len(failed_servers)}/{len(connections)} servers"
            )

            # Apply whitelist filtering if provided
            if white_list:
                white_list_set = set(white_list)
                # Auto-include all tools from the compression server when whitelisted
                # tools come from compression-enabled servers
                if compression_server_slugs:
                    compression_slug = self.config.GATANA_COMPRESSION_SERVER_SLUG
                    whitelisted_has_compression = any(
                        t.metadata and t.metadata.get("compression_enabled") for t in tools if t.name in white_list_set
                    )
                    if whitelisted_has_compression:
                        compression_tool_names = {
                            t.name for t in tools if t.metadata and t.metadata.get("server_name") == compression_slug
                        }
                        if compression_tool_names:
                            white_list_set |= compression_tool_names
                            logger.debug(
                                f"Auto-including compression server tools {compression_tool_names} "
                                f"for whitelisted tools from compression-enabled servers"
                            )
                tools = [tool for tool in tools if tool.name in white_list_set]
                logger.debug(f"Filtered tools based on white list: {len(tools)} tools remain")

            return tools

        except Exception as e:
            logger.error(f"Failed to discover tools with token exchange: {e}", exc_info=True)
            return []
        # finally:
        #     await self.oauth2_client.close()
