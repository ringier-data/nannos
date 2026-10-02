"""Deleting a deployment drops its alias from the failover chains only once nothing serves it.

Another deployment under the same alias (a leftover duplicate from a re-registering edit whose
delete failed, a config-defined twin) keeps the alias live; dropping it from the chains then would
silently stop the tier failing over to a model that is still available (nannos#339).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import console_backend.routers.admin_model_gateway_router as router
import pytest


def _deployment(alias: str, model_id: str) -> dict:
    return {"model_name": alias, "model_info": {"id": model_id}}


class _Gateway:
    """Serves ``deployments`` until one is deleted; ``lagging`` keeps listing the deleted one, as a
    replica that has not caught up with the delete would."""

    def __init__(self, deployments: list[dict], lagging: bool = False):
        self.deployments = list(deployments)
        self.lagging = lagging

    async def get_model_by_id(self, model_id):
        return next((d for d in self.deployments if d["model_info"]["id"] == model_id), None)

    async def delete_model(self, model_id):
        if not self.lagging:
            self.deployments = [d for d in self.deployments if d["model_info"]["id"] != model_id]

    async def list_models(self):
        return self.deployments


def _request(gateway: _Gateway):
    defaults = SimpleNamespace(drop_alias_from_chains=AsyncMock(return_value=[]))
    state = SimpleNamespace(model_gateway_service=gateway, model_defaults_service=defaults)
    return SimpleNamespace(app=SimpleNamespace(state=state)), defaults


async def _delete(gateway: _Gateway, model_id: str):
    request, defaults = _request(gateway)
    await router.delete_model(model_id, request, AsyncMock(), user=SimpleNamespace(id="admin"))
    return defaults.drop_alias_from_chains


@pytest.mark.asyncio
async def test_deleting_one_of_two_deployments_keeps_the_alias_in_its_chains():
    gateway = _Gateway([_deployment("sonnet", "gw-1"), _deployment("sonnet", "gw-2")])
    drop = await _delete(gateway, "gw-1")
    drop.assert_not_awaited()


@pytest.mark.asyncio
async def test_deleting_the_last_deployment_drops_the_alias_from_its_chains():
    gateway = _Gateway([_deployment("sonnet", "gw-1"), _deployment("haiku", "gw-3")])
    drop = await _delete(gateway, "gw-1")
    drop.assert_awaited_once()
    assert drop.await_args.args[2] == "sonnet"


@pytest.mark.asyncio
async def test_a_replica_still_listing_the_deleted_deployment_does_not_count_it():
    gateway = _Gateway([_deployment("sonnet", "gw-1")], lagging=True)
    drop = await _delete(gateway, "gw-1")
    drop.assert_awaited_once()
