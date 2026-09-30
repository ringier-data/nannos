"""The model picker list: one entry per alias, whatever the gateway's deployment count."""

from console_backend.routers.models_router import _picker_models


def _deployment(name: str, *, db: bool, label: str | None = None, mode: str = "chat") -> dict:
    info: dict = {"mode": mode, "db_model": db}
    if label:
        info["label"] = label
    return {"model_name": name, "model_info": info}


def test_an_alias_in_config_and_db_is_listed_once_with_the_db_record():
    raw = [
        _deployment("gpt-4o", db=False),
        _deployment("gpt-6-sol", db=False),
        _deployment("gpt-6-sol", db=True, label="GPT-6 Sol"),
    ]

    models = _picker_models(raw, default_model=None)

    assert [m.value for m in models] == ["gpt-4o", "gpt-6-sol"]
    assert models[1].label == "GPT-6 Sol"


def test_the_db_record_wins_even_when_listed_first():
    raw = [_deployment("m", db=True, label="From DB"), _deployment("m", db=False, label="From config")]

    assert [m.label for m in _picker_models(raw, default_model=None)] == ["From DB"]


def test_repeated_config_deployments_keep_the_first():
    raw = [_deployment("m", db=False, label="first"), _deployment("m", db=False, label="second")]

    assert [m.label for m in _picker_models(raw, default_model=None)] == ["first"]


def test_non_chat_models_stay_out():
    raw = [_deployment("embed", db=True, mode="embedding"), _deployment("chat", db=False)]

    assert [m.value for m in _picker_models(raw, default_model="chat")] == ["chat"]


def test_a_non_chat_db_deployment_does_not_hide_the_chat_one():
    """Filtered before deduplicated (review round 8): the DB entry is an embedding, so the chat
    config deployment of the same alias is the one the picker lists."""
    raw = [_deployment("m", db=False, label="Chat"), _deployment("m", db=True, label="Embed", mode="embedding")]

    assert [m.label for m in _picker_models(raw, default_model=None)] == ["Chat"]
