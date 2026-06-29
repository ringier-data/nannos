"""Tests for whitelisted ``{{TOKEN}}`` system-prompt placeholder resolution."""

import pytest

from app.core.prompt_placeholders import resolve_prompt_placeholders


def test_resolves_console_frontend_url_from_env(monkeypatch):
    """A ``{{CONSOLE_FRONTEND_URL}}`` placeholder is replaced with the env value."""
    monkeypatch.setenv("CONSOLE_FRONTEND_URL", "https://console.example.com")

    result = resolve_prompt_placeholders("Open {{CONSOLE_FRONTEND_URL}}/app/scheduler/{job_id}")

    assert result == "Open https://console.example.com/app/scheduler/{job_id}"


def test_strips_trailing_slashes_from_resolved_url(monkeypatch):
    """Trailing slashes are stripped so prompts can append their own path."""
    monkeypatch.setenv("CONSOLE_FRONTEND_URL", "https://console.example.com///")

    result = resolve_prompt_placeholders("link {{CONSOLE_FRONTEND_URL}}/foo")

    assert result == "link https://console.example.com/foo"


def test_uses_dev_fallback_when_env_unset(monkeypatch):
    """Without the env var, the dev fallback keeps local prompts working."""
    monkeypatch.delenv("CONSOLE_FRONTEND_URL", raising=False)

    result = resolve_prompt_placeholders("go to {{CONSOLE_FRONTEND_URL}}")

    assert result == "go to http://localhost:5173"


def test_replaces_all_occurrences(monkeypatch):
    """Every occurrence of a placeholder is substituted, not just the first."""
    monkeypatch.setenv("CONSOLE_FRONTEND_URL", "https://c.example.com")

    result = resolve_prompt_placeholders("{{CONSOLE_FRONTEND_URL}} and {{CONSOLE_FRONTEND_URL}}")

    assert result == "https://c.example.com and https://c.example.com"


def test_resolved_at_call_time_not_import_time(monkeypatch):
    """Resolution reads the environment on each call, keeping configs portable."""
    monkeypatch.setenv("CONSOLE_FRONTEND_URL", "https://first.example.com")
    assert resolve_prompt_placeholders("{{CONSOLE_FRONTEND_URL}}") == "https://first.example.com"

    monkeypatch.setenv("CONSOLE_FRONTEND_URL", "https://second.example.com")
    assert resolve_prompt_placeholders("{{CONSOLE_FRONTEND_URL}}") == "https://second.example.com"


def test_single_brace_literals_left_untouched(monkeypatch):
    """Single-brace prompt literals (e.g. ``{job_id}``) are not treated as placeholders."""
    monkeypatch.setenv("CONSOLE_FRONTEND_URL", "https://c.example.com")

    prompt = "fill {scheduled_job_id} then visit {CONSOLE_FRONTEND_URL}"
    result = resolve_prompt_placeholders(prompt)

    assert result == prompt


def test_unknown_double_brace_token_left_intact(monkeypatch):
    """Non-whitelisted ``{{TOKEN}}`` placeholders pass through unchanged."""
    monkeypatch.setenv("CONSOLE_FRONTEND_URL", "https://c.example.com")

    result = resolve_prompt_placeholders("{{SOME_OTHER_TOKEN}} stays")

    assert result == "{{SOME_OTHER_TOKEN}} stays"


@pytest.mark.parametrize("prompt", ["", "no placeholders here"])
def test_passthrough_when_nothing_to_resolve(prompt):
    """Empty or placeholder-free prompts are returned unchanged."""
    assert resolve_prompt_placeholders(prompt) == prompt
