/**
 * Reading a JWT the SDK never verifies. Two things are easy to get wrong and
 * both bite in production: the segments are UNPADDED base64url (so `atob`
 * alone throws on most real tokens), and a claim may carry non-ASCII (a
 * display name), which byte-per-char decoding mangles.
 */
import { describe, expect, it } from 'vitest';
import { decodeJwt, jwtExpMs } from './jwt';

/** Encode as a JWT segment does: UTF-8 → base64url, unpadded. */
function segment(value: unknown): string {
  const bytes = new TextEncoder().encode(JSON.stringify(value));
  let binary = '';
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

const token = (header: unknown, payload: unknown) => `${segment(header)}.${segment(payload)}.s1gn4tur3`;

describe('decodeJwt', () => {
  it('reads header and payload, and keeps the signature untouched', () => {
    const decoded = decodeJwt(
      token({ alg: 'RS256', typ: 'JWT', kid: 'k1' }, { sub: 'u-1', azp: 'nannos-embed', exp: 1_757_000_000 }),
    );
    expect(decoded?.header).toEqual({ alg: 'RS256', typ: 'JWT', kid: 'k1' });
    expect(decoded?.payload).toMatchObject({ sub: 'u-1', azp: 'nannos-embed' });
    expect(decoded?.signature).toBe('s1gn4tur3');
  });

  it('survives a claim with non-ASCII in it', () => {
    // Byte-per-char decoding turns this into mojibake — the panel would show a
    // wrong user name and the developer would chase the wrong bug.
    const decoded = decodeJwt(token({ alg: 'RS256' }, { name: 'Jörg Müller — ø' }));
    expect(decoded?.payload.name).toBe('Jörg Müller — ø');
  });

  it('reads a payload whose base64url needs padding put back', () => {
    // Length chosen so the encoded payload is not a multiple of 4 chars.
    const decoded = decodeJwt(token({ alg: 'RS256' }, { a: 'bcd' }));
    expect(decoded?.payload).toEqual({ a: 'bcd' });
  });

  it('is null for an opaque token rather than throwing', () => {
    expect(decodeJwt('not-a-jwt')).toBeNull();
    expect(decodeJwt('two.segments')).toBeNull();
    expect(decodeJwt('a.b.c')).toBeNull(); // three segments, no JSON in them
    expect(decodeJwt('')).toBeNull();
  });

  it('is null when a segment holds JSON that is not an object', () => {
    expect(decodeJwt(`${segment({ alg: 'none' })}.${segment([1, 2])}.sig`)).toBeNull();
  });
});

describe('jwtExpMs', () => {
  it('returns `exp` in milliseconds — the transport schedules re-auth on it', () => {
    expect(jwtExpMs(token({ alg: 'RS256' }, { exp: 1_757_000_000 }))).toBe(1_757_000_000_000);
  });

  it('still reads `exp` when the header segment is unreadable', () => {
    // Re-auth scheduling hangs off this: an odd header must not silently leave
    // the socket carrying a token until it dies mid-session.
    expect(jwtExpMs(`h.${segment({ exp: 1_757_000_000 })}.s`)).toBe(1_757_000_000_000);
  });

  it('is null for a token without `exp`, and for one that cannot be read', () => {
    expect(jwtExpMs(token({ alg: 'RS256' }, { sub: 'u-1' }))).toBeNull();
    expect(jwtExpMs(token({ alg: 'RS256' }, { exp: 'soon' }))).toBeNull();
    expect(jwtExpMs('opaque')).toBeNull();
  });
});
