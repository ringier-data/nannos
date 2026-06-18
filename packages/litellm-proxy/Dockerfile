# Nannos Model Gateway = upstream LiteLLM proxy + our cost-capture callback.
#
# Deployment-AGNOSTIC: the image bakes only the product code (the cost-capture
# callback). The `config.yaml` (model_list, regions, costs) is deployment-specific
# and is mounted at runtime (a ConfigMap in k8s, a bind-mount locally) — never baked.
#
# Base is the upstream LiteLLM image validated in the spike (SPIKE-FINDINGS.md).
# PIN to the exact digest you ship; main-stable resolved to v1.89.2 during the spike.
FROM ghcr.io/berriai/litellm:main-stable

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
