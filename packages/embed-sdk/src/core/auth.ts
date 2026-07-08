// Dependency-free OIDC Authorization-Code + PKCE popup login for embedded hosts.
//
// This is the "self-login" path (ADR-0002 Amendment 2, Tier A): the widget
// authenticates the end user to nannos directly against the nannos IdP, as a
// PUBLIC OIDC client (no client secret). It works in ANY host regardless of the
// host's own auth, needs no host-backend broker, and yields a nannos access
// token the widget presents on the socket (consumed by console-backend's
// token-auth branch). Cross-origin means we must carry a *token*, not a cookie.
//
// Popup mechanics: window.open() must happen inside the user gesture or the
// browser blocks it. login() therefore opens a blank popup synchronously, then
// navigates it to the authorize URL once discovery resolves.
//
// The redirect_uri is a tiny static page on the HOST origin that postMessages
// { code, state } back to this opener (see the cockpit's public callback page).
// The code→token exchange runs HERE, so the PKCE code_verifier never leaves the
// opener.

export interface PkceAuthConfig {
  /** OIDC issuer (e.g. https://login.p.nannos.rcplus.io/realms/nannos). */
  issuer: string;
  /** Public client id registered in the IdP for the embed. */
  clientId: string;
  /** Registered redirect URI — the host's static callback page. */
  redirectUri: string;
  /** OAuth scope. Default: "openid profile email". */
  scope?: string;
  /** sessionStorage key prefix (for the persisted token across reloads). Default "nannos-embed-auth". */
  storageKey?: string;
}

interface StoredToken {
  accessToken: string;
  refreshToken?: string;
  /** Absolute epoch ms at which the access token expires. */
  expiresAt: number;
}

interface OidcMetadata {
  authorization_endpoint: string;
  token_endpoint: string;
}

const b64url = (bytes: ArrayBuffer | Uint8Array): string => {
  const arr = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let str = '';
  for (const b of arr) str += String.fromCharCode(b);
  return btoa(str).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
};

const randomVerifier = (): string => {
  const bytes = new Uint8Array(32);
  crypto.getRandomValues(bytes);
  return b64url(bytes);
};

const challenge = async (verifier: string): Promise<string> => {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
  return b64url(digest);
};

import type { NannosAuth } from './types';

/** PKCE strategy — the concrete `NannosAuth` for OIDC self-login. */
export type PkceAuth = NannosAuth;

/**
 * Run the PKCE redirect callback: parse `?code&state` (or `error`) from the URL,
 * postMessage it back to the opener in the shape `login()` waits for, and close
 * the popup. This is the SDK-owned callback LOGIC — a host's redirect page/route
 * calls this instead of hand-writing the postMessage. (`<NannosAuthCallback>` in
 * `/react` is the component wrapper.) The redirect URI itself must still be a real
 * registered route the host serves; only the JS is ours now.
 *
 * `targetOrigin` defaults to the callback page's own origin, which equals the
 * opener's origin in the standard setup (the redirect route is served by the host
 * app). Pass it explicitly when your callback route lives on a DIFFERENT origin
 * than the app. (`document.referrer` is deliberately NOT used: after an
 * interactive IdP login the referrer — when sent at all — is the IdP's origin,
 * so the postMessage would target the IdP and never reach the opener.)
 */
export function handleAuthCallback(opts?: { targetOrigin?: string }): void {
  const params = new URLSearchParams(window.location.search);
  const target = opts?.targetOrigin ?? window.location.origin;
  window.opener?.postMessage(
    {
      type: 'nannos-auth',
      code: params.get('code') ?? undefined,
      state: params.get('state') ?? undefined,
      error: params.get('error') ?? undefined,
    },
    target,
  );
  window.close();
}

