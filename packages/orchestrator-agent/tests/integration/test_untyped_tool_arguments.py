"""Integration tests: a tool argument with an untyped schema reaches the model and comes back.

An MCP tool's argument can carry an empty schema: ``{}`` (Python ``Any``, zod ``z.any()``) or
one that is empty after cleaning (``{"default": null}``). Schema cleaning used to drop such a
property, and on the binding path prune it from ``required`` too — so a tool with a required
untyped argument looked available but failed every call (nannos#328). The cleaner now keeps
``{}``; this checks, per alias the gateway serves, that a provider accepts the kept schema and
that the model puts the value in the call.

The tool goes through ``validate_and_clean_tool_dict`` first, exactly as the orchestrator binds
MCP tools, so a cleaner that starts dropping the argument again fails here on every family.

Run with: RUN_INTEGRATION_TESTS=1 uv run pytest tests/integration/test_untyped_tool_arguments.py -m integration -v
(the env opt-in because a path argument currently defeats `-m integration` discovery)
"""

import copy

import pytest
from agent_common.core.model_factory import REASONING_OFF, create_model
from agent_common.models.base import ModelType
from langsmith import testing as t
from ringier_a2a_sdk.utils.schema_cleaning import validate_and_clean_tool_dict

from .conftest import ALL_MODELS

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_VALUE = {"a": 1}
_PROMPT = 'Call the submit tool once, with payload set to the JSON object {"a": 1}. Do not reply with text.'


def _tool(parameters: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "submit",
            "description": "Submit a payload. Call it exactly once with the payload the user gives.",
            "parameters": parameters,
        },
    }


def _top_level(payload_schema: dict) -> dict:
    return {"type": "object", "properties": {"payload": payload_schema}, "required": ["payload"]}


# (case id, parameters schema, path from the call's args to the payload, strict?)
# Nested is not strict: in the probe that motivated this, Gemini 3.1 Pro preview made the call
# only 1 time in 4 with a nested ``{}`` (4/4 with ``{"type": "object"}``), while every family
# accepted the schema. A refusal (400) still fails the session through the pass-ratio gate.
_CASES = [
    pytest.param(_top_level({}), ("payload",), marks=pytest.mark.strict, id="top-level-empty"),
    pytest.param(_top_level({"default": None}), ("payload",), marks=pytest.mark.strict, id="default-null"),
    pytest.param(
        {
            "type": "object",
            "properties": {
                "wrapper": {"type": "object", "properties": {"payload": {}}, "required": ["payload"]},
            },
            "required": ["wrapper"],
        },
        ("wrapper", "payload"),
        id="nested-empty",
    ),
]


@pytest.mark.langsmith
@pytest.mark.parametrize("model_type", ALL_MODELS, ids=ALL_MODELS)
@pytest.mark.parametrize(("parameters", "path"), _CASES)
async def test_an_untyped_argument_is_sent_with_its_value(
    model_type: ModelType, parameters: dict, path: tuple[str, ...], usage_recorder
):
    tool = validate_and_clean_tool_dict(_tool(copy.deepcopy(parameters)))
    assert tool is not None
    t.log_inputs({"model": model_type, "parameters": tool["function"]["parameters"]})

    llm = create_model(model_type, streaming=False, reasoning_effort=REASONING_OFF, max_tokens=1024)
    reply = await llm.bind_tools([tool]).ainvoke(_PROMPT, config={"callbacks": [usage_recorder]})

    t.log_outputs({"tool_calls": reply.tool_calls, "content": reply.content})
    assert reply.tool_calls, f"{model_type} made no tool call: {str(reply.content)[:200]}"
    args = reply.tool_calls[0]["args"]
    value = args
    for key in path:
        assert isinstance(value, dict) and key in value, f"{model_type} call is missing {'.'.join(path)}: {args}"
        value = value[key]
    assert value == _VALUE, f"{model_type} sent {'.'.join(path)}={value!r}, expected {_VALUE!r}"
