"""Schemas for runtime model registration via the Model Gateway (Q6 / ADR-0001)."""

from pydantic import BaseModel, Field, field_validator

from .usage import RateCardPricingEntry


DEFAULT_ROLES = ("chat", "embedding", "multimodal_embedding")


class GatewayModel(BaseModel):
    """A model deployment as reported by the gateway's /model/info."""

    model_name: str
    model_id: str | None = None
    provider: str | None = None
    litellm_model: str | None = None
    mode: str | None = None
    input_modes: list[str] = Field(default_factory=list)
    # Roles this model is the fleet default for (a model can hold several, e.g. both
    # "embedding" and "multimodal_embedding").
    default_roles: list[str] = Field(default_factory=list)
    # True only for models registered at runtime (in the gateway DB). Config-defined
    # models can't be edited/deleted/defaulted — LiteLLM rejects /model/update on them.
    db_model: bool = False
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None
    supports_reasoning: bool | None = None
    supports_vision: bool | None = None


class SetDefaultRequest(BaseModel):
    """Mark a model as the fleet default for a role (graceful degradation when an alias retires)."""

    role: str = Field(..., description="One of: chat, embedding, multimodal_embedding")

    @field_validator("role")
    @classmethod
    def _role_known(cls, v: str) -> str:
        if v not in DEFAULT_ROLES:
            raise ValueError(f"role must be one of {DEFAULT_ROLES}")
        return v


class CatalogModel(BaseModel):
    """An entry from LiteLLM's known-model catalog (for the registration picker)."""

    model_id: str
    provider: str | None = None
    mode: str = "chat"
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None
    cache_read_input_token_cost: float | None = None
    cache_creation_input_token_cost: float | None = None
    max_input_tokens: int | None = None
    supports_vision: bool = False
    supports_reasoning: bool = False
    supports_audio_input: bool = False
    supports_pdf_input: bool = False


class CostPrefill(BaseModel):
    """Provider base cost the gateway already knows, as per-million rate-card units.

    Best-effort seed for the registration form; null when the gateway doesn't know
    the model (bleeding-edge), in which case the admin enters rates manually.
    """

    pricing: dict[str, RateCardPricingEntry] = Field(default_factory=dict)
    source: str = Field("gateway", description="Where the seed came from")


class ModelRegistrationRequest(BaseModel):
    """Register a model: routing/capability go to the gateway, billing to the Rate Card.

    Per ADR-0002/Q6a the Rate Card is written first (a model must be billable before
    it is usable), then the deployment is registered on the gateway.
    """

    model_name: str = Field(..., description="Public alias apps request (e.g. 'claude-sonnet-4.6')")
    litellm_params: dict = Field(..., description="Gateway routing (model id, region/creds refs, timeouts)")
    model_info: dict = Field(default_factory=dict, description="Capability + cost metadata stored on the gateway")
    mode: str = Field("chat", description="'chat' or 'embedding' (written to model_info.mode)")
    # Required so every registered model declares what payloads it accepts — the
    # orchestrator relies on this to decide what it can send to (dynamic) sub-agents.
    input_modes: list[str] = Field(
        default_factory=lambda: ["text", "image"],
        description="Content types the model accepts (text/image/audio/video/file)",
    )
    # Billing — written to console-backend's Rate Card (the authoritative billed rate).
    provider: str = Field(..., description="Rate-card provider key (matches what the proxy reports at runtime)")
    pricing: dict[str, RateCardPricingEntry] = Field(..., description="billing_unit → price; the billed rate")
    model_name_pattern: str | None = None


class ModelRegistrationResponse(BaseModel):
    model_name: str
    rate_card_entry_ids: list[int]
    gateway_model_id: str | None = None
    status: str = "registered"