export function createPkceAuth(config: PkceAuthConfig): PkceAuth {
  const scope = config.scope ?? 'openid profile email';
  const storageKey = config.storageKey ?? 'nannos-embed-auth';
  const redirectOrigin = new URL(config.redirectUri).origin;

  let metadata: OidcMetadata | null = null;
  let token: StoredToken | null = loadToken();

  function loadToken(): StoredToken | null {
    try {
      const raw = sessionStorage.getItem(storageKey);
      return raw ? (JSON.parse(raw) as StoredToken) : null;
    } catch {
      return null;
    }
  }
  function saveToken(t: StoredToken | null) {
    token = t;
    try {
      if (t) sessionStorage.setItem(storageKey, JSON.stringify(t));
      else sessionStorage.removeItem(storageKey);
    } catch {
      /* sessionStorage may be unavailable; in-memory token still works */
    }
  }

  async function discover(): Promise<OidcMetadata> {
    if (metadata) return metadata;
    const res = await fetch(`${config.issuer.replace(/\/$/, '')}/.well-known/openid-configuration`);
    if (!res.ok) throw new Error(`[nannos] OIDC discovery failed: ${res.status}`);
    const doc = (await res.json()) as OidcMetadata;
    metadata = { authorization_endpoint: doc.authorization_endpoint, token_endpoint: doc.token_endpoint };
    return metadata;
  }

  // Refresh margin: treat a token as needing refresh well before it expires.
  // MUST exceed the transport's re-auth lead (60s) so that when the socket
  // re-auths ~60s before expiry, getAccessToken() actually mints a FRESH token
  // rather than handing back the same soon-to-die one.
  const REFRESH_MARGIN_MS = 90_000;
  const isValid = (t: StoredToken | null): t is StoredToken => !!t && t.expiresAt > Date.now() + REFRESH_MARGIN_MS;

  async function refresh(): Promise<string | null> {
    if (!token?.refreshToken) return null;
    try {
      const meta = await discover();
      const res = await fetch(meta.token_endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
          grant_type: 'refresh_token',
          client_id: config.clientId,
          refresh_token: token.refreshToken,
        }),
      });
      if (!res.ok) {
        saveToken(null);
        return null;
      }
      return storeTokenResponse(await res.json());
    } catch {
      return null;
    }
  }

  function storeTokenResponse(data: {
    access_token: string;
    refresh_token?: string;
    expires_in?: number;
  }): string {
    saveToken({
      accessToken: data.access_token,
      refreshToken: data.refresh_token ?? token?.refreshToken,
      expiresAt: Date.now() + (data.expires_in ?? 300) * 1000,
    });
    return data.access_token;
  }

  async function getAccessToken(): Promise<string | null> {
    if (isValid(token)) return token.accessToken;
    return refresh();
  }

  async function login(): Promise<string> {
    // Open the popup synchronously to stay inside the user gesture; navigate it
    // once discovery + PKCE params are ready.
    const popup = window.open('', 'nannos-login', 'width=480,height=720');
    if (!popup) throw new Error('[nannos] login popup was blocked');

    try {
      const meta = await discover();
      const verifier = randomVerifier();
      const state = randomVerifier();
      const authUrl = new URL(meta.authorization_endpoint);
      authUrl.search = new URLSearchParams({
        response_type: 'code',
        client_id: config.clientId,
        redirect_uri: config.redirectUri,
        scope,
        state,
        code_challenge: await challenge(verifier),
        code_challenge_method: 'S256',
      }).toString();
      popup.location.href = authUrl.toString();

      const code = await waitForCode(popup, state);
      const res = await fetch(meta.token_endpoint, {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: new URLSearchParams({
          grant_type: 'authorization_code',
          client_id: config.clientId,
          code,
          redirect_uri: config.redirectUri,
          code_verifier: verifier,
        }),
      });
      if (!res.ok) throw new Error(`[nannos] token exchange failed: ${res.status}`);
      return storeTokenResponse(await res.json());
    } finally {
      if (!popup.closed) popup.close();
    }
  }

  /** Resolve with the auth code once the callback page postMessages it back. */
  function waitForCode(popup: Window, expectedState: string): Promise<string> {
    return new Promise<string>((resolve, reject) => {
      let settled = false;
      const cleanup = () => {
        settled = true;
        window.removeEventListener('message', onMessage);
        clearInterval(closedTimer);
      };
      const onMessage = (event: MessageEvent) => {
        if (event.origin !== redirectOrigin) return;
        const data = event.data as { type?: string; code?: string; state?: string; error?: string };
        if (data?.type !== 'nannos-auth') return;
        if (data.state !== expectedState) return; // CSRF guard
        cleanup();
        if (data.error || !data.code) reject(new Error(`[nannos] login failed: ${data.error ?? 'no code'}`));
        else resolve(data.code);
      };
      window.addEventListener('message', onMessage);
      // If the user closes the popup without finishing, don't hang forever.
      const closedTimer = setInterval(() => {
        if (!settled && popup.closed) {
          cleanup();
          reject(new Error('[nannos] login cancelled'));
        }
      }, 500);
    });
  }

  return {
    getAccessToken,
    login,
    // Authenticated if we hold a valid token OR a refresh token to mint one
    // silently — so we don't pop a login when a background refresh would do.
    isAuthenticated: () => !!token && (token.expiresAt > Date.now() + REFRESH_MARGIN_MS || !!token.refreshToken),
    logout: () => saveToken(null),
  };
}
