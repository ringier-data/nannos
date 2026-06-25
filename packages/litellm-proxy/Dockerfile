# Nannos Model Gateway = upstream LiteLLM proxy + our cost-capture callback.
#
# Deployment-AGNOSTIC: the image bakes only the product code (the cost-capture
# callback). The `config.yaml` (model_list, regions, costs) is deployment-specific
# and is mounted at runtime (a ConfigMap in k8s, a bind-mount locally) — never baked.
#
# Base is the upstream LiteLLM image, pinned to an exact version+digest. Reproducible
# builds matter here: Vertex `eu` multi-region routing is version-dependent.
#
# Bumped v1.89.2 -> v1.90.0-rc.1 to pick up the Vertex multi-region context-caching fix
# (BerriAI/litellm#29573, merged 2026-06-09): the cachedContents call hardcoded
# `https://{location}-aiplatform.googleapis.com`, which 404s for the `eu`/`us` multi-regions
# (only the inference path used the correct `aiplatform.{geo}.rep.googleapis.com` REP host —
# hence completions worked but any cache_control on a Vertex Gemini model 404'd). The fix was
# NOT backported to the 1.89.x stable line (still broken in v1.89.4), so the earliest published
# tag carrying it is the v1.90.0 release candidate. Revisit to v1.90.0 final once it ships.
FROM ghcr.io/berriai/litellm:v1.90.0-rc.1@sha256:7315a3a1573d9ff0ed78068b73260e960486ff62922ee6a78f1a2be0f1e5e249

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
