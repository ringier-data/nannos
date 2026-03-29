import { OIDCClient } from './oidcClient.js';
import { Logger } from '../utils/logger.js';
import { Config } from '../config/config.js';
import { Storage } from '../storage/storage.js';

/**
 * Service to manage user authentication and OIDC tokens.
 * Uses email as the user identifier (no Slack userId/teamId).
 */
interface CachedAudienceToken {
  accessToken: string;
  expiresAt: number;
}

export class UserAuthService {
  private readonly storage: Storage;
  private readonly oidcClient: OIDCClient;
  private readonly config: Config;
  private readonly logger = Logger.getLogger(UserAuthService.name);

  /** In-memory cache for audience-specific tokens. Key: `${email}:${audience}` */
  private readonly audienceTokenCache = new Map<string, CachedAudienceToken>();

  constructor(storage: Storage, oidcClient: OIDCClient, config: Config) {
    this.storage = storage;
    this.oidcClient = oidcClient;
    this.config = config;
  }

  private clearAudienceTokenCache(email: string): void {
    const prefix = `${email}:`;
    for (const key of this.audienceTokenCache.keys()) {
      if (key.startsWith(prefix)) {
        this.audienceTokenCache.delete(key);
      }
    }
  }

  /**
   * Check if user is authorized (has valid token, refreshing if needed)
   */
  async isUserAuthorized(email: string): Promise<boolean> {
    const accessToken = await this.getUserAccessToken(email);
    return accessToken !== null;
  }

  /**
   * Get user's base access token, refreshing if necessary
   */
  async getUserAccessToken(email: string): Promise<string | null> {
    const token = await this.storage.getToken(email);
    if (!token) {
      this.logger.debug(`No token found for ${email}`);
      return null;
    }

    const now = Date.now();
    const bufferMs = 5 * 60 * 1000;

    if (token.expiresAt > now + bufferMs) {
      this.logger.debug(`Token for ${email} still valid (expires in ${Math.round((token.expiresAt - now) / 1000)}s)`);
      return token.accessToken;
    }

    // Token expired or expiring — try refresh
    this.logger.info(`Token for ${email} expired or expiring soon, attempting refresh`);
    if (!token.refreshToken) {
      this.logger.info(`No refresh token available for ${email}`);
      return null;
    }

    try {
      const refreshed = await this.oidcClient.refreshAccessToken(token.refreshToken);
      this.clearAudienceTokenCache(email);
      await this.storage.updateToken(email, {
        accessToken: refreshed.accessToken,
        refreshToken: refreshed.refreshToken,
        expiresAt: refreshed.expiresAt,
        idToken: refreshed.idToken,
      });
      this.logger.info(`Token refreshed for ${email}`);
      return refreshed.accessToken;
    } catch (error: unknown) {
      this.logger.error(error, `Failed to refresh token for ${email}`);
      // If refresh token invalid, delete stored token so user can re-authorize
      const err = error as Record<string, unknown>;
      if (err?.error === 'invalid_grant' || (err?.cause as Record<string, unknown>)?.error === 'invalid_grant') {
        this.logger.info(`Refresh token invalid for ${email}, deleting stored token`);
        await this.storage.deleteToken(email).catch((e) => this.logger.debug(`Failed to delete invalid token: ${e}`));
      }
      return null;
    }
  }

  /**
   * Get access token for a specific audience (e.g., 'orchestrator') via RFC 8693 token exchange
   */
  async getTokenForAudience(email: string, audience: string): Promise<string | null> {
    const baseAccessToken = await this.getUserAccessToken(email);
    if (!baseAccessToken) return null;

    const cacheKey = `${email}:${audience}`;
    const now = Date.now();
    const bufferMs = 5 * 60 * 1000;

    const cached = this.audienceTokenCache.get(cacheKey);
    if (cached && cached.expiresAt > now + bufferMs) {
      return cached.accessToken;
    }

    try {
      this.logger.info(`Exchanging token for audience '${audience}' for ${email}`);
      const exchanged = await this.oidcClient.exchangeTokenForAudience(baseAccessToken, audience);
      this.audienceTokenCache.set(cacheKey, {
        accessToken: exchanged.accessToken,
        expiresAt: exchanged.expiresAt,
      });
      return exchanged.accessToken;
    } catch (error) {
      this.logger.error(error, `Failed to exchange token for audience '${audience}' for ${email}`);
      return null;
    }
  }

  /** Get orchestrator-audience token (convenience) */
  async getOrchestratorToken(email: string): Promise<string | null> {
    return this.getTokenForAudience(email, this.config.oidc.orchestratorAudience);
  }

  /**
   * Complete OAuth flow by exchanging authorization code for tokens
   */
  async completeOAuthFlow(email: string, callbackUrl: string, codeVerifier: string, state: string) {
    this.logger.info(`Completing OAuth flow for ${email}`);
    const tokens = await this.oidcClient.exchangeCodeForTokens(callbackUrl, codeVerifier, state);
    await this.storage.saveToken({ email, ...tokens });
    this.logger.info(`Successfully completed OAuth flow for ${email}`);
    return tokens;
  }

  /** Get authorization URL for the user to start OAuth */
  async getAuthorizationUrl(state: string, codeVerifier: string): Promise<string> {
    return this.oidcClient.getAuthorizationUrl(state, codeVerifier);
  }

  /** Store OAuth PKCE state for later retrieval */
  async storeAuthState(state: string, email: string, codeVerifier: string): Promise<void> {
    await this.storage.saveOAuthState(state, email, codeVerifier, 604800); // 7 day TTL
  }
}
