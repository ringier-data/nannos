import { Logger } from '../utils/logger.js';
import { UserAuthService } from './userAuthService.js';
import { Config } from '../config/config.js';

const logger = Logger.getLogger('FeedbackService');

type FeedbackRating = 'positive' | 'negative';

/**
 * Maps a platform-specific response message identifier back to the A2A
 * context / task IDs that are required by the console-backend feedback API.
 */
export interface ResponseMapping {
  contextId: string; // A2A conversation / context ID
  taskId: string; // A2A task ID (used as message_id for feedback)
  userId: string; // Slack user ID
  teamId: string; // Slack team ID
  createdAt: number;
}

/**
 * In-memory cache that maps Slack response messages (`channel:ts`) to A2A
 * identifiers.  Entries are evicted after `ttlMs` (default 24 h).
 */
export class ResponseMappingCache {
  private readonly cache = new Map<string, ResponseMapping>();
  private readonly ttlMs: number;

  constructor(ttlMs = 24 * 60 * 60 * 1000) {
    this.ttlMs = ttlMs;
  }

  buildKey(channelId: string, ts: string): string {
    return `${channelId}:${ts}`;
  }

  set(channelId: string, ts: string, mapping: ResponseMapping): void {
    const key = this.buildKey(channelId, ts);
    this.cache.set(key, mapping);
    this.cleanup();
  }

  get(channelId: string, ts: string): ResponseMapping | undefined {
    const key = this.buildKey(channelId, ts);
    const entry = this.cache.get(key);
    if (entry && Date.now() - entry.createdAt > this.ttlMs) {
      this.cache.delete(key);
      return undefined;
    }
    return entry;
  }

  private cleanup(): void {
    // Evict stale entries every 100 inserts (lightweight)
    if (this.cache.size % 100 !== 0) return;
    const now = Date.now();
    for (const [key, val] of this.cache) {
      if (now - val.createdAt > this.ttlMs) {
        this.cache.delete(key);
      }
    }
  }
}

/**
 * Service for submitting message feedback to the console-backend API.
 *
 * Uses RFC 8693 token exchange (via `UserAuthService`) to obtain a
 * console-backend-scoped access token before calling the feedback endpoint.
 */
export class FeedbackService {
  private readonly userAuthService: UserAuthService;
  private readonly consoleBackendUrl: string;
  private readonly audience: string;
  readonly responseMapping = new ResponseMappingCache();

  constructor(userAuthService: UserAuthService, config: Config) {
    if (!config.consoleBackend) {
      throw new Error('CONSOLE_BACKEND_URL is required for FeedbackService');
    }
    this.userAuthService = userAuthService;
    this.consoleBackendUrl = config.consoleBackend.url.replace(/\/+$/, '');
    this.audience = config.consoleBackend.audience;
  }

  /**
   * Submit positive or negative feedback for a specific A2A response.
   */
  async submitFeedback(
    userId: string,
    teamId: string,
    conversationId: string,
    messageId: string,
    rating: FeedbackRating,
    taskId?: string,
    subAgentId?: string,
  ): Promise<boolean> {
    try {
      const accessToken = await this.userAuthService.getTokenForAudience(userId, teamId, this.audience);
      if (!accessToken) {
        logger.warn(
          `Cannot submit feedback: no console-backend token for user ${userId}. Failed to exchange token for audience: ${this.audience}`
        );
        return false;
      }

      logger.debug(
        `Token exchanged successfully for audience: ${this.audience}. Token starts with: ${accessToken.substring(0, 20)}...`
      );

      const url = `${this.consoleBackendUrl}/api/v1/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(messageId)}/feedback`;

      const body: Record<string, string> = { rating };
      if (taskId) body.task_id = taskId;
      if (subAgentId) body.sub_agent_id = subAgentId;

      const response = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: `Bearer ${accessToken}`,
        },
        body: JSON.stringify(body),
      });

      if (!response.ok) {
        let errorDetails = '';
        try {
          const errorBody = await response.text();
          errorDetails = errorBody ? ` — ${errorBody}` : '';
        } catch (e) {
          // ignore
        }
        logger.warn(
          `Feedback submission failed: ${response.status} ${response.statusText}${errorDetails}. URL: ${url}. Audience: ${this.audience}`
        );
        return false;
      }

      logger.info(`Feedback submitted: ${rating} for conversation=${conversationId} message=${messageId}`);
      return true;
    } catch (error) {
      logger.error(error, `Failed to submit feedback: ${error}`);
      return false;
    }
  }

  /**
   * Remove previously-submitted feedback.
   */
  async deleteFeedback(
    userId: string,
    teamId: string,
    conversationId: string,
    messageId: string,
  ): Promise<boolean> {
    try {
      const accessToken = await this.userAuthService.getTokenForAudience(userId, teamId, this.audience);
      if (!accessToken) {
        logger.warn(`Cannot delete feedback: no console-backend token for user ${userId}`);
        return false;
      }

      const url = `${this.consoleBackendUrl}/api/v1/conversations/${encodeURIComponent(conversationId)}/messages/${encodeURIComponent(messageId)}/feedback`;

      const response = await fetch(url, {
        method: 'DELETE',
        headers: {
          Authorization: `Bearer ${accessToken}`,
        },
      });

      if (!response.ok && response.status !== 404) {
        logger.warn(`Feedback deletion failed: ${response.status} ${response.statusText}`);
        return false;
      }

      logger.info(`Feedback deleted for conversation=${conversationId} message=${messageId}`);
      return true;
    } catch (error) {
      logger.error(error, `Failed to delete feedback: ${error}`);
      return false;
    }
  }
}
