"""Host-published agent definitions (ADR-0006): fetching, digest pinning, validation, caching.

The fixture tree is generated from a small dict per test — no cockpit files are vendored."""

import hashlib
import json

import httpx
import pytest
import respx

from console_backend.services import well_known_agent as wk
from console_backend.services.well_known_agent import (
    FALLBACK_AGENT_NAME,
    WellKnownAgentClient,
    WellKnownFetchError,
    compose_system_prompt,
    compute_revision,
    parse_cache_ttl,
    thinking_params,
    to_sub_agent_name,
    version_hash_for,
)

BASE = "https://riad.example"
WK = "/.well-known/agent-skills"
INDEX_URL = f"{BASE}{WK}/index.json"
SCHEMA = "https://schemas.agentskills.io/discovery/0.2.0/schema.json"


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def skill_md(
    name: str, body: str = "Do the thing.", description: str = "Use when booking."
) -> bytes:
    return f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n".encode()


def agent_md(body: str = "You help campaign managers.") -> bytes:
    return (
        "---\nname: Alloy AI Assistant\ndescription: Helps with campaigns.\ntools:\n  - list_campaigns\n---\n\n"
        f"{body}\n"
    ).encode()


def build_tree(
    *,
    skills=None,
    skill_paths=None,
    agent_bytes=None,
    extension=None,
    schema=SCHEMA,
    drop_extension=False,
    no_tools=False,
):
    """Return (index_bytes, {url: bytes}) for a consistent tree. `skill_paths` overrides a skill's directory."""
    skills = (
        {"book-line-items": skill_md("book-line-items")} if skills is None else skills
    )
    skill_paths = skill_paths or {}
    agent_bytes = agent_md() if agent_bytes is None else agent_bytes
    files: dict[str, bytes] = {}
    entries = []
    for name, content in skills.items():
        path = skill_paths.get(name, f"{WK}/{name}/SKILL.md")
        files[BASE + path] = content
        entries.append(
            {
                "name": name,
                "type": "skill-md",
                "description": f"Use {name}.",
                "url": path,
                "digest": digest(content),
            }
        )
    prompt_path = f"{WK}/AGENT.md"
    files[BASE + prompt_path] = agent_bytes
    ext = {
        "name": "Alloy AI Assistant",
        "description": "Helps with campaigns.",
        "prompt": {"url": prompt_path, "digest": digest(agent_bytes)},
        "tools": ["list_campaigns", "get_campaign"],
    }
    if extension:
        ext.update(extension)
    if no_tools:
        ext.pop("tools", None)
    index = {"$schema": schema, "skills": entries}
    if not drop_extension:
        index["x-nannos-agent"] = ext
    return json.dumps(index).encode(), files


def _fresh(content: bytes, headers=None):
    """A new Response per call: the client streams bodies, and a streamed body is consumed."""
    return lambda request: httpx.Response(200, content=content, headers=headers or {})


def mount(router, index_bytes, files, *, index_headers=None):
    """Mount the tree; returns the index route (re-calling router.get() would re-register it)."""
    index_route = router.get(INDEX_URL).mock(
        side_effect=_fresh(index_bytes, index_headers)
    )
    for url, content in files.items():
        router.get(url).mock(side_effect=_fresh(content))
    return index_route


async def fetch_error(client, **tree_kwargs) -> WellKnownFetchError:
    index_bytes, files = build_tree(**tree_kwargs)
    with respx.mock(assert_all_called=False) as router:
        mount(router, index_bytes, files)
        with pytest.raises(WellKnownFetchError) as info:
            await client.fetch(BASE, force=True)
    assert client._cache == {}, "a failed fetch must not be cached"
    return info.value


@pytest.mark.asyncio
async def test_fetch_happy_path():
    client = WellKnownAgentClient()
    index_bytes, files = build_tree(
        extension={
            "organization": "Ringier Advertising",
            "model_tier": "premium",
            "thinking_level": "high",
        }
    )
    with respx.mock(assert_all_called=True) as router:
        mount(router, index_bytes, files)
        definition = await client.fetch(BASE)

    assert definition.base_url == BASE and definition.index_url == INDEX_URL
    assert definition.agent.name == "Alloy AI Assistant"
    assert definition.agent.organization == "Ringier Advertising"
    assert definition.agent.tools == ["list_campaigns", "get_campaign"]
    assert (
        definition.agent.model_tier == "premium"
        and definition.agent.thinking_level == "high"
    )
    assert definition.agent.prompt_body == "You help campaign managers."
    assert [s.name for s in definition.skills] == ["book-line-items"]
    assert definition.skills[0].body == "Do the thing."
    assert (
        definition.skills[0].description == "Use book-line-items."
    )  # from the index, not the file
    assert len(definition.revision) == 16 and int(definition.revision, 16) >= 0
    assert definition.ttl_seconds == wk.DEFAULT_TTL_SECONDS  # no Cache-Control header
    assert version_hash_for(definition.revision) == "wk" + definition.revision[:10]


