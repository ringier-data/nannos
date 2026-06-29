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
# broken in v1.89.4), so the rc carried it first; now pinned to the v1.90.0 final tag.
FROM ghcr.io/berriai/litellm:v1.90.0@sha256:a50b02a6056095da29308310bb608f0509e08ddcd1d105bae9c21007d82b0e95

WORKDIR /app

# Proxy-side cost/usage capture callback (product logic, not deployment config).
# LiteLLM resolves a `module.attr` callback RELATIVE TO THE CONFIG DIR, so the
# logger must live next to the (runtime-mounted) config at /etc/litellm.
COPY custom_logger.py /etc/litellm/custom_logger.py

# So `callbacks: custom_logger.proxy_handler_instance` resolves.
ENV PYTHONPATH=/etc/litellm

EXPOSE 4000

# Config is provided at runtime by the deployment, mounted at /etc/litellm/config.yaml
# (via subPath so it doesn't hide the baked custom_logger.py). DATABASE_URL /
# LITELLM_MASTER_KEY / provider creds come from the pod environment.
CMD ["--config", "/etc/litellm/config.yaml", "--port", "4000"]
