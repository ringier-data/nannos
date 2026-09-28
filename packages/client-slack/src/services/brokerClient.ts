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

/** What `/redeem` returns: who signed in, and the secret that names this sign-in. */
export interface BrokerRedemption extends BrokerIdentity {
  /**
   * Returned once. Keep it with the user and send it on every `mint` for them: the broker
   * stores only its hash, and a client that requires it mints for no one without it.
   */
  binding_secret: string;
}

/** What a sign-in belongs to, told to the broker when it is redeemed. */
export interface BrokerBinding {
  /**
   * This client's own key for the row that keeps the sign-in (e.g. a Slack user in a team,
   * an email address). A later sign-in into the same row replaces the binding; two rows of
   * one user never evict each other.
   */
  accountKey: string;
  /** The account the sign-in belongs to (a Slack team, a Google Chat project); empty when there is one. */
  workspaceId?: string;
  /**
   * The installations this client runs in that workspace, as it registers its delivery channels
   * (`installation_id`): where every sign-in for the workspace can be reached. Omitted leaves
   * what the broker has for the workspace.
   */
  installationIds?: string[];
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

  /** Trade the one-time code the browser came back with for who signed in, binding the sign-in. */
  async redeem(code: string, binding: BrokerBinding): Promise<BrokerRedemption> {
    const response = await this.post('/api/v1/auth/broker/redeem', {
      code,
      account_key: binding.accountKey,
      workspace_id: binding.workspaceId ?? '',
      ...(binding.installationIds ? { installation_ids: binding.installationIds } : {}),
    });
    if (!response.ok) {
      throw new BrokerError(`Broker refused the sign-in code: ${await describe(response)}`, response.status);
    }
    return (await response.json()) as BrokerRedemption;
  }

  /**
   * Mint an access token for *audience* on behalf of the user *sub*. *bindingSecret* is the
   * one `redeem` returned for their sign-in; null only for a sign-in made before it existed.
   */
  async mint(sub: string, audience: string, bindingSecret: string | null): Promise<MintedToken> {
    const response = await this.post('/api/v1/auth/broker/token', {
      sub,
      audience,
      ...(bindingSecret ? { binding_secret: bindingSecret } : {}),
    });
    if (response.status === 409) {
      throw new BrokerSignInRequiredError(await describe(response));
    }
    if (!response.ok) {
      throw new BrokerError(`Broker could not mint a token for ${audience}: ${await describe(response)}`, response.status);
    }
    const body = (await response.json()) as { access_token: string; expires_in: number };
    return { accessToken: body.access_token, expiresAt: Date.now() + body.expires_in * 1000 };
  }

  /**
   * Tell the broker the installations this client runs in *workspaceId* now, so every sign-in
   * there is reachable on each, including one installed after the user signed in.
   */
  async setWorkspaceInstallations(workspaceId: string, installationIds: string[]): Promise<void> {
    const response = await this.send('PUT', `/api/v1/auth/broker/workspaces/${encodeURIComponent(workspaceId)}`, {
      installation_ids: installationIds,
    });
    if (!response.ok) {
      throw new BrokerError(`Broker refused the installations of ${workspaceId}: ${await describe(response)}`, response.status);
    }
  }

  private endpoint(path: string): string {
    return `${this.options.baseUrl.replace(/\/+$/, '')}${path}`;
  }

  private post(path: string, body: unknown): Promise<Response> {
    return this.send('POST', path, body);
  }

  /** Call as this client. A 401 means the cached service token went stale: renew once. */
  private async send(method: 'POST' | 'PUT', path: string, body: unknown): Promise<Response> {
    const attempt = async (token: string) =>
      fetch(this.endpoint(path), {
        method,
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify(body),
      });
    const response = await attempt(await this.getServiceToken());
    if (response.status !== 401) {
      return response;
    }
    this.serviceToken = null;
    return attempt(await this.getServiceToken());
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
