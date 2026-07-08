"""A2A extension URNs console-backend negotiates on behalf of its clients.

This list feeds the ``X-A2A-Extensions`` header sent to the orchestrator, which is
the actual on/off switch for extension emission (the executor only streams events
whose URN the client requested) — a URN missing here is silently never streamed.

Keep in sync with the repo-root ``a2a-extensions.json`` registry (pinned by
``tests/test_a2a_extensions_conformance.py``); the orchestrator and the embed SDK
carry their own copies of the same list.
"""

SUPPORTED_A2A_EXTENSIONS = [
    "urn:nannos:a2a:activity-log:1.0",
    "urn:nannos:a2a:work-plan:1.0",
    "urn:nannos:a2a:intermediate-output:1.0",
    "urn:nannos:a2a:feedback-request:1.0",
    "urn:nannos:a2a:human-in-the-loop:1.0",
    "urn:nannos:a2a:client-action:1.0",
]

X_A2A_EXTENSIONS_HEADER = ", ".join(SUPPORTED_A2A_EXTENSIONS)
