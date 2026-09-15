"""Fit a client's answer to the question a paused sub-agent graph is asking.

The caller of a paused task sends back what the user decided. The graph, though,
is parked on a specific ``interrupt()`` — a tool approval, an in-task
authorization, a client-action round trip — and the two need not be the same
question: declining an authorization makes the sub-agent re-run the blocked
tool, whose risk guard raises a fresh *approval* interrupt, and the decline
written for the auth prompt would then be delivered to that one. Delivered raw
it reached ``interrupt(...)["decisions"]`` and killed the sub-agent with
``KeyError('decisions')``.

So the answer is translated into the shape the pending question reads, and only
where the translation is honest; then keyed by interrupt id, which LangGraph
>=1.2 requires whenever more than one interrupt is pending.
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from langgraph.types import Command

from agent_common.core.hitl_resume import (
    KIND_AUTH,
    KIND_HITL,
    authorization_from_decisions,
    interrupt_kind,
    structural_decisions,
)

logger = logging.getLogger(__name__)

#: Sentinel: the answer in hand belongs to a question the sub-agent has already
#: moved past, so it must not be delivered at all. Resuming with an EMPTY
#: id-keyed map runs the graph forward without answering anything, so its
#: pending interrupt raises again and the right question gets asked.
ANSWER_NOTHING = object()


def replicate_blanket_decision(payload: Any, interrupt_value: Any) -> Any:
    """Expand a single blanket decision to the interrupt's action_request count.

    The sub-agent's HITL middleware enforces exactly one decision per pending call
    and raises ``ValueError`` on a mismatch. When the client forwarded a single
    blanket approve/reject for N parallel calls, replicate it to N so the
    sub-agent does not raise. Per-call decision lists (``len != 1`` — already
    aligned, possibly by id) pass through unchanged.
    """
    if not isinstance(payload, dict) or not isinstance(interrupt_value, dict):
        return payload
    decisions = payload.get("decisions")
    if not isinstance(decisions, list) or len(decisions) != 1:
        return payload
    n = len(interrupt_value.get("action_requests", []))
    if n > 1:
        return {**payload, "decisions": decisions * n}
    return payload


def align_answer_to_interrupt(answer: Any, interrupt_value: Any) -> Any:
    """Fit the user's answer to the question the sub-agent is actually paused on.

    - auth answer -> approval prompt: a decline becomes an explicit **rejection**
      carrying the reason (skipping the authorization is not approving the call);
    - approval answer -> auth prompt: a rejection becomes an explicit *declined*
      authorization ("don't run it" and "don't authorize" are the same no);
    - an *approval* has no honest reading as an authorization — the user was never
      asked that — so it is not delivered at all (:data:`ANSWER_NOTHING`);
    - words are forwarded untouched: both readers classify them
      (``agent_common.core.hitl_resume``).
    """
    kind = interrupt_kind(interrupt_value)
    if kind == KIND_HITL and isinstance(answer, dict) and not isinstance(answer.get("decisions"), list):
        action_requests = interrupt_value.get("action_requests") or []
        decisions = structural_decisions(answer, action_requests)
        if decisions is not None:
            logger.info("[HITL] Translated an authorization answer into %d explicit decision(s)", len(decisions))
            return {"decisions": decisions}
        message = (answer.get("authorization") or {}).get("message")
        return message if isinstance(message, str) and message.strip() else answer
    if kind == KIND_AUTH and isinstance(answer, dict) and not isinstance(answer.get("authorization"), dict):
        authorization = authorization_from_decisions(answer.get("decisions"))
        if authorization is not None:
            logger.info("[HITL] Translated a rejection into a declined authorization")
            return {"authorization": authorization}
        logger.info("[HITL] Answer in hand was written for another interrupt — resuming without answering")
        return ANSWER_NOTHING
    return answer


def client_action_result(answer: Any, interrupt_value: dict[str, Any]) -> Any:
    """The browser's result for a paused ``client_action`` round trip.

    Matched by the request id the SDK echoed on its decision; an id-less
    result-bearing decision still resolves. An answer WITHOUT a result (the user
    typed while the round trip was parked) hands the tool an explicit no-result
    so it reports honestly instead of assuming success.
    """
    request = interrupt_value.get("client_action_request") or {}
    decisions = answer.get("decisions") if isinstance(answer, dict) else None
    decisions = [d for d in (decisions or []) if isinstance(d, dict)]
    decision = next((d for d in decisions if d.get("id") == request.get("id")), None)
    if decision is None or "client_action_result" not in decision:
        decision = next((d for d in decisions if "client_action_result" in d), None)
    result = decision.get("client_action_result") if isinstance(decision, dict) else None
    return result if isinstance(result, dict) else {"ok": False, "reason": "no-result"}


def build_resume_command(interrupts: Sequence[Any], answer: Any) -> Command:
    """The ``Command(resume=...)`` that delivers ``answer`` to a paused graph.

    One entry per pending interrupt, keyed by ``Interrupt.id`` (the xxh3
    namespace hash LangGraph matches the map against). An interrupt the answer
    cannot honestly address is left out — the graph re-raises it and the caller
    is asked the right question. An interrupt with no id (older LangGraph shapes)
    is resumed with the bare payload, which is only unambiguous when it is the
    sole one pending.
    """
    payload_in: Any = {} if answer is None else answer
    if not interrupts:
        # Nothing known to be pending: hand the graph the answer as-is and let it
        # decide (LangGraph accepts a bare resume for a single interrupt).
        return Command(resume=payload_in)
    resume_map: dict[str, Any] = {}
    bare: Any = None
    for intr in interrupts:
        value = getattr(intr, "value", intr)
        intr_id = getattr(intr, "id", None)
        if isinstance(intr, dict):
            value = intr.get("value", intr)
            intr_id = intr.get("id", intr_id)
        if isinstance(value, dict) and isinstance(value.get("client_action_request"), dict):
            payload = client_action_result(payload_in, value)
        else:
            aligned = align_answer_to_interrupt(payload_in, value)
            if aligned is ANSWER_NOTHING:
                continue
            payload = replicate_blanket_decision(aligned, value)
        if intr_id:
            resume_map[str(intr_id)] = payload
        else:
            bare = payload
    if resume_map:
        return Command(resume=resume_map)
    if bare is not None:
        return Command(resume=bare)
    return Command(resume={})
