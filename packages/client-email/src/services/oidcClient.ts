import * as client from 'openid-client';
import { Logger } from '../utils/logger.js';
import { Config } from '../config/config.js';

/**
 * OIDC client for token exchange and refresh using openid-client
 */
export class OIDCClient {
  private readonly logger = Logger.getLogger('OIDCClient');
  private configPromise: Promise<client.Configuration> | null = null;
  private scopes = 'openid email profile offline_access';
  constructor(private readonly config: Config) {}

  /**
   * Discover and get OIDC configuration
   */
  private async getConfiguration(): Promise<client.Configuration> {
    if (!this.configPromise) {
      this.configPromise = (async () => {
        try {
          this.logger.info(`Discovering OIDC configuration from ${this.config.oidc.issuerUrl}`);
          const issuer = new URL(this.config.oidc.issuerUrl);
          const config = await client.discovery(issuer, this.config.oidc.clientId, this.config.oidc.clientSecret);
          this.logger.info('OIDC configuration discovered successfully');
          return config;
        } catch (error) {
          this.logger.error(error, `Failed to discover OIDC configuration: ${error}`);
          this.configPromise = null; // Reset to allow retry
          throw new Error(`OIDC discovery failed: ${error}`);
        }
      })();
    }
    return this.configPromise;
  }

  /**
   * Exchange authorization code for tokens
   * Note: userId and teamId must be added by the caller
   * @param callbackUrl - The full callback URL including query parameters (code, state, etc.)
   * @param codeVerifier - The PKCE code verifier
   * @param expectedState - The expected state value for validation
   */
  async exchangeCodeForTokens(
    callbackUrl: string,
    codeVerifier: string,
    expectedState: string
  ) {
    try {
      const config = await this.getConfiguration();

      // Parse the callback URL - openid-client will extract code and other params
      const currentUrl = new URL(callbackUrl);
      this.logger.info(`Exchanging code for tokens with callback URL: ${currentUrl}`);
      const tokens = await client.authorizationCodeGrant(config, currentUrl, {
        expectedState: expectedState,
        pkceCodeVerifier: codeVerifier,
      });

      return this.mapTokenSet(tokens);
    } catch (error) {
      this.logger.error(error, `Failed to exchange code for tokens: ${error}`);
      throw new Error(`OIDC token exchange failed: ${error}`);
    }
  }

  /**
   * Refresh an access token using refresh token
   * Note: userId and teamId must be added by the caller
   */
  async refreshAccessToken(refreshToken: string) {
    try {
      const config = await this.getConfiguration();

      const tokens = await client.refreshTokenGrant(config, refreshToken);

      return this.mapTokenSet(tokens);
    } catch (error) {
      this.logger.error(error, `Failed to refresh access token: ${error}`);
      throw new Error(`OIDC token refresh failed: ${error}`);
    }
  }

  /**
   * Get authorization URL for user to authorize
   */
  async getAuthorizationUrl(state: string, codeVerifier: string): Promise<string> {
    try {
      const config = await this.getConfiguration();

      const codeChallenge = await client.calculatePKCECodeChallenge(codeVerifier);

      const parameters: Record<string, string> = {
        redirect_uri: new URL('/api/v1/oauth/callback', this.config.baseUrl).toString(),
        scope: this.scopes,
        state: state,
        code_challenge: codeChallenge,
        code_challenge_method: 'S256',
      };

      const authUrl = client.buildAuthorizationUrl(config, parameters);

      return authUrl.href;
    } catch (error) {
      this.logger.error(error, `Failed to build authorization URL: ${error}`);
      throw new Error(`Failed to generate authorization URL: ${error}`);
    }
  }

  /**
   * Exchange a token for a different audience using RFC 8693 Token Exchange
   * @param subjectToken - The access token to exchange
   * @param targetAudience - The target audience for the new token
   * @returns The exchanged token and its expiry time
   */
  async exchangeTokenForAudience(
    subjectToken: string,
    targetAudience: string
  ): Promise<{ accessToken: string; expiresAt: number }> {
    try {
      const config = await this.getConfiguration();

      this.logger.info(`Exchanging token for audience: ${targetAudience}`);

      const response = await client.genericGrantRequest(config, 'urn:ietf:params:oauth:grant-type:token-exchange', {
        subject_token: subjectToken,
        subject_token_type: 'urn:ietf:params:oauth:token-type:access_token',
        audience: targetAudience,
        requested_token_type: 'urn:ietf:params:oauth:token-type:access_token',
      });

      const now = Date.now();
      const expiresIn = response.expires_in ?? 3600; // Default to 1 hour if not provided
      const expiresAt = now + expiresIn * 1000;

      this.logger.info(`Token exchange successful, expires in ${expiresIn}s`);

      return {
        accessToken: response.access_token,
        expiresAt,
      };
    } catch (error) {
      this.logger.error(error, `Failed to exchange token for audience ${targetAudience}: ${error}`);
      throw new Error(`Token exchange failed (original token=${subjectToken}): ${error}`);
    }
  }

  /**
   * Map openid-client token set to UserAuthToken
   */
  private mapTokenSet(tokens: client.TokenEndpointResponse) {
    const now = Date.now();
    const expiresIn = tokens.expires_in ?? 3600; // Default to 1 hour if not provided
    const expiresAt = now + expiresIn * 1000;

    return {
      accessToken: tokens.access_token,
      refreshToken: tokens.refresh_token,
      expiresAt,
      tokenType: tokens.token_type ?? 'Bearer',
      scope: tokens.scope,
      idToken: tokens.id_token,
      createdAt: now,
      updatedAt: now,
    };
  }
}
