"""Which AWS regions actually offer a given Bedrock model.

Bedrock availability is per-region, and AWS's rejection says nothing about it: calling a model that
isn't offered in the caller's region returns "The provided model identifier is invalid" — the same
message a genuinely wrong model id gets. That reads as "this model can't be registered" and sends
admins to debug the id (verified 2026-08-05: ``amazon.nova-2-multimodal-embeddings-v1:0`` is absent
from eu-central-1 and works in us-east-1). This service answers the actual question so the
registration UI can state it up front, and name the regions in the failure.

Two id shapes matter:
- foundation models (``amazon.titan-embed-image-v1``) → ``list_foundation_models`` per region;
- cross-region inference profiles (``eu.anthropic.claude-…``, ``us.amazon.nova-…``) → those ids are
  NOT foundation models; they live in ``list_inference_profiles``, so both catalogs are consulted.

Fails soft on purpose. It needs ``bedrock:ListFoundationModels`` /
``bedrock:ListInferenceProfiles``, which a deployment may not grant, and it must never block or slow
a registration: any error yields "unknown" (``regions=None``) and the UI simply says nothing.
"""

import asyncio
import logging
import time

from aiobotocore.session import get_session

from ..config import config

logger = logging.getLogger(__name__)

# Model catalogs change on AWS's release cadence, not ours — a long TTL, and the whole point is that
# a re-query never happens on the critical path of a registration.
_CACHE_TTL_SECONDS = 6 * 3600
# region → (fetched_at, model ids offered there). None ids = that region couldn't be read.
_cache: dict[str, tuple[float, set[str] | None]] = {}


async def _region_model_ids(region: str) -> set[str] | None:
    """Every model id callable in this region — foundation models plus inference profiles."""
    # A cached None ("we couldn't read this region") is a real answer and is honoured for the TTL,
    # so the freshness check has to gate on the ENTRY, never on the value inside it.
    entry = _cache.get(region)
    if entry and time.monotonic() - entry[0] < _CACHE_TTL_SECONDS:
        return entry[1]

    ids: set[str] | None
    try:
        session = get_session()
        async with session.create_client("bedrock", region_name=region) as client:
            models = await client.list_foundation_models()
            ids = {m["modelId"] for m in models.get("modelSummaries", [])}
            try:
                profiles = await client.list_inference_profiles()
                ids |= {p["inferenceProfileId"] for p in profiles.get("inferenceProfileSummaries", [])}
            except Exception as e:  # older botocore / no permission: foundation models are still useful
                logger.debug(f"Bedrock availability: inference profiles unreadable in {region}: {e}")
    except Exception as e:
        # No credentials, no permission, region disabled — all mean "we can't say", never "unavailable".
        logger.warning(f"Bedrock availability: cannot list models in {region}: {e}")
        ids = None

    _cache[region] = (time.monotonic(), ids)
    return ids


async def model_regions(model_id: str) -> list[str] | None:
    """The probed regions that offer ``model_id``, or None when availability can't be determined.

    An empty list is a real answer ("none of the probed regions offer it"); None means the probe
    itself failed everywhere, so the caller must stay silent rather than claim unavailability.
    """
    if not model_id:
        return None
    regions = probed_regions()
    results = await asyncio.gather(*(_region_model_ids(r) for r in regions))
    if all(ids is None for ids in results):
        return None
    return [region for region, ids in zip(regions, results) if ids and model_id in ids]


def probed_regions() -> list[str]:
    """Regions this deployment checks: the gateway's own first, then the configured extras.

    Not every AWS region — probing is a real API call per region, and the question an admin has is
    "where can I put this model", which a handful of Bedrock regions answers.
    """
    gateway_region = config.model_gateway.default_bedrock_region
    extras = [r for r in config.model_gateway.bedrock_availability_regions if r != gateway_region]
    return ([gateway_region] if gateway_region else []) + extras
