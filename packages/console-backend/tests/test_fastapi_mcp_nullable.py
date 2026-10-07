"""Optional (`X | None`) tool parameters accept an explicit null over MCP.

fastapi_mcp wrote a top-level `type` next to the `anyOf: [X, null]` union, so the MCP
server's input validation refused `task_id: null` on `console_create_bug_report` after the
user had approved the call — and the model's retry asked for approval again.
"""

import jsonschema
import pytest

from console_backend.utils import fastapi_mcp_patch


def _tool(name: str):
    import app as appmod

    return next(t for t in appmod.mcp.tools if t.name == name)


def test_an_optional_query_parameter_validates_with_null():
    schema = _tool("console_create_bug_report").inputSchema
    jsonschema.validate({"description": "x", "task_id": None}, schema)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"description": None}, schema)


@pytest.mark.asyncio
async def test_a_null_query_parameter_is_left_out_of_the_request(monkeypatch):
    seen = {}

    async def original(self, client, method, path, query, headers, body):
        seen["query"] = query
        return None

    monkeypatch.setattr(fastapi_mcp_patch, "_original_request", original)
    await fastapi_mcp_patch._request_without_null_query(
        None, None, "post", "/x", {"description": "d", "task_id": None}, {}, None
    )
    assert seen["query"] == {"description": "d"}