@pytest.mark.asyncio
async def test_revision_is_stable_and_tracks_every_byte():
    client = WellKnownAgentClient()
    index_bytes, files = build_tree()
    with respx.mock(assert_all_called=False) as router:
        mount(router, index_bytes, files)
        first = await client.fetch(BASE, force=True)
        second = await client.fetch(BASE, force=True)
    assert first.revision == second.revision

    changed_index, changed_files = build_tree(
        skills={
            "book-line-items": skill_md(
                "book-line-items", body="Do the thing, carefully."
            )
        }
    )
    with respx.mock(assert_all_called=False) as router:
        mount(router, changed_index, changed_files)
        third = await client.fetch(BASE, force=True)
    assert third.revision != first.revision


@pytest.mark.asyncio
async def test_tools_are_optional_and_stay_nannos_side_when_absent():
    client = WellKnownAgentClient()
    index_bytes, files = build_tree(no_tools=True)
    with respx.mock(assert_all_called=False) as router:
        mount(router, index_bytes, files)
        definition = await client.fetch(BASE, force=True)
    assert (
        definition.agent.tools is None
    )  # the cockpit publishes no tool list (decided on the Nannos side)

    err = await fetch_error(WellKnownAgentClient(), extension={"tools": []})
    assert "when published" in err.detail


def test_framing_template_version_is_part_of_the_revision():
    prompt, skills = "sha256:" + "a" * 64, ["sha256:" + "b" * 64]
    assert compute_revision(prompt, skills, framing_version="1") != compute_revision(
        prompt, skills, framing_version="2"
    )
    # order of skills does not matter
    assert compute_revision(
        prompt, ["sha256:" + "c" * 64, "sha256:" + "b" * 64]
    ) == compute_revision(prompt, ["sha256:" + "b" * 64, "sha256:" + "c" * 64])


@pytest.mark.asyncio
async def test_digest_mismatch_is_a_hard_error():
    client = WellKnownAgentClient()
    index_bytes, files = build_tree()
    files[f"{BASE}{WK}/book-line-items/SKILL.md"] = skill_md(
        "book-line-items", body="tampered"
    )
    with respx.mock(assert_all_called=False) as router:
        mount(router, index_bytes, files)
        with pytest.raises(WellKnownFetchError) as info:
            await client.fetch(BASE)
    assert info.value.step == "skill:book-line-items"
    assert "digest mismatch" in info.value.detail
    assert client._cache == {}


@pytest.mark.asyncio
async def test_same_origin_redirect_is_followed_and_cross_origin_refused():
    client = WellKnownAgentClient()
    index_bytes, files = build_tree()
    prompt_url = f"{BASE}{WK}/AGENT.md"
    prompt_bytes = files.pop(prompt_url)
    with respx.mock(assert_all_called=False) as router:
        mount(router, index_bytes, files)
        router.get(prompt_url).mock(
            return_value=httpx.Response(
                302, headers={"location": f"{WK}/moved/AGENT.md"}
            )
        )
        moved = router.get(f"{BASE}{WK}/moved/AGENT.md").mock(
            side_effect=_fresh(prompt_bytes)
        )
        definition = await client.fetch(BASE, force=True)
    assert moved.call_count == 1
    assert definition.agent.prompt_body == "You help campaign managers."
    assert (
        definition.agent.url == prompt_url
    )  # the index-declared URL is what admins see

    with respx.mock(assert_all_called=False) as router:
        mount(router, index_bytes, files)
        router.get(prompt_url).mock(
            return_value=httpx.Response(
                302, headers={"location": "https://evil.example/AGENT.md"}
            )
        )
        with pytest.raises(WellKnownFetchError) as info:
            await client.fetch(BASE, force=True)
    assert info.value.step == "agent" and "cross-origin" in info.value.detail


