"""Foundry cost-attribution config-version wiring.

Foundry agents report cost via the manual report_llm_usage path (not the gateway),
so the running config-version id must be threaded onto the runnable and surfaced via
get_sub_agent_config_version_id, mirroring DynamicLocalAgentRunnable.
"""

from agent_common.a2a.models import LocalFoundrySubAgentConfig
from agent_common.agents.foundry_agent import FoundryLocalAgentRunnable, create_foundry_local_subagent


def _config() -> LocalFoundrySubAgentConfig:
    return LocalFoundrySubAgentConfig(
        name="data-analyst",
        description="Analyzes data",
        client_id="cid",
        client_secret_ref="/ssm/secret",
        ontology_rid="ri.ontology.main.ontology.x",
        query_api_name="a2aAgent",
        scopes=["api:use-ontologies-read"],
    )


def test_config_carries_config_version_field():
    cfg = LocalFoundrySubAgentConfig(**{**_config().model_dump(), "sub_agent_config_version_id": 7})
    assert cfg.sub_agent_config_version_id == 7


def test_runnable_surfaces_config_version():
    # backend_url=None → skip cost-tracking setup (no network); we only assert the hook.
    runnable = FoundryLocalAgentRunnable(
        config=_config(), user={"sub": "u1"}, sub_agent_config_version_id=55
    )
    assert runnable.get_sub_agent_config_version_id(input_data=None) == 55


def test_runnable_config_version_defaults_none():
    runnable = FoundryLocalAgentRunnable(config=_config(), user={"sub": "u1"})
    assert runnable.get_sub_agent_config_version_id(input_data=None) is None


def test_factory_threads_config_version_to_runnable():
    compiled = create_foundry_local_subagent(
        config=_config(), user={"sub": "u1"}, sub_agent_config_version_id=88
    )
    assert compiled["runnable"].get_sub_agent_config_version_id(input_data=None) == 88
