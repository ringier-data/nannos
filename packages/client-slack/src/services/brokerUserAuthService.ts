import { randomUUID } from 'crypto';
import type { IOAuthStateStore, IUserAuthStorage, UserAuthToken } from '../storage/types.js';
import type { Config } from '../config/config.js';
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
 * Slack user is (`oidcSub`), and has tokens minted per audience when it needs them.
 */
export class BrokerUserAuthService implements IUserAuthService {
  private readonly logger = Logger.getLogger(BrokerUserAuthService.name);
  /** Key: `${userId}:${teamId}:${audience}` */
  private readonly tokenCache = new Map<string, CachedToken>();

  constructor(
    private readonly storage: IUserAuthStorage,
    private readonly broker: BrokerClient,
    private readonly config: Config,
    private readonly oauthStateStore: IOAuthStateStore
  ) {}

  /** The broker sends the browser back to the same callback the local login uses. */
  private callbackUrl(): string {
    return new URL('/api/v1/oauth/callback', this.config.baseUrl).toString();
  }

  private clearCache(userId: string, teamId: string): void {
    const prefix = `${userId}:${teamId}:`;
    for (const key of this.tokenCache.keys()) {
      if (key.startsWith(prefix)) {
        this.tokenCache.delete(key);
      }
    }
  }

  async isUserAuthorized(userId: string, teamId: string): Promise<boolean> {
    const row = await this.storage.getToken(userId, teamId);
    return !!row && row.authMode === 'broker' && !!row.oidcSub;
  }

  async getTokenForAudience(userId: string, teamId: string, audience: string): Promise<string | null> {
    const key = `${userId}:${teamId}:${audience}`;
    const cached = this.tokenCache.get(key);
    if (cached && cached.renewAt > Date.now()) {
      return cached.accessToken;
    }

    const row = await this.storage.getToken(userId, teamId);
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
        this.logger.error(error, `Failed to mint a ${audience} token for user ${userId}: ${error}`);
        throw error;
      }
      // Nothing this client holds can fix it: forget the sign-in, so the caller's
      // "please authorize" path asks the user to sign in again.
      this.logger.info(`User ${userId} must sign in again (${error.message}); removing the sign-in`);
      this.clearCache(userId, teamId);
      await this.storage
        .deleteToken(userId, teamId)
        .catch((e) => this.logger.warn(`Failed to remove the sign-in of user ${userId}: ${e}`));
      return null;
    }
  }

  async getOrchestratorToken(userId: string, teamId: string): Promise<string | null> {
    return this.getTokenForAudience(userId, teamId, this.config.oidc.orchestratorAudience);
  }

  async completeOAuthFlow(
    userId: string,
    teamId: string,
    callbackUrl: string,
    _codeVerifier: string,
    _state: string
  ): Promise<UserAuthToken> {
    const code = new URL(callbackUrl).searchParams.get('code');
    if (!code) {
      throw new Error('The broker callback carries no code');
    }
    const identity = await this.broker.redeem(code);
    const now = Date.now();
    const row: UserAuthToken = {
      userId,
      teamId,
      oidcSub: identity.sub,
      authMode: 'broker',
      createdAt: now,
      updatedAt: now,
    };
    // Replaces every column: a user who signed in locally before leaves no tokens behind.
    await this.storage.saveToken(row);
    this.clearCache(userId, teamId);
    this.logger.info(`User ${userId} in team ${teamId} signed in through the broker`);
    return row;
  }

  async revokeUserAuthorization(userId: string, teamId: string): Promise<void> {
    this.clearCache(userId, teamId);
    await this.storage.deleteToken(userId, teamId);
  }

  async getAuthorizationUrl(state: string, _teamId: string, _codeVerifier: string): Promise<string> {
    return this.broker.authorizeUrl(this.callbackUrl(), state);
  }

  async storeAuthState(state: string, userId: string, teamId: string): Promise<void> {
    // The broker runs PKCE with Keycloak itself; the state store requires a verifier,
    // so it gets a random value that is never used.
    await this.oauthStateStore.set(state, userId, teamId, randomUUID(), 604800); // 7 day TTL
  }
}
