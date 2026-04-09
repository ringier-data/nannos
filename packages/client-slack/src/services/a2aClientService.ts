import { A2AClient } from '@a2a-js/sdk/client';
import {
  MessageSendParams,
  Message,
  Task,
  Part,
  TextPart,
  FilePart,
  GetTaskResponse,
  TaskStatusUpdateEvent,
  TaskArtifactUpdateEvent,
} from '@a2a-js/sdk';
import { Logger } from '../utils/logger.js';
import { randomUUID } from 'crypto';
import _ from 'lodash';

/**
 * File data to be sent with A2A request (base64 encoded - legacy)
 */
export interface SlackFileData {
  name: string;
  mimeType: string;
  data: string; // base64 encoded data
}

/**
 * File URL to be sent with A2A request (S3 presigned URL)
 */
export interface SlackFileUrl {
  name: string;
  mimeType: string;
  url: string; // S3 presigned URL
}

export interface A2ASlackBasedRequest {
  userId: string; // Slack user ID
  teamId: string; // Slack team/workspace ID
  channelId: string; // Slack channel ID
  threadTs?: string; // Thread timestamp if in thread
  messageTs: string; // Message timestamp
  text: string; // The user's request text
  files?: SlackFileData[]; // Attached files with base64 data (legacy)
  fileUrls?: SlackFileUrl[]; // Attached files with S3 presigned URLs (preferred)
  contextId?: string; // A2A context ID for conversation continuity
  webhookUrl?: string; // Webhook URL for A2A push notifications
  webhookToken?: string; // Token for validating incoming webhook requests
}

/**
 * Create a fetch implementation that injects Bearer token authentication
 */
function createAuthenticatedFetch(accessToken: string): typeof fetch {
  return async (input, init) => {
    const headers = new Headers(init?.headers);
    headers.set('Authorization', `Bearer ${accessToken}`);
    headers.set(
      'X-A2A-Extensions',
      'urn:nannos:a2a:activity-log:1.0, urn:nannos:a2a:work-plan:1.0, urn:nannos:a2a:intermediate-output:1.0'
    );
    return fetch(input, {
      ...init,
      headers,
    });
  };
}

const MODELS = {
  ClaudeSonnet45: 'claude-sonnet-4.5',
  GPT4o: 'gpt4o',
};

/**
 * HTTP client to communicate with A2A server using @a2a-js/sdk
 */
export class A2AClientService {
  private readonly agentCardUrl: string;
  private readonly logger = Logger.getLogger('A2AClientService');

  constructor(baseUrl: string, _timeout: number = 30000) {
    // Use URL constructor to properly handle trailing slashes
    this.agentCardUrl = new URL('.well-known/agent-card.json', baseUrl).toString();
    this.logger.debug(`A2AClientService initialized with agentCardUrl: ${this.agentCardUrl}`);
  }

  /**
   * Build message parts from request (text + optional files)
   * Prefers FileWithUri (S3 presigned URLs) over FileWithBytes (base64)
   */
  private buildMessageParts(request: A2ASlackBasedRequest): Part[] {
    const parts: Part[] = [
      {
        kind: 'text',
        text: request.text,
      } as TextPart,
    ];

    // Prefer fileUrls (S3 presigned URLs) over files (base64)
    if (request.fileUrls && request.fileUrls.length > 0) {
      for (const file of request.fileUrls) {
        // Use FilePart with FileWithUri for S3 presigned URLs
        parts.push({
          kind: 'file',
          file: {
            uri: file.url,
            mimeType: file.mimeType,
            name: file.name,
          },
        } as FilePart);
        this.logger.debug(`Added file part with URI: ${file.name} (${file.mimeType})`);
      }
      this.logger.info(`Including ${request.fileUrls.length} file(s) with URLs in A2A request`);
    } else if (request.files && request.files.length > 0) {
      // Fallback to base64 encoded files
      for (const file of request.files) {
        // Use FilePart with FileWithBytes for inline base64 data
        parts.push({
          kind: 'file',
          file: {
            bytes: file.data,
            mimeType: file.mimeType,
            name: file.name,
          },
        } as FilePart);
        this.logger.debug(`Added file part with bytes: ${file.name} (${file.mimeType})`);
      }
      this.logger.info(`Including ${request.files.length} file(s) with bytes in A2A request`);
    }

    return parts;
  }

  /**
   * Send message to A2A agent with streaming, yielding raw SDK events.
   * The consumer is responsible for interpreting events and building the final response.
   */
  async *sendMessageStream(
    request: A2ASlackBasedRequest,
    accessToken: string
  ): AsyncGenerator<Message | Task | TaskStatusUpdateEvent | TaskArtifactUpdateEvent> {
    this.logger.info(`Sending streaming message to A2A server for user ${request.userId}`);
    this.logger.debug(
      `A2A stream request details: channelId=${request.channelId}, threadTs=${request.threadTs}, contextId=${request.contextId}`
    );

    // Create client with authenticated fetch
    const client = await A2AClient.fromCardUrl(this.agentCardUrl, {
      fetchImpl: createAuthenticatedFetch(accessToken),
    });

    const messageId = randomUUID();

    // Build message parts (text + optional files)
    const messageParts = this.buildMessageParts(request);

    // Build A2A message send params
    const sendParams: MessageSendParams = {
      message: {
        messageId,
        kind: 'message',
        role: 'user',
        parts: messageParts,
        ...(request.contextId && { contextId: request.contextId }),
      },
      metadata: {
        slackUserId: request.userId,
        slackTeamId: request.teamId,
        slackChannelId: request.channelId,
        slackThreadTs: request.threadTs,
        slackMessageTs: request.messageTs,
        model: MODELS.ClaudeSonnet45,
        messageFormatting: 'slack',
      },
    };

    const stream = client.sendMessageStream(sendParams);

    for await (const event of stream) {
      this.logger.debug(`Stream event received: kind=${event.kind} content=${JSON.stringify(event)}`);
      yield event;
    }

    this.logger.info(`Stream finished for user ${request.userId}`);
  }

  async getTaskStatus(taskId: string, accessToken: string): Promise<GetTaskResponse> {
    this.logger.debug(`Getting task status for taskId: ${taskId}`);

    // Create client with authenticated fetch
    const client = await A2AClient.fromCardUrl(this.agentCardUrl, {
      fetchImpl: createAuthenticatedFetch(accessToken),
    });

    this.logger.debug(`Calling getTask for taskId: ${taskId}`);
    const response: GetTaskResponse = await client.getTask({ id: taskId });
    this.logger.debug(response, `getTask response`);

    // Check for error response
    if ('error' in response && response.error) {
      this.logger.debug(`getTask error response: ${response.error.message}`);
      return response;
    }

    const task = (response as { result: Task }).result;
    const state = task.status?.state || 'working';
    this.logger.debug(`Task status: id=${task.id}, state=${state}, artifacts=${task.artifacts?.length}`);
    return response;
  }
}
