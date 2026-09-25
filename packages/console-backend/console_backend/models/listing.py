"""Shared query models for the console's list endpoints."""

from enum import Enum


class OwnershipFilter(str, Enum):
    """Which side of the console's owned / shared-with-me split to list.

    The console shows these as tabs. Splitting a page in the browser would give
    each tab an arbitrary fraction of its real contents, so the choice is a
    server-side filter applied before the page is cut.
    """

    OWNED = "owned"
    SHARED = "shared"


class ActivationFilter(str, Enum):
    """Whether a sub-agent is activated *for the caller*, as the console means it.

    This matches the `is_activated` field exactly — `usa.sub_agent_id IS NOT NULL`
    — so the facet and the card's toggle always agree. It is deliberately NOT the
    same predicate as the service's `activated_only`, which additionally treats
    public system agents as activated because the orchestrator needs them in a
    user's registry; a seeded system agent renders a Disabled toggle, so listing
    it under Enabled would contradict what the card shows.
    """

    ENABLED = "enabled"
    DISABLED = "disabled"

