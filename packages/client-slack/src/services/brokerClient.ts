/**
 * Client for console-backend's token broker (`/api/v1/auth/broker`).
 *
 * In broker mode this client does not run its own Keycloak login. It sends the user's
 * browser to the broker, which signs them in, keeps their offline token, and sends the
 * browser back with a one-time code. The code is redeemed for who signed in; from then on
 * access tokens are minted by the broker for the audiences this client is allowed.
 *
 * Every call is authenticated with this client's own client-credentials token (never a
 * user's), cached until shortly before it expires.
 *
 * Plain `fetch` and no framework imports on purpose: the chat clients share no package,
 * and this file is the same in each of them.
 */

/** Who signed in, as the broker's `/redeem` returns it. */
export interface BrokerIdentity {
  user_id: string;
  sub: string;
  email?: string | null;
  email_verified?: boolean | null;
  name?: string | null;
  preferred_username?: string | null;
  given_name?: string | null;
  family_name?: string | null;
  groups: string[];
  phone_number?: string | null;
  phone_number_idp?: string | null;
  company_name?: string | null;
}

export interface MintedToken {
  accessToken: string;
  /** Unix time (ms) at which the token expires. */
  expiresAt: number;
}

/**
 * The broker cannot serve this user until they sign in again (HTTP 409): they never
 * signed in through this client, or their offline token has expired or was revoked.
 */
export class BrokerSignInRequiredError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'BrokerSignInRequiredError';
  }
}

/** Any other broker failure: configuration, availability, a refused code. */
export class BrokerError extends Error {
  constructor(
    message: string,
    readonly status: number
  ) {
    super(message);
    this.name = 'BrokerError';
  }
}

export interface BrokerClientOptions {
  /** console-backend's base URL for this client's own calls, e.g. `http://console:8080`. */
  baseUrl: string;
  /**
   * Where a browser reaches console-backend: the console's public URL, whose `/api` is
   * console-backend. The sign-in link is built from it. Defaults to *baseUrl*, which
   * works only when that is public too.
   */
  publicBaseUrl?: string;
  /** This client's Keycloak client id; the broker knows it as a registered client. */
  clientId: string;
  /** The audience of this client's client-credentials token (console-backend's client). */
  serviceAudience: string;
  /** Obtains this client's own client-credentials token. */
  getServiceCredentials: (audience: string) => Promise<MintedToken>;
}

/** A client-credentials token is renewed this long before it expires. */
const SERVICE_TOKEN_MARGIN_MS = 30 * 1000;

export class BrokerClient {
  private serviceToken: MintedToken | null = null;
  private serviceTokenRequest: Promise<MintedToken> | null = null;

  constructor(private readonly options: BrokerClientOptions) {}

  /** Where to send the browser to sign in. The broker sends it back to *redirectUri*. */
  authorizeUrl(redirectUri: string, state: string): string {
    const base = this.options.publicBaseUrl || this.options.baseUrl;
    const url = new URL(`${base.replace(/\/+$/, '')}/api/v1/auth/broker/authorize`);
    url.searchParams.set('client_id', this.options.clientId);
    url.searchParams.set('redirect_uri', redirectUri);
    url.searchParams.set('state', state);
    return url.toString();
  }

  /** Trade the one-time code the browser came back with for who signed in. */
  async redeem(code: string): Promise<BrokerIdentity> {
    const response = await this.post('/api/v1/auth/broker/redeem', { code });
    if (!response.ok) {
      throw new BrokerError(`Broker refused the sign-in code: ${await describe(response)}`, response.status);
    }
    return (await response.json()) as BrokerIdentity;
  }

  /** Mint an access token for *audience* on behalf of the user *sub*. */
  async mint(sub: string, audience: string): Promise<MintedToken> {
    const response = await this.post('/api/v1/auth/broker/token', { sub, audience });
    if (response.status === 409) {
      throw new BrokerSignInRequiredError(await describe(response));
    }
    if (!response.ok) {
      throw new BrokerError(`Broker could not mint a token for ${audience}: ${await describe(response)}`, response.status);
    }
    const body = (await response.json()) as { access_token: string; expires_in: number };
    return { accessToken: body.access_token, expiresAt: Date.now() + body.expires_in * 1000 };
  }

  private endpoint(path: string): string {
    return `${this.options.baseUrl.replace(/\/+$/, '')}${path}`;
  }

  /** POST as this client. A 401 means the cached service token went stale: renew once. */
  private async post(path: string, body: unknown): Promise<Response> {
    const send = async (token: string) =>
      fetch(this.endpoint(path), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify(body),
      });
    const response = await send(await this.getServiceToken());
    if (response.status !== 401) {
      return response;
    }
    this.serviceToken = null;
    return send(await this.getServiceToken());
  }

  private async getServiceToken(): Promise<string> {
    if (this.serviceToken && this.serviceToken.expiresAt - SERVICE_TOKEN_MARGIN_MS > Date.now()) {
      return this.serviceToken.accessToken;
    }
    // One request at a time: concurrent callers share it instead of each asking Keycloak.
    if (!this.serviceTokenRequest) {
      this.serviceTokenRequest = this.options
        .getServiceCredentials(this.options.serviceAudience)
        .then((token) => {
          this.serviceToken = token;
          return token;
        })
        .finally(() => {
          this.serviceTokenRequest = null;
        });
    }
    return (await this.serviceTokenRequest).accessToken;
  }
}

async function describe(response: Response): Promise<string> {
  let detail = '';
  try {
    const body = (await response.json()) as { detail?: unknown };
    detail = typeof body?.detail === 'string' ? body.detail : '';
  } catch {
    // Not JSON; the status says enough.
  }
  return detail ? `HTTP ${response.status}: ${detail}` : `HTTP ${response.status}`;
}