@pytest.mark.asyncio
async def test_cross_origin_skill_url_in_index_is_refused():
    client = WellKnownAgentClient()
    index_bytes, files = build_tree()
    index = json.loads(index_bytes)
    index["skills"][0]["url"] = "https://evil.example/SKILL.md"
    with respx.mock(assert_all_called=False) as router:
        mount(router, json.dumps(index).encode(), files)
        with pytest.raises(WellKnownFetchError) as info:
            await client.fetch(BASE)
    assert "cross-origin" in info.value.detail


@pytest.mark.asyncio
async def test_oversize_index_is_refused():
    client = WellKnownAgentClient()
    index_bytes, files = build_tree()
    index = json.loads(index_bytes)
    index["padding"] = "x" * (wk.MAX_INDEX_BYTES + 1)
    with respx.mock(assert_all_called=False) as router:
        mount(router, json.dumps(index).encode(), files)
        with pytest.raises(WellKnownFetchError) as info:
            await client.fetch(BASE)
    assert info.value.step == "index" and "larger than" in info.value.detail


@pytest.mark.asyncio
async def test_non_200_is_an_error_with_the_status():
    client = WellKnownAgentClient()
    with respx.mock(assert_all_called=False) as router:
        router.get(INDEX_URL).mock(return_value=httpx.Response(503))
        with pytest.raises(WellKnownFetchError) as info:
            await client.fetch(BASE)
    assert "HTTP 503" in info.value.detail


@pytest.mark.asyncio
async def test_index_shape_errors_are_distinct():
    client = WellKnownAgentClient()
    too_many = {f"skill-{i}": skill_md(f"skill-{i}") for i in range(21)}
    err = await fetch_error(client, skills=too_many)
    assert "maximum is 20" in err.detail

    err = await fetch_error(client, skills={"Bad_Name": skill_md("Bad_Name")})
    assert "lowercase letters" in err.detail

    err = await fetch_error(client, drop_extension=True)
    assert "x-nannos-agent" in err.detail

    err = await fetch_error(client, schema="https://example.com/other-schema.json")
    assert "unsupported $schema" in err.detail

    err = await fetch_error(client, extension={"model_tier": "gigantic"})
    assert "model_tier" in err.detail

    err = await fetch_error(client, extension={"thinking_level": "maximum"})
    assert "thinking_level" in err.detail

    err = await fetch_error(
        client, extension={"tools": ["list_campaigns", "list_campaigns"]}
    )
    assert "listed twice" in err.detail

    err = await fetch_error(client, extension={"tools": ["Not Snake"]})
    assert "snake_case" in err.detail


@pytest.mark.asyncio
async def test_skill_file_errors_are_distinct():
    client = WellKnownAgentClient()
    # frontmatter name differs from the index entry
    err = await fetch_error(client, skills={"book-line-items": skill_md("other-name")})
    assert "does not match the index entry" in err.detail

    # served from a directory that is not the skill's name
    err = await fetch_error(
        client, skill_paths={"book-line-items": f"{WK}/elsewhere/SKILL.md"}
    )
    assert "directory named 'book-line-items'" in err.detail

    err = await fetch_error(client, skills={"book-line-items": b"no frontmatter here"})
    assert "frontmatter" in err.detail

    err = await fetch_error(
        client,
        skills={
            "book-line-items": skill_md("book-line-items", body="Use {{SECRET}} here")
        },
    )
    assert "'{{'" in err.detail


@pytest.mark.asyncio
async def test_prompt_placeholder_and_empty_body_refused():
    client = WellKnownAgentClient()
    err = await fetch_error(client, agent_bytes=agent_md(body="Hello {{USER_NAME}}"))
    assert err.step == "agent" and "'{{'" in err.detail

    err = await fetch_error(client, agent_bytes=b"---\nname: x\n---\n")
    assert err.step == "agent" and "no prompt body" in err.detail


@pytest.mark.asyncio
async def test_cache_honours_max_age_and_force():
    client = WellKnownAgentClient()
    index_bytes, files = build_tree()
    with respx.mock(assert_all_called=False) as router:
        index_route = mount(
            router,
            index_bytes,
            files,
            index_headers={"Cache-Control": "public, max-age=30"},
        )
        first = await client.fetch(BASE)
        assert first.ttl_seconds == wk.MIN_TTL_SECONDS  # 30 s floored to 60 s
        assert index_route.call_count == 1

        second = await client.fetch(BASE)  # within TTL: served from cache, no HTTP
        assert second is first
        assert index_route.call_count == 1

        third = await client.fetch(BASE, force=True)  # force bypasses the cache
        assert third.revision == first.revision
        assert index_route.call_count == 2

    client.invalidate(BASE)
    assert client._cache == {}


