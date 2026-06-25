"""Schemas for runtime model registration via the Model Gateway."""

from pydantic import BaseModel, Field, field_validator

from .usage import RateCardPricingEntry


# Canonical fleet-default roles — the single source of truth, imported by both the
# admin API validator (below) and ModelDefaultsService. A role is a model_defaults slot
# apps resolve at runtime, falling back to it when a referenced alias retires:
#   chat / chat:low / chat:premium — chat tiers ("chat" IS the standard tier). Sub-agents
#     may bind to a tier instead of a concrete alias, so retiring/upgrading a model is one
#     slot repoint rather than a fleet-wide reclassification.
#   embedding / multimodal_embedding — vector models.
#   search — the model backing the web_search tool (the "Search Provider" picker's gateway-native
#     model). Optional: when unset, web search auto-selects the cheapest web-search-capable model
#     (see services.web_search.pick_web_search_model). Distinct from chat: it's a tool-only sub-call.
# Semantic indexing/chunking has no slot of its own — it runs on the 'chat:low' tier
# (see model_factory.get_default_indexing_model).
VALID_ROLES = (
    "chat",
    "chat:low",
    "chat:premium",
    "embedding",
    "multimodal_embedding",
    "search",
)

# The chat tiers ("chat" = standard). Assignments to these roles are remembered per alias
# (model_alias_tiers) so a retired concrete model degrades to its tier's successor.
CHAT_TIER_ROLES = ("chat", "chat:low", "chat:premium")


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
    # Azure deployments map to a known model via model_info.base_model (cost/metadata). Surfaced
    # so the admin edit form can round-trip it instead of silently dropping it on update.
    base_model: str | None = None
    # Routing params surfaced so the edit form round-trips them instead of dropping them on
    # update (same rationale as base_model). NOT credentials — the proxy is the auth authority,
    # so registrations carry no per-model creds (vertex_credentials/keys are never stored here).
    vertex_location: str | None = None
    vertex_project: str | None = None
    aws_region_name: str | None = None
    input_cost_per_token: float | None = None
    output_cost_per_token: float | None = None
    supports_reasoning: bool | None = None
    supports_vision: bool | None = None
    # Whether the gateway advertises server-side web search for this model. Drives the console's
    # Web Search picker (which models can back the gateway-native search provider).
    supports_web_search: bool | None = None


class WebSearchModelOption(BaseModel):
    """One web-search-capable model as the Web Search picker should render it."""

    model_id: str | None = None
    model_name: str
    # First in the cheapest-first ordering — the model auto-selected when no `search` default is set.
    is_cheapest: bool = False
    # The model the console_web_search tool resolves to right now (selected default or auto).
    is_active: bool = False


class WebSearchConfig(BaseModel):
    """Fully-resolved Web Search picker state — the single backend-owned source of which model
    backs the ``console_web_search`` tool, so the console never re-derives the pick client-side.

    ``models`` is sorted cheapest-first (services.web_search ordering); ``source`` is ``"selected"``
    (admin's ``search`` default), ``"auto"`` (cheapest capable), or ``None`` when none is available.
    """

    provider: str = "gateway"
    available: bool = False
    source: str | None = None
    active_model_id: str | None = None
    active_model_name: str | None = None
    models: list[WebSearchModelOption] = Field(default_factory=list)


class SetDefaultRequest(BaseModel):
    """Mark a model as the fleet default for a role (graceful degradation when an alias retires)."""

    role: str = Field(..., description=f"One of: {', '.join(VALID_ROLES)}")

    @field_validator("role")
    @classmethod
    def _role_known(cls, v: str) -> str:
        if v not in VALID_ROLES:
            raise ValueError(f"role must be one of {VALID_ROLES}")
        return v


class CatalogModel(BaseModel):
    """An entry from LiteLLM's known-model catalog (for the registration picker)."""

    model_id: str
    provider: str | None = None
    mode: str = "chat"
    input_cost_per_token: float | None = None
    # Per-image input cost — the cross-provider signal that an embedding model accepts images
    # (set for Gemini, Vertex multimodalembedding, Bedrock Nova/Titan even where the boolean
    # capability flags are absent). The picker uses it to derive the 'image' input mode.
    input_cost_per_image: float | None = None
    output_cost_per_token: float | None = None
    cache_read_input_token_cost: float | None = None
    cache_creation_input_token_cost: float | None = None
    # Per-query web-search (grounding) fee keyed by context size (e.g.
    # {"search_context_size_medium": 0.014}); lets the picker pre-fill the `web_search` rate.
    search_context_cost_per_query: dict[str, float] | None = None
    max_input_tokens: int | None = None
    supports_vision: bool = False
    supports_reasoning: bool = False
    supports_audio_input: bool = False
    supports_pdf_input: bool = False


class GatewayUiConfig(BaseModel):
    """Deployment-specific defaults the registration UI needs (env-driven, read-only)."""

    default_vertex_location: str = Field(
        ..., description="Suggested Vertex serving region for new Vertex models (e.g. 'eu')"
    )
    default_vertex_project: str = Field(
        "", description="Suggested GCP project id for new Vertex models; '' when unset (no hardcoded default)"
    )


class CostPrefill(BaseModel):
    """Provider base cost the gateway already knows, as per-million rate-card units.

    Best-effort seed for the registration form; null when the gateway doesn't know
    the model (bleeding-edge), in which case the admin enters rates manually.
    """

    pricing: dict[str, RateCardPricingEntry] = Field(default_factory=dict)
    source: str = Field("gateway", description="Where the seed came from")


class ModelRegistrationRequest(BaseModel):
    """Register a model: routing/capability go to the gateway, billing to the Rate Card.

    The Rate Card is written first (a model must be billable before
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
