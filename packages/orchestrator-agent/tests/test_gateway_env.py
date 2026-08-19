"""Both env files must layer identically at collection time and at runtime.

The integration tier reads its environment twice — once at import to discover
models, once in a fixture to actually run — and when those two disagree the
failure points away from the cause. Two shipped disagreements are pinned below:
a key-level shadow that reported "No Model Gateway" for a valid split
configuration, and a file-level short-circuit that collected the tests and then
errored every one of them mid-call.

Deliberately pure — no conftest import, no gateway. These take explicit paths.
"""

from __future__ import annotations

import pytest

from tests.support.gateway_env import (
    CREDENTIAL_KEYS,
    FAKE_CREDENTIAL_VALUES,
    GATEWAY_KEYS,
    gateway_env,
    layered_env,
    load_env_file,
    restored_environment,
)

URL = "LLM_GATEWAY_URL"
KEY = "LLM_GATEWAY_API_KEY"


@pytest.fixture()
def files(tmp_path):
    """The two paths, neither existing yet. Write with ``write``."""

    class Files:
        integration = tmp_path / ".env.integration"
        orchestrator = tmp_path / ".env"

        def write(self, path, **pairs):
            path.write_text("".join(f"{k}={v}\n" for k, v in pairs.items()))

        def layered(self):
            return layered_env(self.integration, self.orchestrator)

        def gateway(self):
            return gateway_env(self.integration, self.orchestrator)

    return Files()


# ---------------------------------------------------------------------------
# The two reported failure modes
# ---------------------------------------------------------------------------


def test_a_partial_override_file_does_not_shadow_the_other_file(files):
    """Reported bug (a): a key-level shadow.

    Discovery used to return the first file containing *any* gateway key, so an
    override file holding only the API key hid the URL next door — and the tier
    skipped with "No Model Gateway" against a configuration that was fine.
    """
    files.write(files.orchestrator, LLM_GATEWAY_URL="http://localhost:4000")
    files.write(files.integration, LLM_GATEWAY_API_KEY="sk-override")

    assert files.gateway() == {
        URL: "http://localhost:4000",
        KEY: "sk-override",
    }


def test_a_stale_override_file_without_gateway_keys_is_harmless(files):
    """Reported bug (b): a file-level short-circuit.

    Credential restoration used to return on any non-empty override file while
    discovery fell through per file. A leftover .env.integration — easy to have,
    since the template it was copied from was deleted from this branch — let
    collection succeed via the orchestrator .env while runtime injected nothing,
    so every test died on `LLM_GATEWAY_URL is not set`.
    """
    files.write(
        files.orchestrator,
        LLM_GATEWAY_URL="http://localhost:4000",
        LLM_GATEWAY_API_KEY="sk-local",
    )
    files.write(files.integration, SOME_STALE_SETTING="1")

    layered = files.layered()
    assert layered[URL] == "http://localhost:4000"
    assert layered[KEY] == "sk-local"
    assert layered["SOME_STALE_SETTING"] == "1"

    # The part that actually broke: the runtime view must carry the coordinates.
    restored = restored_environment({}, files.integration, files.orchestrator)
    assert restored[URL] == "http://localhost:4000"


def test_discovery_and_runtime_agree_on_the_gateway(files):
    """The invariant behind both bugs, stated directly."""
    files.write(files.orchestrator, LLM_GATEWAY_URL="http://localhost:4000")
    files.write(files.integration, LLM_GATEWAY_API_KEY="sk-override", OTHER="x")

    discovery = files.gateway()
    runtime = restored_environment({}, files.integration, files.orchestrator)

    assert all(runtime[k] == v for k, v in discovery.items())


# ---------------------------------------------------------------------------
# Layering rules
# ---------------------------------------------------------------------------


def test_the_override_file_wins_per_key(files):
    files.write(files.orchestrator, LLM_GATEWAY_URL="http://from-orchestrator", LLM_GATEWAY_API_KEY="sk-a")
    files.write(files.integration, LLM_GATEWAY_URL="http://from-override")

    layered = files.layered()
    assert layered[URL] == "http://from-override"
    assert layered[KEY] == "sk-a"  # not clobbered by the partial override


def test_the_orchestrator_env_is_filtered_to_credentials(files):
    """It is generated app config; injecting it wholesale would be a wide blast radius."""
    files.write(
        files.orchestrator,
        LLM_GATEWAY_URL="http://localhost:4000",
        DATABASE_URL="postgres://nope",
        PORT="8080",
    )

    layered = files.layered()
    assert layered[URL] == "http://localhost:4000"
    assert "DATABASE_URL" not in layered
    assert "PORT" not in layered


def test_the_override_file_is_not_filtered(files):
    """Developer-authored for these tests, so everything in it is intentional."""
    files.write(files.integration, DATABASE_URL="postgres://test", ANYTHING="1")

    layered = files.layered()
    assert layered["DATABASE_URL"] == "postgres://test"
    assert layered["ANYTHING"] == "1"


def test_gateway_keys_are_credential_keys():
    """Otherwise the orchestrator .env filter would drop the coordinates silently."""
    assert set(GATEWAY_KEYS) <= CREDENTIAL_KEYS


def test_no_files_is_empty_not_an_error(files):
    assert files.layered() == {}
    assert files.gateway() == {}


# ---------------------------------------------------------------------------
# Fake credential stripping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("key", "fake"), sorted(FAKE_CREDENTIAL_VALUES.items()))
def test_placeholders_are_stripped_so_sdks_use_default_chains(files, key, fake):
    restored = restored_environment({key: fake}, files.integration, files.orchestrator)

    assert key not in restored


def test_placeholders_are_stripped_even_when_an_override_file_exists(files):
    """Previously the early return on a non-empty override file skipped this."""
    files.write(files.integration, LLM_GATEWAY_API_KEY="sk-override")

    restored = restored_environment(
        {"AWS_ACCESS_KEY_ID": "testing"}, files.integration, files.orchestrator
    )

    assert "AWS_ACCESS_KEY_ID" not in restored
    assert restored[KEY] == "sk-override"


def test_a_real_credential_that_is_not_the_placeholder_survives(files):
    restored = restored_environment(
        {"AWS_ACCESS_KEY_ID": "AKIAREAL"}, files.integration, files.orchestrator
    )

    assert restored["AWS_ACCESS_KEY_ID"] == "AKIAREAL"


def test_unrelated_environment_is_passed_through(files):
    restored = restored_environment({"HOME": "/Users/x"}, files.integration, files.orchestrator)

    assert restored["HOME"] == "/Users/x"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_comments_blanks_and_quotes(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                '  LLM_GATEWAY_URL = "http://localhost:4000"  ',
                "LLM_GATEWAY_API_KEY='sk-quoted'",
                "MALFORMED_NO_EQUALS",
                "=novalue",
            ]
        )
    )

    parsed = load_env_file(path)
    assert parsed == {
        URL: "http://localhost:4000",
        KEY: "sk-quoted",
    }


def test_a_missing_file_is_empty(tmp_path):
    assert load_env_file(tmp_path / "nope") == {}
