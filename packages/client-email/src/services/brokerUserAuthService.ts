import type { Config } from '../config/config.js';
import type { Storage } from '../storage/storage.js';
import type { IUserAuthService } from './userAuthService.js';
import { BrokerClient, BrokerSignInRequiredError, MintedToken } from './brokerClient.js';
import { Logger } from '../utils/logger.js';

/** A minted token is renewed this long before it expires, at most half its lifetime. */
const RENEW_MARGIN_MS = 5 * 60 * 1000;

interface CachedToken extends MintedToken {
  renewAt: number;
}

/**
 * Sign-in through console-backend's token broker (USER_AUTH_MODE=broker).
 *
 * The broker runs the Keycloak login and keeps the user's one offline token, so signing
 * in here also makes the user ready for scheduled jobs. This client keeps only who the
 * sender is (`oidcSub`), and has tokens minted per audience when it needs them.
 */
export class BrokerUserAuthService implements IUserAuthService {
  private readonly logger = Logger.getLogger(BrokerUserAuthService.name);
  /** Key: `${email}:${audience}` */
  private readonly tokenCache = new Map<string, CachedToken>();

  constructor(
    private readonly storage: Storage,
    private readonly broker: BrokerClient,
    private readonly config: Config
  ) {}

  /** The broker sends the browser back to the same callback the local login uses. */
  private callbackUrl(): string {
    return new URL('/api/v1/oauth/callback', this.config.baseUrl).toString();
  }

  private clearCache(email: string): void {
    const prefix = `${email}:`;
    for (const key of this.tokenCache.keys()) {
      if (key.startsWith(prefix)) {
        this.tokenCache.delete(key);
      }
    }
  }

  async isUserAuthorized(email: string): Promise<boolean> {
    const row = await this.storage.getToken(email);
    return !!row && row.authMode === 'broker' && !!row.oidcSub;
  }

  async getTokenForAudience(email: string, audience: string): Promise<string | null> {
    const key = `${email}:${audience}`;
    const cached = this.tokenCache.get(key);
    if (cached && cached.renewAt > Date.now()) {
      return cached.accessToken;
    }

    const row = await this.storage.getToken(email);
    if (!row || row.authMode !== 'broker' || !row.oidcSub) {
      return null;
    }
    try {
      const minted = await this.broker.mint(row.oidcSub, audience);
      const now = Date.now();
      const margin = Math.min(RENEW_MARGIN_MS, (minted.expiresAt - now) / 2);
      this.tokenCache.set(key, { ...minted, renewAt: minted.expiresAt - margin });
      return minted.accessToken;
    } catch (error) {
      if (!(error instanceof BrokerSignInRequiredError)) {
        // The broker or Keycloak is down, or this client is not set up for the audience.
        // Signing in again fixes neither, so the caller must not ask for it: it answers
        // "try again later" instead.
        this.logger.error(error, `Failed to mint a ${audience} token for ${email}: ${error}`);
        throw error;
      }
      // Nothing this client holds can fix it: forget the sign-in, so the next email
      // gets the "please sign in" reply.
      this.logger.info(`${email} must sign in again (${error.message}); removing the sign-in`);
      this.clearCache(email);
      await this.storage
        .deleteToken(email)
        .catch((e) => this.logger.warn(`Failed to remove the sign-in of ${email}: ${e}`));
      return null;
    }
  }

  async getOrchestratorToken(email: string): Promise<string | null> {
    return this.getTokenForAudience(email, this.config.oidc.orchestratorAudience);
  }

  async completeOAuthFlow(email: string, callbackUrl: string, _codeVerifier: string, _state: string): Promise<void> {
    const code = new URL(callbackUrl).searchParams.get('code');
    if (!code) {
      throw new Error('The broker callback carries no code');
    }
    const identity = await this.broker.redeem(code);
    // Replaces every column: a user who signed in locally before leaves no tokens behind.
    await this.storage.saveToken({ email, oidcSub: identity.sub, authMode: 'broker' });
    this.clearCache(email);
    this.logger.info(`${email} signed in through the broker`);
  }

  async getAuthorizationUrl(state: string, _codeVerifier: string): Promise<string> {
    return this.broker.authorizeUrl(this.callbackUrl(), state);
  }

  async storeAuthState(state: string, email: string, codeVerifier: string): Promise<void> {
    // The broker runs PKCE with Keycloak itself; the verifier is stored only because
    // the state store requires one.
    await this.storage.saveOAuthState(state, email, codeVerifier, 604800); // 7 day TTL
  }
}
