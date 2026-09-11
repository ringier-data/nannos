"""Where the integration tier's gateway coordinates come from.

Two files can supply them and they must layer the same way *everywhere*, which
is the whole reason this is a module rather than a couple of helpers in the
conftest. The tier reads its environment at two separate moments:

- **at import**, to discover which models the gateway serves (parametrize needs
  the list at collection time), and
- **at runtime**, in the ``integration_environment`` fixture, because the import
  path deliberately rolls its mutations back rather than leaking gateway
  variables into every other test in the session.

When those two disagree, the failure is silent and points away from the cause.
Two such disagreements were shipped and reported:

*A key-level shadow.* Discovery used to return the first file that contained
*any* gateway key, so a ``.env.integration`` holding only ``LLM_GATEWAY_API_KEY``
hid the ``LLM_GATEWAY_URL`` sitting in the orchestrator ``.env``. A valid split
configuration reported "No Model Gateway".

*A file-level short-circuit.* Credential restoration used to return on any
non-empty ``.env.integration`` while discovery fell through per file. So a stale
``.env.integration`` with no gateway keys — easy to have, since the
``.env.integration.template`` it was copied from was deleted from this branch —
let discovery succeed via the orchestrator ``.env`` and collect the tests, while
runtime never re-injected the variables. Every test then died mid-call with
``RuntimeError: LLM_GATEWAY_URL is not set``, blaming configuration the
developer demonstrably had.

Both are the same bug: layering that is per-file in one place and per-key in
another. Everything here is pure and takes explicit paths, so the layering can
be tested without a gateway and without a conftest import.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

# The two coordinates that decide whether the tier can run at all.
GATEWAY_KEYS = ("LLM_GATEWAY_URL", "LLM_GATEWAY_API_KEY")

# What may be taken from the orchestrator ``.env``. That file is generated app
# config — database URLs, ports, feature flags — and injecting it wholesale into
# the test process would be a much larger blast radius than reading two
# coordinates. ``.env.integration`` is developer-authored for these tests, so it
# is *not* filtered; everything in it is there on purpose.
CREDENTIAL_KEYS = frozenset(
    {
        # All LLM traffic goes through the gateway, so these two are what
        # integration tests actually need. Without them in this whitelist the
        # values are silently dropped and the failure looks like a missing
        # provider credential, which it is not.
        *GATEWAY_KEYS,
        # Per-provider credentials belong to the gateway process, not to the
        # tests. Kept only because the rest of the conftest still probes them.
        "AZURE_OPENAI_API_KEY",
        "AZURE_API_BASE",
        "GCP_KEY",
        "GCP_PROJECT_ID",
        "GCP_LOCATION",
        "LANGSMITH_API_KEY",
        "LANGSMITH_ENDPOINT",
        "AWS_DEFAULT_REGION",
        "AWS_REGION",
    }
)

# Placeholders pytest-env injects for the whole session (see pyproject). They
# must come *out* for integration tests, so the provider SDKs fall back to the
# default credential chains — ~/.aws/credentials, az login, gcloud auth — rather
# than authenticating as the string "testing".
FAKE_CREDENTIAL_VALUES = {
    "AWS_ACCESS_KEY_ID": "testing",
    "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SECURITY_TOKEN": "testing",
    "AWS_SESSION_TOKEN": "testing",
    "AZURE_OPENAI_API_KEY": "test-key",
    "LANGSMITH_API_KEY": "test-key",
}


def load_env_file(path: Path) -> dict[str, str]:
    """Parse a .env file into a dict (simple KEY=VALUE format)."""
    env_vars: dict[str, str] = {}
    if not path.exists():
        return env_vars
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("'\"")
        if key:
            env_vars[key] = value
    return env_vars


def layered_env(integration_file: Path, orchestrator_file: Path) -> dict[str, str]:
    """Both files merged **per key**, with ``.env.integration`` winning.

    The orchestrator ``.env`` contributes only ``CREDENTIAL_KEYS``;
    ``.env.integration`` contributes everything it has. Neither file suppresses
    the other, so a split configuration — URL in one, key in the other — works,
    and a stale override file cannot hide coordinates that exist.
    """
    merged = {
        key: value
        for key, value in load_env_file(orchestrator_file).items()
        if key in CREDENTIAL_KEYS
    }
    merged.update(load_env_file(integration_file))
    return merged


def gateway_env(integration_file: Path, orchestrator_file: Path) -> dict[str, str]:
    """Just the gateway coordinates, from the same layering as everything else."""
    return {k: v for k, v in layered_env(integration_file, orchestrator_file).items() if k in GATEWAY_KEYS}


def restored_environment(
    environ: Mapping[str, str],
    integration_file: Path,
    orchestrator_file: Path,
) -> dict[str, str]:
    """The environment the integration tier should run under.

    Pure: returns the desired mapping and leaves applying it to the caller, so
    the layering is testable without mutating the process. Fakes are stripped
    unconditionally — the previous early return meant a mere *presence* of
    ``.env.integration`` left ``AWS_ACCESS_KEY_ID=testing`` in place.
    """
    result = dict(environ)
    result.update(layered_env(integration_file, orchestrator_file))
    for key, fake_value in FAKE_CREDENTIAL_VALUES.items():
        if result.get(key) == fake_value:
            del result[key]
    return result
