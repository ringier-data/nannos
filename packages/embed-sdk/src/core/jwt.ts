/**
 * Reading a JWT, WITHOUT verifying it. The browser holds no key and decides
 * nothing on these claims — the backend verifies the token it is presented.
 *
 * Two callers, both of them diagnostic in spirit: the transport schedules its
 * re-auth from `exp` (client.ts), and the dev inspector shows the developer the
 * exact bearer their sends carry (panel/components/dev-context-inspector.tsx).
 */

export interface DecodedJwt {
  header: Record<string, unknown>;
  payload: Record<string, unknown>;
  /** The signature segment, still base64url — shown, never checked. */
  signature: string;
}

function decodeSegment(segment: string): Record<string, unknown> | null {
  try {
    let b64 = segment.replace(/-/g, '+').replace(/_/g, '/');
    b64 += '='.repeat((4 - (b64.length % 4)) % 4); // JWT segments are unpadded base64url
    // A claim can hold non-ASCII (a display name), which `atob` alone mangles:
    // it yields bytes, so decode them as the UTF-8 the JWT spec requires.
    const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
    const json: unknown = JSON.parse(new TextDecoder().decode(bytes));
    return json && typeof json === 'object' && !Array.isArray(json)
      ? (json as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

/** Parse a compact JWS. Null for anything else — an opaque token, or junk. */
export function decodeJwt(token: string): DecodedJwt | null {
  const parts = token.split('.');
  if (parts.length !== 3) return null;
  const header = decodeSegment(parts[0]);
  const payload = decodeSegment(parts[1]);
  if (!header || !payload) return null;
  return { header, payload, signature: parts[2] };
}

/** `exp` as epoch ms, or null when the token carries none.
 *
 *  Reads the payload segment ALONE, deliberately not via `decodeJwt`: the
 *  transport schedules its re-auth on this, and a header this parser cannot
 *  read is no reason to leave a connection carrying a token until it dies. */
export function jwtExpMs(token: string): number | null {
  const exp = decodeSegment(token.split('.')[1] ?? '')?.exp;
  return typeof exp === 'number' ? exp * 1000 : null;
}
