export interface UserAuthToken {
  userId: string; // Google Chat user ID (e.g. users/12345)
  projectId: string; // Google Chat project number;
  accessToken: string; // OIDC access token
  refreshToken?: string; // OIDC refresh token
  expiresAt: number; // Unix timestamp when token expires
  tokenType: string; // Usually "Bearer"
  scope?: string; // Granted scopes
  idToken?: string; // OIDC ID token
  createdAt: number; // Unix timestamp when token was created
  updatedAt: number; // Unix timestamp when token was last updated
}

export interface UserAuthRecord extends UserAuthToken {
  // Record may have additional metadata
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
  getToken(userId: string, projectId: string): Promise<UserAuthToken | null>;
  deleteToken(userId: string, projectId: string): Promise<void>;
  updateToken(
    userId: string,
    projectId: string,
    updates: {
      accessToken?: string;
      refreshToken?: string;
      expiresAt?: number;
      idToken?: string;
    }
  ): Promise<void>;
  hasValidToken(userId: string, projectId: string): Promise<boolean>;
}

/**
 * Context record for mapping Google Chat threads to A2A context IDs
 */
export interface ContextRecord {
  contextKey: string;
  contextId: string;
  lastProcessedMessageId?: string;
  createdAt: number;
  updatedAt: number;
}

/**
 * Interface for context storage
 */
export interface IContextStore {
  set(key: string, contextId: string, lastProcessedMessageId?: string): Promise<void>;
  get(key: string): Promise<ContextRecord | null>;
  delete(key: string): Promise<void>;
  buildKey(projectId: string, spaceId: string, threadId: string): string;
}

/**
 * Pending request data structure
 */
export interface PendingRequest {
  visitorId: string; // PK: {projectId}:{userId}
  text: string;
  spaceId: string;
  threadId: string;
  messageId: string;
  userEmail: string;
  source: 'space_message' | 'direct_message';
  createdAt: number;
}

/**
 * Interface for pending request storage
 */
export interface IPendingRequestStore {
  buildVisitorId(projectId: string, userId: string): string;
  set(request: PendingRequest): Promise<void>;
  consume(projectId: string, userId: string): Promise<PendingRequest | null>;
  delete(projectId: string, userId: string): Promise<void>;
}

/**
 * In-flight task data structure
 */
export interface InFlightTask {
  taskId: string; // PK: A2A task ID
  visitorId: string; // {projectId}:{userId} for querying by user
  userId: string; // Google Chat user ID
  projectId: string; // Google Chat project number
  spaceId: string; // Google Chat space ID
  threadId: string; // Thread key/name to reply in
  messageId: string; // Original message name (for updates)
  statusMessageId?: string; // Status message name (for updates)
  contextKey: string; // Context store key for conversation continuity
  webhookToken?: string; // Token for validating A2A push notifications
  source: 'space_message' | 'direct_message';
  createdAt: number;
  ttl: number; // Unix timestamp (seconds) for cleanup
}

/**
 * Interface for in-flight task storage
 */
export interface IInFlightTaskStore {
  buildVisitorId(projectId: string, userId: string): string;
  save(task: Omit<InFlightTask, 'ttl'>): Promise<void>;
  get(taskId: string): Promise<InFlightTask | null>;
  updateStatusMessageId(taskId: string, statusMessageId: string): Promise<void>;
  delete(taskId: string): Promise<void>;
  getByUser(projectId: string, userId: string): Promise<InFlightTask[]>;
  getAll(minAgeMs?: number): Promise<InFlightTask[]>;
  touch(taskId: string): Promise<void>;
}

/**
 * OAuth state data structure
 */
export interface OAuthStateData {
  userId: string;
  projectId: string;
  codeVerifier: string;
  expiresAt: number;
}

/**
 * Interface for OAuth state storage
 */
export interface IOAuthStateStore {
  set(state: string, userId: string, projectId: string, codeVerifier: string, ttlSeconds?: number): Promise<void>;
  get(state: string): Promise<OAuthStateData | null>;
  consume(state: string): Promise<{ userId: string; projectId: string; codeVerifier: string } | null>;
}
