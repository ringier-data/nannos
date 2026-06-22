"""Single source of truth for how every app reaches the Model Gateway.

Both the chat path (``agent_common.core.model_factory``) and the embeddings path
(``ringier_a2a_sdk.embeddings``) resolve the gateway base URL and the virtual key here, so
they can never drift. They used to inline their own copies — and a missed update (e.g.
rotating the virtual key) meant one path silently 401'd while the other kept working. This
lives in the SDK, the lowest shared layer, since agent_common depends on it (and the SDK
must not import agent_common).
"""

import os

# The virtual key apps present to the gateway when LLM_GATEWAY_API_KEY is unset. Kept in one
# place so every caller sends the SAME credential; change it here and every path follows.
_DEFAULT_GATEWAY_API_KEY = "sk-nannos-gateway"


def gateway_base_url() -> str:
    """The gateway base URL, trailing slash stripped. Raises when unset — the gateway is the
    sole path for LLM/embedding traffic, so a missing URL is a hard misconfiguration."""
    url = os.getenv("LLM_GATEWAY_URL")
    if not url:
        raise RuntimeError(
            "LLM_GATEWAY_URL is not set. The Model Gateway is the sole path for LLM "
            "calls; point it at the litellm-proxy service."
        )
    return url.rstrip("/")


def gateway_api_key() -> str:
    """The virtual/master key apps present to the gateway. Single resolver so every gateway
    caller (chat, embeddings, the model-info fetch) sends the SAME credential."""
    return os.getenv("LLM_GATEWAY_API_KEY", _DEFAULT_GATEWAY_API_KEY)
