export interface UserAuthToken {
  userId: string; // Slack user ID
  teamId: string; // Slack team/workspace ID
  accessToken: string; // OIDC access token
  refreshToken?: string; // OIDC refresh token
  expiresAt: number; // Unix timestamp when token expires
  tokenType: string; // Usually "Bearer"
  scope?: string; // Granted scopes
  idToken?: string; // OIDC ID token
  createdAt: number; // Unix timestamp when token was created
  updatedAt: number; // Unix timestamp when token was last updated
}

export interface UserAuthRecord extends UserAuthToken {}

/**
 * Bot installation record — one row per registered Slack App persona
 */
export interface BotInstallation {
  appId: string; // Slack App ID (PK)
  teamId: string; // Slack workspace/team ID
  botToken: string; // Slack bot token (xoxb-...)
  signingSecret: string; // Slack signing secret for this app
  botName: string; // Display name shown in messages
  avatarUrl?: string; // Optional bot avatar image URL
  hasAvatar: boolean; // Whether binary avatar data exists
  slashCommand: string; // e.g. '/nannos' or '/my-team-bot'
  isActive: boolean;
  createdAt: Date;
  updatedAt: Date;
}

/**
 * Interface for bot installation storage
 */
export interface IBotInstallationStore {
  /** Primary runtime lookup — by Slack App ID from api_app_id in request body */
  getByAppId(appId: string): Promise<BotInstallation | null>;
  /** Returns all bots for a workspace (may be multiple) */
  getByTeamId(teamId: string): Promise<BotInstallation[]>;
  /** List all installations */
  listAll(): Promise<BotInstallation[]>;
  /** Create or update an installation (keyed by appId) */
  upsert(bot: Omit<BotInstallation, 'createdAt' | 'updatedAt' | 'hasAvatar'>): Promise<void>;
  /** Soft-deactivate a bot */
  deactivate(appId: string): Promise<void>;
  /** Store avatar binary data */
  updateAvatar(appId: string, data: Buffer, mimeType: string): Promise<void>;
  /** Retrieve avatar binary data */
  getAvatar(appId: string): Promise<{ data: Buffer; mimeType: string } | null>;
  /** Delete avatar binary data */
  deleteAvatar(appId: string): Promise<void>;
}

// =============================================================================
// Storage Interface Definitions
// These interfaces define the contracts that storage implementations must satisfy
// =============================================================================

/**
 * Interface for user authentication token storage
 */
export interface IUserAuthStorage {
  saveToken(token: UserAuthToken): Promise<void>;
  getToken(userId: string, teamId: string): Promise<UserAuthToken | null>;
  deleteToken(userId: string, teamId: string): Promise<void>;
  updateToken(
    userId: string,
    teamId: string,
    updates: {
      accessToken?: string;
      refreshToken?: string;
      expiresAt?: number;
      idToken?: string;
    }
  ): Promise<void>;
  hasValidToken(userId: string, teamId: string): Promise<boolean>;
}

/**
 * Context record for mapping Slack threads to A2A context IDs
 */
export interface ContextRecord {
  contextKey: string;
  contextId: string;
  lastProcessedTs?: string;
  createdAt: number;
  updatedAt: number;
}

/**
 * Interface for context storage
 */
export interface IContextStore {
  set(key: string, contextId: string, lastProcessedTs?: string): Promise<void>;
  get(key: string): Promise<ContextRecord | null>;
  delete(key: string): Promise<void>;
  buildKey(teamId: string, channelId: string, threadTs: string): string;
}

/**
 * Pending request data structure
 */
export interface PendingRequest {
  visitorId: string; // PK: {teamId}:{userId}
  text: string;
  channelId: string;
  threadTs: string;
  messageTs: string;
  source: 'app_mention' | 'direct_message';
  appId?: string; // Slack App ID that received the original message (for multi-bot routing)
  createdAt: number;
}

/**
 * Interface for pending request storage
 */
export interface IPendingRequestStore {
  buildVisitorId(teamId: string, userId: string): string;
  set(request: PendingRequest): Promise<void>;
  consume(teamId: string, userId: string): Promise<PendingRequest | null>;
  delete(teamId: string, userId: string): Promise<void>;
}

/**
 * In-flight task data structure
 */
export interface InFlightTask {
  taskId: string; // PK: A2A task ID
  visitorId: string; // {teamId}:{userId} for querying by user
  userId: string; // Slack user ID
  teamId: string; // Slack team/workspace ID
  channelId: string; // Slack channel ID
  threadTs: string; // Thread timestamp to reply to
  messageTs: string; // Original message timestamp (for reactions)
  statusMessageTs?: string; // Status message timestamp (for updates)
  contextKey: string; // Context store key for conversation continuity
  webhookToken?: string; // Token for validating A2A push notifications
  source: 'app_mention' | 'direct_message';
  appId?: string; // Slack App ID that received the message (for multi-bot token routing)
  createdAt: number;
  ttl: number; // Unix timestamp (seconds) for cleanup
}

/**
 * Interface for in-flight task storage
 */
export interface IInFlightTaskStore {
  buildVisitorId(teamId: string, userId: string): string;
  save(task: Omit<InFlightTask, 'ttl'>): Promise<void>;
  get(taskId: string): Promise<InFlightTask | null>;
  updateStatusMessageTs(taskId: string, statusMessageTs: string): Promise<void>;
  delete(taskId: string): Promise<void>;
  getByUser(teamId: string, userId: string): Promise<InFlightTask[]>;
  getAll(minAgeMs?: number): Promise<InFlightTask[]>;
  touch(taskId: string): Promise<void>;
}

/**
 * OAuth state data structure
 */
export interface OAuthStateData {
  userId: string;
  teamId: string;
  codeVerifier: string;
  expiresAt: number;
}

/**
 * Interface for OAuth state storage
 */
export interface IOAuthStateStore {
  set(state: string, userId: string, teamId: string, codeVerifier: string, ttlSeconds?: number): Promise<void>;
  get(state: string): Promise<OAuthStateData | null>;
  consume(state: string): Promise<{ userId: string; teamId: string; codeVerifier: string } | null>;
}

/**
 * Admin session data structure for V2 API cookie-based authentication
 */
export interface AdminSession {
  sessionId: string;
  sub: string;
  email?: string;
  groups: string[];
  accessToken: string;
  refreshToken?: string;
  accessTokenExpiresAt: number; // Unix timestamp (ms)
  createdAt: number; // Unix timestamp (ms)
  expiresAt: number; // Unix timestamp (ms)
}

/**
 * Interface for admin session storage
 */
export interface IAdminSessionStore {
  createSession(session: AdminSession): Promise<void>;
  getSession(sessionId: string): Promise<AdminSession | null>;
  updateSession(
    sessionId: string,
    updates: { accessToken: string; refreshToken?: string; accessTokenExpiresAt: number }
  ): Promise<void>;
  deleteSession(sessionId: string): Promise<void>;
  deleteExpired(): Promise<number>;
}
