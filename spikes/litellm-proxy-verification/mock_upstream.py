"""Minimal OpenAI-compatible upstream for the timeout/attribution checks. Throwaway.

Behaviour is keyed on the request body `model`:
  - "first-token-delay"  : sleep FIRST_TOKEN_DELAY_S before the FIRST chunk   (Check 2a)
  - "inter-chunk-stall"  : emit one chunk, then sleep INTER_CHUNK_STALL_S      (Check 2b)
  - "fast" / anything else: stream a few chunks immediately                    (Check 4)

Run (see docker-compose.yml):
  uv run --with starlette --with uvicorn mock_upstream.py
"""

import asyncio
import json
import os
import time

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

FIRST_TOKEN_DELAY_S = float(os.environ.get("FIRST_TOKEN_DELAY_S", "30"))
INTER_CHUNK_STALL_S = float(os.environ.get("INTER_CHUNK_STALL_S", "30"))

WORDS = ["Hello", " from", " the", " mock", " upstream", " server", "."]


def _chunk(model, delta=None, finish=None, usage=None):
    choice = {"index": 0, "delta": delta or {}, "finish_reason": finish}
    payload = {
        "id": "chatcmpl-mock",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [choice] if not usage else [],
    }
    if usage is not None:
        payload["usage"] = usage
    return f"data: {json.dumps(payload)}\n\n"


async def _stream(model):
    if model == "first-token-delay":
        await asyncio.sleep(FIRST_TOKEN_DELAY_S)  # delay BEFORE first token
    # first chunk: role
    yield _chunk(model, delta={"role": "assistant", "content": WORDS[0]})
    if model == "inter-chunk-stall":
        await asyncio.sleep(INTER_CHUNK_STALL_S)  # stall AFTER first chunk
    for w in WORDS[1:]:
        yield _chunk(model, delta={"content": w})
        await asyncio.sleep(0.02)
    yield _chunk(model, finish="stop")
    yield _chunk(
        model,
        usage={"prompt_tokens": 10, "completion_tokens": len(WORDS), "total_tokens": 10 + len(WORDS)},
    )
    yield "data: [DONE]\n\n"


async def chat_completions(request: Request):
    body = await request.json()
    model = body.get("model", "fast")
    if body.get("stream"):
        return StreamingResponse(_stream(model), media_type="text/event-stream")
    # non-streaming
    return JSONResponse(
        {
            "id": "chatcmpl-mock",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {"index": 0, "message": {"role": "assistant", "content": "".join(WORDS)}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": len(WORDS), "total_tokens": 10 + len(WORDS)},
        }
    )


app = Starlette(routes=[Route("/v1/chat/completions", chat_completions, methods=["POST"])])

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8090)
