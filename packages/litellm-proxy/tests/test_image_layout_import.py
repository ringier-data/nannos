"""custom_logger imports the SDK modules copied next to it, whatever the pod's PYTHONPATH.

The image copies ``nannos_model_capabilities.py`` and ``nannos_span_filter.py`` into
/etc/litellm beside custom_logger.py and sets ``PYTHONPATH=/etc/litellm``. LiteLLM loads the
callback by file path, which does not put that directory on ``sys.path`` — and in prod the
OpenTelemetry Python auto-instrumentation webhook replaced the pod's PYTHONPATH with its own
directories, so the unguarded import failed and the proxy would not start.

Run in a fresh interpreter: the in-process suite registers the module in ``sys.modules``,
which would hide exactly this failure.
"""

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

_PKG = Path(__file__).resolve().parent.parent
_SDK = _PKG.parent / "ringier-a2a-sdk" / "ringier_a2a_sdk"


def test_custom_logger_loads_by_path_with_a_foreign_pythonpath(tmp_path):
    # The image layout: custom_logger and its SDK siblings in one directory.
    etc = tmp_path / "etc-litellm"
    etc.mkdir()
    shutil.copy(_PKG / "custom_logger.py", etc / "custom_logger.py")
    shutil.copy(_SDK / "model_capabilities.py", etc / "nannos_model_capabilities.py")
    shutil.copy(_SDK / "telemetry" / "span_filter.py", etc / "nannos_span_filter.py")

    # A stand-in for litellm's CustomLogger base class, on the only PYTHONPATH entry.
    stub = tmp_path / "stub" / "litellm" / "integrations"
    stub.mkdir(parents=True)
    (stub.parent / "__init__.py").write_text("")
    (stub / "__init__.py").write_text("")
    (stub / "custom_logger.py").write_text("class CustomLogger:\n    pass\n")

    # How LiteLLM's get_instance_fn loads a `module.attr` callback: by file path.
    code = textwrap.dedent(
        f"""
        import importlib.util
        spec = importlib.util.spec_from_file_location("custom_logger", {str(etc / "custom_logger.py")!r})
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        print("loaded", module.proxy_handler_instance.__class__.__name__)
        """
    )
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(tmp_path / "stub")  # what a webhook leaves: not the callback's directory
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert "loaded" in result.stdout
