"""The session and OAuth-state cookie names are overridable (ADR-0016: side-by-side local stacks)."""

from console_backend.config import Config


def test_cookie_names_default(monkeypatch):
    monkeypatch.delenv("SESSION_COOKIE_NAME", raising=False)
    monkeypatch.delenv("OAUTH_STATE_COOKIE_NAME", raising=False)

    config = Config()

    assert config.cookie_name == "a2a-chatui"
    assert config.oauth_state_cookie_name == "session"


def test_cookie_names_from_env(monkeypatch):
    monkeypatch.setenv("SESSION_COOKIE_NAME", "a2a-chatui-s3")
    monkeypatch.setenv("OAUTH_STATE_COOKIE_NAME", "session-s3")

    config = Config()

    assert config.cookie_name == "a2a-chatui-s3"
    assert config.oauth_state_cookie_name == "session-s3"