def test_parse_cache_ttl_clamps():
    assert parse_cache_ttl(None) == wk.DEFAULT_TTL_SECONDS
    assert parse_cache_ttl("no-store") == wk.DEFAULT_TTL_SECONDS
    assert parse_cache_ttl("max-age=30") == wk.MIN_TTL_SECONDS
    assert parse_cache_ttl("public, max-age=3000") == 3000
    assert parse_cache_ttl("max-age=86400") == wk.MAX_TTL_SECONDS


def test_thinking_params_mapping():
    assert thinking_params(None) == (None, None)
    assert thinking_params("off") == (False, None)
    enabled, level = thinking_params("high")
    assert enabled is True and level is not None and level.value == "high"


def test_compose_system_prompt_frames_before_the_host_prompt():
    agent = wk.WellKnownAgent(
        name="Alloy AI Assistant",
        description="Helps with campaigns.",
        organization="Ringier Advertising",
        prompt_body="Domain guidance here.",
        tools=["list_campaigns"],
        url=f"{BASE}{WK}/AGENT.md",
        digest="sha256:" + "a" * 64,
    )
    prompt = compose_system_prompt(BASE, agent, "abcdef0123456789")
    framing, _, body = prompt.partition("\n\n" + "Domain guidance here.")
    assert framing.startswith("You are Nannos, Ringier's AI assistant.")
    assert BASE in framing and "operated by Ringier Advertising" in framing
    assert '"Alloy AI Assistant"' in framing and "revision abcdef0123456789" in framing
    assert body == ""  # nothing after the host prompt

    agent_no_org = agent.model_copy(update={"organization": None})
    assert "operated by" not in compose_system_prompt(
        BASE, agent_no_org, "abcdef0123456789"
    )


@pytest.mark.parametrize(
    "published,expected",
    [
        # The case that broke dev: a display name with spaces reached the sub_agents row,
        # and the orchestrator's config model rejected it on every turn thereafter.
        ("Alloy AI Assistant", "Alloy-AI-Assistant"),
        ("data-analyst", "data-analyst"),
        ("Campaign  Helper", "Campaign-Helper"),  # a run collapses to one hyphen
        ("  Padded Name  ", "Padded-Name"),
        ("Ringier's Assistant", "Ringier-s-Assistant"),
        ("3rd Party Bot", "rd-Party-Bot"),  # a name must start with a letter
        ("Wetter ☀ Agent", "Wetter-Agent"),  # trailing separator run is trimmed
        ("agent_v2", "agent_v2"),  # underscores are already legal
        ("123", FALLBACK_AGENT_NAME),  # no letter to start from
        ("", FALLBACK_AGENT_NAME),
    ],
)
def test_to_sub_agent_name_derives_a_name_a_sub_agent_row_can_hold(published, expected):
    assert to_sub_agent_name(published) == expected


def test_to_sub_agent_name_output_is_always_accepted_by_the_console_models():
    from console_backend.models.sub_agent import SUB_AGENT_NAME_RE

    for published in [
        "Alloy AI Assistant",
        "3rd Party Bot",
        "!!!",
        "x" * 200,
        "Ünïcödé Agent",
        "-leading-hyphen",
    ]:
        derived = to_sub_agent_name(published)
        assert SUB_AGENT_NAME_RE.match(derived), f"{published!r} -> {derived!r}"
        assert 1 <= len(derived) <= 64


def test_well_known_agent_keeps_the_published_name_for_display():
    """The transform is for the row name only; people still see what the host published."""
    agent = wk.WellKnownAgent(
        name="Alloy AI Assistant",
        description="Helps with campaigns.",
        prompt_body="Domain guidance here.",
        url=f"{BASE}{WK}/AGENT.md",
        digest="sha256:" + "a" * 64,
    )
    assert agent.name == "Alloy AI Assistant"
    assert agent.sub_agent_name == "Alloy-AI-Assistant"
    assert '"Alloy AI Assistant"' in compose_system_prompt(BASE, agent, "abc123")
