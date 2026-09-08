"""CORS_ALLOWED_CHAT_ORIGINS: one allowlist, exact origins + `*` wildcard patterns, consumed
identically by REST CORS (Starlette allow_origin_regex) and the Socket.IO handshake
(engineio callable). Wildcards exist for per-PR cockpit environments (pr-123-riad.d.alloy.ch)."""

import logging

import pytest
from starlette.applications import Starlette
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from console_backend.cors_origins import parse_origin_allowlist

ENTRIES = [
    "https://riad.alloy.ch",
    " http://localhost:3000/ ",
    "https://pr-*-riad.d.alloy.ch",
    "",
    "https://riad.alloy.ch",
]


def test_parse_splits_exact_and_patterns_and_dedupes():
    allow = parse_origin_allowlist(ENTRIES)
    assert allow.exact == ("https://riad.alloy.ch", "http://localhost:3000")
    assert allow.patterns == ("https://pr-*-riad.d.alloy.ch",)
    assert len(allow) == 3


@pytest.mark.parametrize(
    ("origin", "expected"),
    [
        ("https://riad.alloy.ch", True),
        ("http://localhost:3000", True),
        ("https://pr-123-riad.d.alloy.ch", True),
        ("https://pr-7-riad.d.alloy.ch", True),
        ("https://riad.d.alloy.ch", False),  # not listed, and not a PR host
        ("https://pr-123-riad.d.alloy.ch.evil.com", False),  # anchored
        ("https://evil.com/pr-1-riad.d.alloy.ch", False),
        ("https://pr-1.2-riad.d.alloy.ch", False),  # `*` never crosses a dot
        ("https://pr--riad.d.alloy.ch", False),  # `*` needs at least one char
        ("http://pr-123-riad.d.alloy.ch", False),  # scheme is part of the origin
        (None, False),
        ("", False),
    ],
)
def test_is_allowed(origin, expected):
    assert parse_origin_allowlist(ENTRIES).is_allowed(origin) is expected


def test_no_patterns_means_no_regex():
    allow = parse_origin_allowlist(["https://riad.alloy.ch"])
    assert allow.regex is None
    assert allow.is_allowed("https://pr-1-riad.d.alloy.ch") is False


def test_starlette_cors_honours_the_pattern_and_echoes_the_requesting_origin():
    """The REST leg: a preflight from a PR host is allowed via allow_origin_regex, and the
    echoed Access-Control-Allow-Origin is the exact requesting origin (credentials mode)."""
    allow = parse_origin_allowlist(ENTRIES)
    app = Starlette(
        routes=[Route("/ping", lambda r: PlainTextResponse("ok"), methods=["GET"])]
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(allow.exact),
        allow_origin_regex=allow.regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    client = TestClient(app)

    for origin in ("https://pr-123-riad.d.alloy.ch", "https://riad.alloy.ch"):
        res = client.options(
            "/ping", headers={"Origin": origin, "Access-Control-Request-Method": "GET"}
        )
        assert res.status_code == 200, origin
        assert res.headers["access-control-allow-origin"] == origin
        assert res.headers["access-control-allow-credentials"] == "true"

    denied = client.options(
        "/ping",
        headers={
            "Origin": "https://riad.d.alloy.ch",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert denied.status_code == 400


def test_socketio_callable_allows_only_matching_origins():
    """The socket leg: engineio calls our callable per handshake with the Origin header and
    echoes only that origin back when we say yes."""
    import socketio

    allow = parse_origin_allowlist(ENTRIES)
    sio = socketio.AsyncServer(
        async_mode="asgi",
        cors_allowed_origins=lambda origin, environ=None: allow.is_allowed(origin),
    )

    assert sio.eio._cors_allowed_origins(
        {"HTTP_ORIGIN": "https://pr-42-riad.d.alloy.ch"}
    ) == ["https://pr-42-riad.d.alloy.ch"]
    assert sio.eio._cors_allowed_origins({"HTTP_ORIGIN": "https://riad.alloy.ch"}) == [
        "https://riad.alloy.ch"
    ]
    assert sio.eio._cors_allowed_origins({"HTTP_ORIGIN": "https://evil.com"}) == []


def test_config_reads_new_name_and_falls_back_to_deprecated_one(monkeypatch, caplog):
    import console_backend.config as config_module

    monkeypatch.delenv("CORS_ALLOWED_CHAT_ORIGINS", raising=False)
    monkeypatch.delenv("EMBED_ALLOWED_ORIGINS", raising=False)
    assert config_module._read_cors_allowed_chat_origins() == []

    monkeypatch.setenv(
        "EMBED_ALLOWED_ORIGINS", "https://old.example, https://old2.example"
    )
    with caplog.at_level(logging.WARNING):
        assert config_module._read_cors_allowed_chat_origins() == [
            "https://old.example",
            "https://old2.example",
        ]
    assert "EMBED_ALLOWED_ORIGINS is deprecated" in caplog.text

    monkeypatch.setenv(
        "CORS_ALLOWED_CHAT_ORIGINS", "https://new.example,https://pr-*-riad.d.alloy.ch"
    )
    assert config_module._read_cors_allowed_chat_origins() == [
        "https://new.example",
        "https://pr-*-riad.d.alloy.ch",
    ]
