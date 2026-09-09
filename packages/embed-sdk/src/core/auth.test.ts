// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createPkceAuth } from './auth';
import type { PkceAuthConfig } from './auth';

const ISSUER = 'https://idp.example/realms/nannos';
const REDIRECT_URI = 'https://host.example/nannos-auth-callback.html';
const AUTHORIZE = `${ISSUER}/protocol/openid-connect/auth`;
const TOKEN = `${ISSUER}/protocol/openid-connect/token`;
const BASE: PkceAuthConfig = { issuer: ISSUER, clientId: 'nannos-embedded', redirectUri: REDIRECT_URI };

function stubNetwork() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith('/.well-known/openid-configuration')) {
        return new Response(JSON.stringify({ authorization_endpoint: AUTHORIZE, token_endpoint: TOKEN }));
      }
      if (url === TOKEN) return new Response(JSON.stringify({ access_token: 'at', expires_in: 300 }));
      throw new Error(`unexpected fetch ${url}`);
    }),
  );
}

/** Drive login(): capture the authorize URL the popup is sent to, then answer it with a code. */
async function runLogin(config: PkceAuthConfig): Promise<URL> {
  const popup = { closed: false, close: vi.fn(), location: { href: '' } };
  vi.spyOn(window, 'open').mockReturnValue(popup as unknown as Window);
  stubNetwork();

  const pending = createPkceAuth(config).login();
  await vi.waitFor(() => expect(popup.location.href).not.toBe(''));
  const authUrl = new URL(popup.location.href);

  window.dispatchEvent(
    new MessageEvent('message', {
      origin: new URL(REDIRECT_URI).origin,
      data: { type: 'nannos-auth', code: 'the-code', state: authUrl.searchParams.get('state') },
    }),
  );
  await expect(pending).resolves.toBe('at');
  expect(popup.close).toHaveBeenCalled();
  return authUrl;
}

describe('createPkceAuth login() — authorize request', () => {
  afterEach(() => {
    vi.restoreAllMocks();
    vi.unstubAllGlobals();
    sessionStorage.clear();
  });

  it('sends kc_idp_hint when idpHint is set (brokered SSO, ADR-0002 Amendment 4)', async () => {
    const url = await runLogin({ ...BASE, idpHint: 'alloy' });
    expect(url.origin + url.pathname).toBe(AUTHORIZE);
    expect(url.searchParams.get('kc_idp_hint')).toBe('alloy');
    expect(url.searchParams.get('client_id')).toBe('nannos-embedded');
    expect(url.searchParams.get('redirect_uri')).toBe(REDIRECT_URI);
    expect(url.searchParams.get('code_challenge_method')).toBe('S256');
  });

  it('omits kc_idp_hint by default', async () => {
    const url = await runLogin(BASE);
    expect(url.searchParams.has('kc_idp_hint')).toBe(false);
  });

  it('appends extraAuthParams but never lets them override the PKCE parameters', async () => {
    const url = await runLogin({
      ...BASE,
      extraAuthParams: { login_hint: 'erik@example.com', client_id: 'spoofed', response_type: 'token' },
    });
    expect(url.searchParams.get('login_hint')).toBe('erik@example.com');
    expect(url.searchParams.get('client_id')).toBe('nannos-embedded');
    expect(url.searchParams.get('response_type')).toBe('code');
  });
});
