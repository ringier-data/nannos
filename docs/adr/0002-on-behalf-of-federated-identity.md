---
status: accepted (mechanism refined 2026-07-06 — see Amendments 1 & 2)
---

# Act-on-behalf-of uses the end user's federated identity, not a service identity

> **Amendment (2026-07-06).** The **principle** below stands: end-user identity,
> host authz authoritative, service-identity/confused-deputy rejected. The
> **mechanism** does not: this ADR assumed a shared/federated OIDC issuer with
> direct RFC 8693, and called federation a "hard prerequisite." In reality
> nannos and the cockpit (alloy) run **different IdPs**. Decision: route the
> cockpit API through **Gatana as a runnable MCP server**, so it rides Nannos's
> existing per-user Gatana token-exchange path — Gatana performs the on-behalf-of
> exchange to the upstream API. This satisfies the principle without hand-built
> federation. Federation / an offline-token vault only return for a host that is
> neither Gatana-fronted nor able to mint per-user tokens. See CONTEXT.md
> "On-behalf-of identity".

> **Amendment 2 (2026-07-06) — the browser-identity leg.** The Gatana decision
> above resolves the *tool* leg (orchestrator → host API). It does **not** cover
> the *browser* leg: the embedded widget must authenticate the end user to the
> **nannos** console-backend socket, but a cockpit user holds an **Alloy** token,
> not a nannos one. Decision: a **cross-IdP token-exchange service** in the nannos
> backend. It (1) trusts a small set of **explicitly configured** external IdPs;
> (2) validates the incoming foreign token **offline against that IdP's JWKS**
> (`jwks_uri` from its `.well-known/openid-configuration`) — pinning `iss`,
> `aud`, `exp`/`nbf` — *not* via the foreign `/token` or `/introspect` endpoint,
> to avoid client-credential and runtime coupling to the foreign IdP; (3) resolves
> the nannos identity **by email**; (4) returns a nannos token the widget uses for
> the socket. Prefer the **broker model** (configure the Alloy realm as a trusted
> RFC 8693 subject issuer on the *nannos* Keycloak and let it exchange +
> auto-provision — same standard Gatana uses on the tool leg) over a hand-built
> **refresh-token vault** (nannos backend stores per-user nannos refresh tokens
> from a prior enrollment); fall back to the vault only if cross-realm exchange
> can't be enabled on the nannos realm.
>
> **Email-mapping guardrails (required, not optional).** Mapping by email is a
> trust decision on the account-takeover boundary. It is safe only when: the
> incoming token asserts `email_verified: true`; the match is against a
> **provisioned link / allowlist** (an Alloy identity is bound to a nannos user
> ahead of time), never "any email from a trusted issuer is that nannos user";
> and the foreign issuer is one where email ownership is actually enforced. Absent
> a verified email + provisioned link, refuse the exchange.
>
> This mechanism is orthogonal to the grounding tier: the in-form `apply` /
> client-action loop needs only *a* valid nannos token, so it can be demoed with a
> locally-sourced token behind the widget's `getToken` seam and the exchange
> service swapped in later with no client change.
>
> **Amendment 3 (2026-07-06) — self-login is the DEFAULT; federation is Tier B.**
> The browser leg has two ways to get a nannos token into the widget:
> - **Tier A — self-login (default, generic).** The widget authenticates the user
>   to nannos directly via OIDC **Authorization-Code + PKCE** as a *public client*
>   (SDK `createPkceAuth`, popup). Works in ANY host regardless of its auth, needs
>   **no host-backend broker and no `FEDERATED_IDPS`/email-mapping/enrollment**;
>   nannos stays the sole authority for nannos identity. Cost: a one-time nannos
>   login per device. This is why the cross-origin token path (not a cookie) is
>   mandatory — the socket carries a bearer token (consumed by Part 1, the
>   console-backend socket token-auth branch).
> - **Tier B — host federation (the exchange service above).** Seamless (no second
>   login) but per-host and heavier. Reserve it for tightly-integrated hosts that
>   refuse a second login.
>
> **Operational consequence (verified live 2026-07-06):** the self-login public
> client (e.g. `nannos-embedded`) must be granted **token-exchange permission** to
> the downstream audiences its user acts on — `orchestrator` (chat), then `gatana`
> / `console` (tool OBO). Otherwise the socket connects but `initialize_client`
> fails at `OrchestratorAuth`'s RFC 8693 exchange (`init_failed`). This is realm
> config, not code — the browser→socket→init path is otherwise proven end-to-end.
>
> Chosen: **ship Tier A as the default**; keep Tier B parked (seams built). The
> long game — brokering the host IdP *into the nannos Keycloak* — makes Tier A's
> one-time login silent SSO AND relocates the email-trust into Keycloak, which
> **retires the hand-built Tier-B exchange service entirely**. So Tier A now +
> IdP-brokering later dominates building and operating the broker.

When PTC code calls a host application's MCP server, it runs under the **end
user's own OIDC identity**, carried end-to-end (host → embed widget →
orchestrator → host MCP) via OIDC **token exchange (RFC 8693)**. The host app
and Nannos must trust the same (or a federated) OIDC issuer. The host's own
authorization (e.g. the cockpit's CASL rules) remains authoritative. We
explicitly reject a model where Nannos calls the host with its own service
credential and merely *asserts* which user is acting.

## Why

"Nannos acts on your behalf" is only literally true if calls carry the user's
identity. With end-to-end user identity, Nannos can never hold authority the
user lacks, and the host's existing permission system stays the single source of
truth — decisive for a correctness-critical, multi-tenant app. A service
identity + asserted-user model creates a **confused-deputy** risk: a
prompt-injected, code-writing agent could act as any user, with no independent
host-side check. This also matches how every existing Nannos client already
authenticates (per-user OIDC, per-service clients, zero-trust JWKS validation).

## Consequences

- **Federated identity is a hard prerequisite** for the act-on-behalf tier. This
  raises the integration bar beyond "run an MCP server + add the SDK."
- A host that cannot/won't federate identity is limited to the **grounding /
  read-only tier** (Embed SDK + in-form `apply`, human submits), with no
  headless API actions.
- Token-exchange/audience-retargeting must be implemented along the
  host → widget → orchestrator → host-MCP path.
