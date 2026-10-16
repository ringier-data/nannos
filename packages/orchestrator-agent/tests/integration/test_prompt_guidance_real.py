"""Does the orchestrator still behave without deepagents' prompt prose? Real tier.

deepagents 0.7 stopped injecting its base prompt, the task-tool usage prose and the
filesystem / large-result guidance. This runs the behaviours that prose taught
(``tests/datasets/prompt_guidance.yaml``) against real models, ``PROMPT_AB_RUNS``
times each (default 3), so the pass ratio can be compared between a checkout on
deepagents 0.6 and one on 0.7. A single failure is a data point, not a verdict.

Each run appends one JSON line to ``$PROMPT_AB_LOG`` when set, for aggregation:
    PROMPT_AB_RUNS=5 PROMPT_AB_LOG=/tmp/ab.jsonl RUN_INTEGRATION_TESTS=1 \\
        uv run pytest tests/integration/test_prompt_guidance_real.py -m integration -n 4
"""

from __future__ import annotations

import json
import os
import time

import pytest
from agent_common.models.base import ModelType

from tests.support.scenarios import assert_scenario, describe_outcome, load_scenarios

from .conftest import one_model_per_behaviour
from .test_scenarios_real import run_turn

SCENARIOS = load_scenarios("prompt_guidance.yaml")
RUNS = int(os.getenv("PROMPT_AB_RUNS", "3"))

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _log(record: dict) -> None:
    path = os.getenv("PROMPT_AB_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")


@pytest.mark.parametrize("run", range(RUNS))
@pytest.mark.parametrize("model_type", one_model_per_behaviour())
@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.id for s in SCENARIOS])
async def test_prompt_guidance_behaviour(
    scenario,
    model_type: ModelType,
    run: int,
    patched_agent,
    user_config_with_subagents,
    make_config,
):
    agents = scenario.mock_subagents()
    user_config = user_config_with_subagents(*agents)
    user_config.model = model_type

    started = time.monotonic()
    state = await run_turn(patched_agent, scenario.query, user_config, make_config(model_type), model_type)
    outcome = describe_outcome(state)
    record = {"scenario": scenario.id, "model": model_type, "run": run, "seconds": round(time.monotonic() - started, 1)}
    try:
        assert_scenario(scenario, state, agents)
    except AssertionError as exc:
        _log({**record, "passed": False, "reason": str(exc)[:300], "outcome": outcome})
        print(f"\n[{scenario.id} / {model_type} / run {run}] outcome: {outcome}")
        raise
    _log({**record, "passed": True, "outcome": outcome})
