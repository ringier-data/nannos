import { A2AClient } from '@a2a-js/sdk/client';
import {
  MessageSendParams,
  Message,
  Task,
  Part,
  TextPart,
  DataPart,
  FilePart,
  GetTaskResponse,
  TaskStatusUpdateEvent,
  TaskArtifactUpdateEvent,
} from '@a2a-js/sdk';
import { Logger } from '../utils/logger.js';
import { randomUUID } from 'crypto';
import _ from 'lodash';

/**
 * File URL to be sent with A2A request (S3 presigned URL)
 */
export interface GoogleChatFileUrl {
  name: string;
  mimeType: string;
  url: string; // S3 presigned URL
}

export interface A2AGoogleChatBasedRequest {
  userId: string; // Google Chat user ID
  projectId: string; // Google Chat project number
  spaceId: string; // Google Chat space ID
  threadId?: string; // Thread key/name if in thread
  messageId: string; // Message name
  text: string; // The user's request text
  fileUrls?: GoogleChatFileUrl[]; // Attached files with S3 presigned URLs (preferred)
  dataParts?: Record<string, unknown>[]; // Structured data (e.g., HITL decisions)
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
      'urn:nannos:a2a:activity-log:1.0, urn:nannos:a2a:work-plan:1.0, urn:nannos:a2a:feedback-request:1.0, urn:nannos:a2a:human-in-the-loop:1.0'
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
  private buildMessageParts(request: A2AGoogleChatBasedRequest): Part[] {
    const parts: Part[] = [
      {
        kind: 'text',
        text: request.text,
      } as TextPart,
    ];

    // Add structured DataParts (e.g., HITL decisions)
    if (request.dataParts && request.dataParts.length > 0) {
      for (const data of request.dataParts) {
        parts.push({
          kind: 'data',
          data,
        } as DataPart);
      }
    }

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
    }

    return parts;
  }

 /**
   * Send message to A2A agent with streaming, yielding raw SDK events.
   * The consumer is responsible for interpreting events and building the final response.
   */
  async *sendMessageStream(
    request: A2AGoogleChatBasedRequest,
    accessToken: string
  ): AsyncGenerator<Message | Task | TaskStatusUpdateEvent | TaskArtifactUpdateEvent> {
    this.logger.info(`Sending streaming message to A2A server for user ${request.userId}`);
    this.logger.debug(
      `A2A stream request details: spaceId=${request.spaceId}, threadId=${request.threadId}, contextId=${request.contextId}`
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
        googleChatUserId: request.userId,
        googleChatProjectId: request.projectId,
        googleChatSpaceId: request.spaceId,
        googleChatThreadId: request.threadId,
        googleChatMessageId: request.messageId,
        model: MODELS.ClaudeSonnet45,
        messageFormatting: 'plain', // TODO: update
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
