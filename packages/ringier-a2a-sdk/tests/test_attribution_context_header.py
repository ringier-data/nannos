"""x-nannos-context header: the inter-service request-context channel for the orchestrator →
console-backend MCP hop (distinct from the gateway's x-litellm-spend-logs-metadata header)."""

import json

from ringier_a2a_sdk.cost_tracking.attribution import (
    NANNOS_CONTEXT_HEADER,
    attribution_scope,
    context_header,
    parse_context_header,
)


def test_context_header_uses_dedicated_name_and_carries_context_not_identity():
    with attribution_scope(user_sub="u1", conversation_id="c1", sub_agent_id="sa1"):
        h = context_header()
    assert set(h) == {NANNOS_CONTEXT_HEADER}
    assert NANNOS_CONTEXT_HEADER == "x-nannos-context"
    payload = json.loads(h[NANNOS_CONTEXT_HEADER])
    # user_sub is omitted — identity travels via the authenticated token on this hop.
    assert payload == {"conversation_id": "c1", "sub_agent_id": "sa1"}


def test_context_header_empty_when_no_attribution():
    # Outside any attribution scope nothing is stamped, so headers.update() is a no-op.
    assert context_header() == {} or "conversation_id" not in json.loads(
        context_header().get(NANNOS_CONTEXT_HEADER, "{}")
    )


def test_overrides_win():
    with attribution_scope(conversation_id="c1"):
        h = context_header(conversation_id="override")
    assert json.loads(h[NANNOS_CONTEXT_HEADER])["conversation_id"] == "override"


def test_round_trip_carries_context_without_identity():
    with attribution_scope(user_sub="u1", conversation_id="c1"):
        h = context_header()
    # identity (user_sub) intentionally not forwarded; only the derivable-only context round-trips.
    assert parse_context_header(h[NANNOS_CONTEXT_HEADER]) == {"conversation_id": "c1"}


def test_parse_handles_missing_and_malformed():
    assert parse_context_header(None) == {}
    assert parse_context_header("") == {}
    assert parse_context_header("not-json") == {}
    assert parse_context_header("[1,2,3]") == {}  # non-dict JSON
