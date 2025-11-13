"""
Agent Card Security Analysis and Auto-Configuration.

This module provides utilities to inspect AgentCard security configuration
and automatically determine whether OAuth2 token exchange is required.
"""

import logging
from typing import Any, Dict, List, Optional

from a2a.types import AgentCard

logger = logging.getLogger(__name__)


class AgentSecurityConfig:
    """
    Analyzes AgentCard security configuration to determine auth requirements.

    This class examines the security_schemes and security fields in an AgentCard
    to determine:
    - Whether the agent requires OAuth2 authentication
    - What scopes are required
    - Whether token exchange is needed (different client_id)
    - The OAuth2 client_id (audience) for token exchange
    """

    def __init__(self, agent_card: AgentCard):
        """
        Initialize security configuration analyzer.

        Args:
            agent_card: The AgentCard to analyze
        """
        self.agent_card = agent_card
        self._analyzed = False
        self._requires_oauth2 = False
        self._requires_token_exchange = False
        self._client_id: Optional[str] = None
        self._required_scopes: List[str] = []

    def analyze(self) -> "AgentSecurityConfig":
        """
        Analyze the agent card security configuration.

        Returns:
            Self for method chaining
        """
        if self._analyzed:
            return self

        logger.debug(f"Analyzing security config for agent: {self.agent_card.name}")

        # Check if agent has security requirements
        if not self.agent_card.security:
            logger.debug(f"Agent {self.agent_card.name} has no security requirements")
            self._analyzed = True
            return self

        # Examine security schemes
        security_schemes = self.agent_card.security_schemes or {}
        security_requirements = self.agent_card.security or []

        for requirement in security_requirements:
            for scheme_name, required_scopes in requirement.items():
                scheme = security_schemes.get(scheme_name)

                if not scheme:
                    logger.warning(
                        f"Security requirement '{scheme_name}' not found in "
                        f"security_schemes for agent {self.agent_card.name}"
                    )
                    continue

                # Check if it's OAuth2
                scheme_root = scheme.root if hasattr(scheme, "root") else scheme
                scheme_type = getattr(scheme_root, "type", None)

                if scheme_type == "oauth2":
                    logger.debug(f"Agent {self.agent_card.name} requires OAuth2 authentication")
                    self._requires_oauth2 = True
                    self._required_scopes.extend(required_scopes)

                    # Try to extract OAuth2 configuration
                    self._extract_oauth2_config(scheme_root)

        self._analyzed = True
        return self

    def _extract_oauth2_config(self, oauth2_scheme) -> None:
        """
        Extract OAuth2 configuration from security scheme.

        Tries to determine:
        - Client ID (audience) for token exchange
        - Token URL for validation
        """
        # OAuth2 schemes might have flows configuration
        flows = getattr(oauth2_scheme, "flows", None)
        if flows:
            # Check authorization_code flow (most common for user auth)
            auth_code = getattr(flows, "authorization_code", None)
            if auth_code:
                token_url = getattr(auth_code, "token_url", None)
                if token_url:
                    logger.debug(f"Found token URL: {token_url}")
                    # Token exchange will be needed if this is a different issuer
                    self._requires_token_exchange = True

        # Try to infer client_id from agent card metadata or URL
        # Convention: Agent name + "_client_id" or extract from security metadata
        agent_name_normalized = self.agent_card.name.lower().replace(" ", "_").replace("-", "_")
        self._client_id = f"{agent_name_normalized}_client_id"

        logger.debug(f"Inferred client_id for {self.agent_card.name}: {self._client_id}")

    @property
    def requires_oauth2(self) -> bool:
        """Check if agent requires OAuth2 authentication."""
        if not self._analyzed:
            self.analyze()
        return self._requires_oauth2

    @property
    def requires_token_exchange(self) -> bool:
        """
        Check if token exchange is required.

        Token exchange is needed when the agent has OAuth2 security configured,
        indicating it has its own client_id and will validate tokens accordingly.
        """
        if not self._analyzed:
            self.analyze()
        return self._requires_token_exchange

    @property
    def client_id(self) -> Optional[str]:
        """Get the inferred OAuth2 client_id for this agent."""
        if not self._analyzed:
            self.analyze()
        return self._client_id

    @property
    def required_scopes(self) -> List[str]:
        """Get the list of required OAuth2 scopes."""
        if not self._analyzed:
            self.analyze()
        return self._required_scopes

    def get_summary(self) -> Dict[str, Any]:
        """
        Get a summary of the security configuration.

        Returns:
            Dictionary with security configuration details
        """
        if not self._analyzed:
            self.analyze()

        return {
            "agent_name": self.agent_card.name,
            "requires_oauth2": self._requires_oauth2,
            "requires_token_exchange": self._requires_token_exchange,
            "client_id": self._client_id,
            "required_scopes": self._required_scopes,
        }


def requires_token_exchange(agent_card: AgentCard) -> bool:
    """
    Quick check if an agent card requires OAuth2 token exchange.

    Args:
        agent_card: The AgentCard to check

    Returns:
        True if token exchange is required, False otherwise
    """
    config = AgentSecurityConfig(agent_card)
    return config.requires_token_exchange


def get_agent_client_id(agent_card: AgentCard) -> Optional[str]:
    """
    Extract or infer the OAuth2 client_id from an agent card.

    Args:
        agent_card: The AgentCard to analyze

    Returns:
        The client_id if determinable, None otherwise
    """
    config = AgentSecurityConfig(agent_card)
    return config.client_id


def get_required_scopes(agent_card: AgentCard) -> List[str]:
    """
    Get the list of OAuth2 scopes required by an agent.

    Args:
        agent_card: The AgentCard to analyze

    Returns:
        List of required scope strings
    """
    config = AgentSecurityConfig(agent_card)
    return config.required_scopes
