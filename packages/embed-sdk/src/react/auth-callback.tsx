/**
 * PKCE self-login surface: `pkce()` builds the strategy for
 * `<NannosProvider auth={…}>`; `<NannosAuthCallback>` is mounted at the
 * registered redirect route and runs the SDK-owned callback logic.
 */
import { useEffect, type ReactNode } from 'react';
import { createPkceAuth, handleAuthCallback, type NannosAuth, type PkceAuthConfig } from '../core';

/** PKCE self-login strategy for `<NannosProvider auth={pkce({...})}>`. Thin alias
 *  of `createPkceAuth` so hosts import one thing from the react entry. */
export function pkce(config: PkceAuthConfig): NannosAuth {
  return createPkceAuth(config);
}

/**
 * Mount at your PKCE redirect route (the `redirectUri` you registered). It runs
 * the SDK-owned callback logic — postMessage the code back to the opener and
 * close the popup — so you don't hand-write callback JS. Renders nothing (or your
 * `children`, e.g. a "Signing you in…" splash the popup shows briefly).
 *
 *   <Route path="/nannos-auth-callback" element={<NannosAuthCallback />} />
 */
export function NannosAuthCallback({
  targetOrigin,
  children,
}: {
  targetOrigin?: string;
  children?: ReactNode;
}): ReactNode {
  useEffect(() => {
    handleAuthCallback({ targetOrigin });
  }, [targetOrigin]);
  return children ?? null;
}
