# Nannos Model Gateway = upstream LiteLLM proxy + our cost-capture callback.
#
# Deployment-AGNOSTIC: the image bakes only the product code (the cost-capture
# callback). The `config.yaml` (model_list, regions, costs) is deployment-specific
# and is mounted at runtime (a ConfigMap in k8s, a bind-mount locally) — never baked.
#
# Base is the upstream LiteLLM image, pinned to an exact version+digest. Reproducible
# builds matter here: Vertex `eu` multi-region routing is version-dependent.
#
# Bumped v1.89.2 -> v1.90.0-rc.1 -> v1.90.0 (final, released 2026-06-26) to pick up the
# Vertex multi-region context-caching fix (BerriAI/litellm#29573, merged 2026-06-09): the
# cachedContents call hardcoded `https://{location}-aiplatform.googleapis.com`, which 404s for
# the `eu`/`us` multi-regions (only the inference path used the correct
# `aiplatform.{geo}.rep.googleapis.com` REP host — hence completions worked but any cache_control
# on a Vertex Gemini model 404'd). The fix was NOT backported to the 1.89.x stable line (still
# broken in v1.89.4), so the rc carried it first; then pinned to the v1.90.0 final tag.
#
# v1.90.0 -> v1.90.7, same stable line: the backport of GHSA-3cv6-jpf6-8222 (authenticated SSRF
# and provider-credential exfiltration through unvalidated request-body routing fields). No
# database migrations between the two. Moving to a newer line is tracked separately.
FROM ghcr.io/berriai/litellm:v1.90.7@sha256:917d480b7e4a7e61822e54f70be5c7cf753fb0b7328cbf6467612652ca748075

WORKDIR /app

# Proxy-side cost/usage capture callback (product logic, not deployment config).
# LiteLLM resolves a `module.attr` callback RELATIVE TO THE CONFIG DIR, so the
# logger must live next to the (runtime-mounted) config at /etc/litellm.
COPY custom_logger.py /etc/litellm/custom_logger.py

# Trace-export filter, shared with the other services. Copied as a single file
# rather than installing the SDK: this image has no Python build, and the module
# is written with no intra-package imports for exactly this reason. custom_logger
# imports it at proxy startup.
#
# Why the proxy needs it: streaming a completion back to a caller costs one OTel
# span per forwarded chunk (116 of them for a single call, measured), which
# swamps every trace that crosses the gateway.
COPY --from=ringier-a2a-sdk ringier_a2a_sdk/telemetry/span_filter.py /etc/litellm/nannos_span_filter.py

# So `callbacks: custom_logger.proxy_handler_instance` resolves — and so
# `import nannos_span_filter` does.
ENV PYTHONPATH=/etc/litellm

EXPOSE 4000

# Config is provided at runtime by the deployment, mounted at /etc/litellm/config.yaml
# (via subPath so it doesn't hide the baked custom_logger.py). DATABASE_URL /
# LITELLM_MASTER_KEY / provider creds come from the pod environment.
CMD ["--config", "/etc/litellm/config.yaml", "--port", "4000"]
