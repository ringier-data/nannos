# Gateway management auth: master key + app-side scoping

**Status:** accepted

console-backend authenticates to the LiteLLM Proxy management API (model CRUD, `model_info` reads) with the proxy **master key**, held server-side only. The access scoping is enforced in console-backend (`require_admin` + audit, mirroring the MCP gateway router), not by a scoped LiteLLM key.

## Why

We wanted a virtual key scoped to *only* model-management endpoints, but route-level scoping and role-based access control are **LiteLLM Enterprise** features — the OSS license has no narrowly-scoped management key. The OSS options are the master key or a full `proxy_admin`-role key, both effectively god-mode. So the security boundary has to live in our layer.

## Consequences

- console-backend holds a god-mode credential. A security reviewer will (correctly) flag this — it is a known, accepted consequence of the OSS constraint, not an oversight.
- Compensating controls are mandatory: network-isolate the proxy's management routes so only console-backend can reach them; audit every model mutation; rotate the master key.
- Revisit if/when LiteLLM Enterprise is adopted — a scoped management key would then replace the master key.
