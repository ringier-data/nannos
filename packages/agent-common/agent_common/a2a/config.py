"""
Configuration for A2A client.

Provides configuration options for timeouts, authentication, telemetry,
and circuit breaker functionality for Agent-to-Agent communication.
"""

import os
from typing import Any


class A2AClientConfig:
    """Configuration for A2A client.

    Note: Retry logic should be configured at the LangGraph level using retry policies,
    not within this A2A client wrapper. See LangGraph documentation:
    https://langchain-ai.github.io/langgraph/how-tos/graph-api/#add-retry-policies

    Example:
        from langgraph.types import RetryPolicy

        graph = create_deep_agent(...).with_retry(
            retry_policy=RetryPolicy(
                max_attempts=3,
                backoff_factor=2.0,
                retry_on=Exception
            )
        )
    """

    def __init__(
        self,
        *,
        timeout_connect: float | None = None,
        timeout_read: float | None = None,
        timeout_write: float | None = None,
        timeout_pool: float | None = None,
        auth_interceptor: Any = None,
        user_agent_prefix: str = "Orchestrator/1.0",
        circuit_breaker_threshold: int = 5,
        circuit_breaker_timeout: float = 30.0,
        sub_agent_id: int | None = None,
    ):
        """Initialize A2A configuration.

        Args:
            timeout_connect: Connection timeout in seconds (default: from A2A_TIMEOUT_CONNECT env or 10.0)
            timeout_read: Read timeout in seconds (default: from A2A_TIMEOUT_READ env or 600.0)
            timeout_write: Write timeout in seconds (default: from A2A_TIMEOUT_WRITE env or 10.0)
            timeout_pool: Connection pool timeout in seconds (default: from A2A_TIMEOUT_POOL env or 5.0)
            auth_interceptor: Optional authentication interceptor
            user_agent_prefix: User agent prefix for HTTP requests (default: "Orchestrator/1.0")
            circuit_breaker_threshold: Number of failures before circuit opens (default: 5)
            circuit_breaker_timeout: Seconds before circuit attempts to close (default: 30.0)
            sub_agent_id: Sub-agent ID for cost tracking attribution (optional)
        """
        # Load timeouts from environment variables with sensible defaults
        # timeout_read defaults to 600s (10 minutes) to handle heavy A2A operations like campaign_proposal
        self.timeout_connect = (
            timeout_connect if timeout_connect is not None else float(os.getenv("A2A_TIMEOUT_CONNECT", "10.0"))
        )
        self.timeout_read = timeout_read if timeout_read is not None else float(os.getenv("A2A_TIMEOUT_READ", "600.0"))
        self.timeout_write = (
            timeout_write if timeout_write is not None else float(os.getenv("A2A_TIMEOUT_WRITE", "10.0"))
        )
        self.timeout_pool = timeout_pool if timeout_pool is not None else float(os.getenv("A2A_TIMEOUT_POOL", "5.0"))
        self.auth_interceptor = auth_interceptor
        self.user_agent_prefix = user_agent_prefix
        self.circuit_breaker_threshold = circuit_breaker_threshold
        self.circuit_breaker_timeout = circuit_breaker_timeout
        self.sub_agent_id = sub_agent_id
