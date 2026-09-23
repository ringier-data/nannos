---
status: proposed (2026-09-23); implemented in console-backend, the three chat clients, the
  cockpit BFF, the Keycloak provisioning and gitops, pending review. Every chat client
  defaults to its old sign-in until its flag is flipped; the cockpit BFF uses only the broker.
---

# console-backend brokers sign-in and holds the only offline token

## Context

A scheduled run needs its subscriber's offline token in console-backend's vault
(`user_offline_tokens`, ADR-0010). Until now the vault had one writer: the console's own
login callback. The chat clients and the cockpit BFF each ran their own Keycloak login and
kept their own refresh token (`slack-client`, `email-client`, `google-chat-client`, and the
public `nannos-embedded` of ADR-0002 Amendment 5). A user who only ever used Slack was
signed in, but not scheduler-ready, and a group default (ADR-0010) could activate a job
under an identity that had no token to run it with.

Handing those tokens over does not work. Keycloak binds a refresh token to the client it
was issued to, and console-backend refreshes as `agent-console`, so a `slack-client` token
in the vault is dead weight. Four custodians also meant four copies of what is, per user,
one standing consent.

## Decision

1. **console-backend is a token broker.** A registered client sends the user's browser to
   `GET /api/v1/auth/broker/authorize` on the console's public URL (the browser opens it;
   the client's own calls may use an in-cluster URL). console-backend runs the ordinary
   console sign-in as `agent-console` (same claims, same user upsert, same vaulting), then
   sends the browser back to the client's registered callback with a one-time code and the
   client's own `state`. The client redeems the code (`POST /redeem`) for who signed in, and
   from then on asks `POST /token` for an access token for a given audience; the broker
   refreshes the vaulted token and exchanges it (RFC 8693). Signing in anywhere now makes a
   user scheduler-ready, and there is one offline token per user.

2. **A client reaches only its own users, calling as itself.** `/redeem` and `/token`
   accept only the client's own client-credentials token (a service account, addressed to
   `agent-console`); a user token issued to the same client is refused. What tells them
   apart is structural, not a name: Keycloak opens no user session for client credentials,
   so that token has no `sid`, and every user token has one. Redeeming records
   the user as signed in through that client (`broker_client_users`), and `/token` mints
   only for those users and only for the audiences the client is registered for. A leaked
   client secret therefore reaches the people who signed in through that client, not
   everyone with a vaulted token. Every "cannot serve this user" answer is a 409, whose one
   remedy is to sign the user in again. Any other failure (a 403 audience, a 502 Keycloak)
   is not a reason to sign in: the client answers "try again later" and keeps the sign-in.

3. **Clients are admin data.** `broker_clients` (client id, exact redirect URIs with an
   optional `*` in the first host label for preview hosts, audiences, enabled) is managed
   through `/api/v1/admin/broker-clients` and audited like delivery channels.

4. **Old sign-ins drain.** Each chat client selects its mode with `USER_AUTH_MODE`. In
   broker mode, new sign-ins go through the broker, and a row signed in the old way keeps
   being served from its refresh token until it lapses or the user signs in again. Nobody is
   forced to sign in again; a drained user becomes scheduler-ready at their next sign-in.
   The cockpit BFF has no old mode: its users sign in once more, through the broker.

5. **An embedded host is bound by `azp` or, for a broker-minted token, `aud`.** The broker
   mints the cockpit's tokens with audience `cockpit-embed` and its own `azp`
   (`agent-console`), so ADR-0006's binding also matches on `aud` when `azp` is
   console-backend's own client. What tells a minted token from a console session token is
   console-backend's own audience: every login or refresh token of `agent-console` carries
   `agent-console` in `aud` (its audience mapper), and Keycloak's exchange downscopes `aud`
   to the requested audience and adds nothing. A token of `agent-console` that names
   `agent-console` in `aud` therefore binds to nothing, whatever else it carries. Old host
   tokens (`azp=nannos-embedded`) still match; the binding table is unchanged.

6. **A subscription that could not run is not created on.** What a user starts themselves —
   create, subscribe, copy, resume, switch on — is refused with a 400 carrying the console
   sign-in link when they have no vaulted token; the task-scheduler agent relays it. A group
   default creates such a member's subscription switched off, with a fixed reason and a
   console notification carrying the link; their first sign-in switches it on. A run that
   finds no vaulted token holds its subscription the same way, instead of counting failures
   until it pauses. The chat activation notice is skipped for them: it is sent under the
   subscriber's own token.

## Considered options

- **Clients forward their refresh token to the vault.** Rejected: the token is bound to
  the client that received it, and two services refreshing copies of one session breaks the
  day refresh-token rotation is turned on.
- **A second, silent login (`prompt=none`) from each client to the console.** Keeps every
  client's own token and adds a hop per client; rejected in favour of one custodian.
- **Asserting the chat platform's identity (e.g. Google Chat's email) to skip the click.**
  Rejected: it makes a chat client an identity authority for every user.
- **Email nudges for users who are not scheduler-ready.** Deferred: console-backend sends no
  email today, and the in-band answers above cover the paths users start themselves.

## Consequences

- The cockpit BFF no longer holds a refresh token (supersedes that part of ADR-0002
  Amendment 5); it keeps the user's Nannos subject and has tokens minted.
- Deleting a broker client removes its user links, so all its users must sign in again,
  also if it is registered again. Switching it off keeps them.
- Keycloak: the broker callback is a redirect URI of `agent-console`; `email-client` gains
  the `agent-console` audience; `cockpit-embed` is a new service-only client whose audience
  `agent-console` maps, provisioned once its secret exists.
- Every minted token costs a KMS decrypt and two Keycloak calls; clients cache minted tokens
  until shortly before expiry.
- A group-default member who has never signed in is reached only by the console
  notification until they do. A chat notice would need a delivery path that does not run
  under the subscriber's own token.
- Cleanup once a client has drained: drop its local-login code path and its Keycloak
  redirect URIs; its service account stays (broker calls, delivery-channel registration).
