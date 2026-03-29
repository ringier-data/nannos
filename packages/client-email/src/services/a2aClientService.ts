import { A2AClient } from '@a2a-js/sdk/client';
import {
  MessageSendParams,
  Message,
  Task,
  Part,
  TextPart,
  FilePart,
  DataPart,
  Artifact,
  SendMessageResponse,
  GetTaskResponse,
  TaskStatusUpdateEvent,
  TaskArtifactUpdateEvent,
  PushNotificationConfig,
} from '@a2a-js/sdk';
import { Logger } from '../utils/logger.js';
import { randomUUID } from 'crypto';
import _ from 'lodash';

/**
 * File data to be sent with A2A request (base64 encoded - legacy)
 */
export interface A2AFileData {
  name: string;
  mimeType: string;
  data: string; // base64 encoded data
}

/**
 * File URL to be sent with A2A request (S3 presigned URL)
 */
export interface A2AFileUrl {
  name: string;
  mimeType: string;
  url: string; // S3 presigned URL
}

export interface A2ARequest {
  senderEmail: string; // Sender's email address
  subject?: string; // Email subject
  text: string; // The user's request text (email body)
  files?: A2AFileData[]; // Attached files with base64 data (legacy)
  fileUrls?: A2AFileUrl[]; // Attached files with S3 URIs (preferred)
  contextId?: string; // A2A context ID for conversation continuity
  webhookUrl?: string; // Webhook URL for A2A push notifications
  webhookToken?: string; // Token for validating incoming webhook requests
}

export interface A2AResponse {
  success: boolean;
  message?: string;
  taskId?: string;
  contextId?: string;
  artifacts?: A2AArtifact[];
  state?:
    | 'completed'
    | 'working'
    | 'blocked'
    | 'failed'
    | 'submitted'
    | 'canceled'
    | 'rejected'
    | 'input-required'
    | 'auth-required';
  error?: string;
}

export interface A2AArtifact {
  artifactId: string;
  name?: string;
  parts: A2APart[];
}

export interface A2APart {
  kind: string;
  text?: string;
  data?: string; // base64 encoded binary data
  mimeType?: string;
  name?: string; // filename for file parts
  uri?: string; // URI for file parts
}

/**
 * Streaming event types
 */
export type A2AStreamEvent =
  | { kind: 'task-created'; task: A2AResponse }
  | { kind: 'status-update'; taskId: string; state: A2AResponse['state']; message?: string }
  | { kind: 'artifact-update'; taskId: string; artifact: A2AArtifact }
  | { kind: 'completed'; response: A2AResponse }
  | { kind: 'error'; error: string };

/**
 * Callback for streaming events
 */
export type A2AStreamCallback = (event: A2AStreamEvent) => Promise<void> | void;

/**
 * Create a fetch implementation that injects Bearer token authentication
 */
function createAuthenticatedFetch(accessToken: string): typeof fetch {
  return async (input, init) => {
    const headers = new Headers(init?.headers);
    headers.set('Authorization', `Bearer ${accessToken}`);

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
  private buildMessageParts(request: A2ARequest): Part[] {
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
   * Send message to A2A agent using @a2a-js/sdk
   */
  async sendMessage(request: A2ARequest, accessToken: string): Promise<A2AResponse> {
    try {
      this.logger.info(`Sending message to A2A server for user ${request.senderEmail}`);
      this.logger.debug(`A2A request details: subject=${request.subject}, contextId=${request.contextId}`);
      this.logger.debug(`A2A request text: ${request.text.substring(0, 100)}${request.text.length > 100 ? '...' : ''}`);

      // Create client with authenticated fetch
      this.logger.debug(`Creating A2AClient from card URL: ${this.agentCardUrl}`);
      const client = await A2AClient.fromCardUrl(this.agentCardUrl, {
        fetchImpl: createAuthenticatedFetch(accessToken),
      });
      this.logger.debug('A2AClient created successfully');

      const messageId = randomUUID();
      this.logger.debug(`Generated messageId: ${messageId}`);

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
          senderEmail: request.senderEmail,
          emailSubject: request.subject,
          model: MODELS.ClaudeSonnet45,
          messageFormatting: 'markdown',
        },
      };

      this.logger.trace({ a2aSendParams: sendParams }, `Sending message with params`);
      const response = await client.sendMessage(sendParams);
      this.logger.trace({ rawA2AResponse: response }, `Raw A2A response`);

      this.logger.info(`A2A server responded for user ${request.senderEmail}`);
      const parsedResponse = this.parseA2AResponse(response);
      this.logger.trace(
        { parsedA2AResponse: parsedResponse },
        `Parsed A2A response: success=${parsedResponse.success}, state=${parsedResponse.state}, taskId=${parsedResponse.taskId}`
      );
      return parsedResponse;
    } catch (error) {
      this.logger.debug(`A2A sendMessage error: ${error}`);
      return this.handleError(error, request.senderEmail);
    }
  }

  /**
   * Send message to A2A agent with streaming updates
   * Calls the callback for each event (status updates, artifacts, etc.)
   */
  async sendMessageStream(request: A2ARequest, accessToken: string, onEvent: A2AStreamCallback): Promise<A2AResponse> {
    try {
      this.logger.info(
        `Sending streaming message to A2A server for user ${request.senderEmail}: ${request.text.substring(0, 100)}${request.text.length > 100 ? '...' : ''}`
      );
      this.logger.debug(`A2A stream request details: subject=${request.subject}, contextId=${request.contextId}`);

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
          senderEmail: request.senderEmail,
          emailSubject: request.subject,
          model: MODELS.ClaudeSonnet45,
          messageFormatting: 'markdown',
        },
      };

      this.logger.debug(`Starting stream for messageId: ${messageId}`);
      const stream = client.sendMessageStream(sendParams);

      let finalResponse: A2AResponse | null = null;
      let currentTask: Task | null = null;

      for await (const event of stream) {
        this.logger.debug(`Stream event received: kind=${event.kind} content=${JSON.stringify(event)}`);

        if (event.kind === 'task') {
          // Task created or updated
          currentTask = event as Task;
          const state = currentTask.status?.state || 'submitted';
          this.logger.debug(`Task event: id=${currentTask.id}, state=${state}`);

          const taskResponse: A2AResponse = {
            success: true,
            taskId: currentTask.id,
            contextId: currentTask.contextId,
            state: state as A2AResponse['state'],
            artifacts: this.convertArtifacts(currentTask.artifacts),
            message: currentTask.status?.message
              ? this.extractTextFromParts(currentTask.status.message.parts)
              : undefined,
          };

          await onEvent({ kind: 'task-created', task: taskResponse });

          // Update final response
          finalResponse = taskResponse;
        } else if (event.kind === 'status-update') {
          // Status update event
          const statusEvent = event as TaskStatusUpdateEvent;
          const state = statusEvent.status?.state || 'working';
          this.logger.debug(`Status update: taskId=${statusEvent.taskId}, state=${state}, final=${statusEvent.final}`);

          const message = statusEvent.status?.message
            ? this.extractTextFromParts(statusEvent.status.message.parts)
            : undefined;

          await onEvent({
            kind: 'status-update',
            taskId: statusEvent.taskId,
            state: state as A2AResponse['state'],
            message,
          });

          // Update final response state
          if (finalResponse) {
            finalResponse.state = state as A2AResponse['state'];
            if (message) finalResponse.message = message;
          }
        } else if (event.kind === 'artifact-update') {
          // Artifact update event
          const artifactEvent = event as TaskArtifactUpdateEvent;
          this.logger.debug(
            `Artifact update: taskId=${artifactEvent.taskId}, artifactId=${artifactEvent.artifact?.artifactId}`
          );

          if (artifactEvent.artifact) {
            const artifact: A2AArtifact = {
              artifactId: artifactEvent.artifact.artifactId,
              name: artifactEvent.artifact.name,
              parts: artifactEvent.artifact.parts.map((p): A2APart => {
                if (p.kind === 'text') {
                  return { kind: p.kind, text: (p as TextPart).text };
                } else if (p.kind === 'file') {
                  const filePart = p as FilePart;
                  const fileData = filePart.file as { bytes?: string; mimeType?: string; name?: string };
                  return {
                    kind: p.kind,
                    data: fileData.bytes,
                    mimeType: fileData.mimeType,
                    name: fileData.name,
                  };
                } else if (p.kind === 'data') {
                  const dataPart = p as DataPart;
                  // DataPart contains structured JSON data, convert to string if needed
                  return {
                    kind: p.kind,
                    text: JSON.stringify(dataPart.data),
                  };
                }
                // Fallback for any other part types
                return { kind: (p as Part).kind };
              }),
            };

            await onEvent({
              kind: 'artifact-update',
              taskId: artifactEvent.taskId,
              artifact,
            });

            // Add artifact to final response
            if (finalResponse) {
              if (!finalResponse.artifacts) finalResponse.artifacts = [];
              finalResponse.artifacts.push(artifact);
            }
          }
        } else if (event.kind === 'message') {
          // Direct message response
          const message = event as Message;
          this.logger.debug(`Message event: contextId=${message.contextId}`);

          finalResponse = {
            success: true,
            message: this.extractTextFromParts(message.parts),
            contextId: message.contextId,
            state: 'completed',
          };

          await onEvent({ kind: 'completed', response: finalResponse });
        } else {
          this.logger.debug(`Unknown stream event kind: ${_.get(event, 'kind')}`);
        }
      }

      this.logger.info(`Stream finished for user ${request.senderEmail}`);

      // Build final response if we have a task
      if (finalResponse) {
        // Extract message from artifacts if completed
        if (finalResponse.state === 'completed' && finalResponse.artifacts?.length) {
          finalResponse.message = finalResponse.artifacts
            .map((a) =>
              a.parts
                .filter((p) => p.text)
                .map((p) => p.text)
                .join('\n')
            )
            .filter((t) => t)
            .join('\n\n');
        }

        await onEvent({ kind: 'completed', response: finalResponse });
        return finalResponse;
      }

      // No response received
      return {
        success: false,
        error: 'No response received from A2A server',
      };
    } catch (error) {
      this.logger.error(error, `A2A stream error (accessToken=${accessToken}): ${error}`);
      const errorResponse = this.handleError(error, request.senderEmail);
      await onEvent({ kind: 'error', error: errorResponse.error || 'Unknown error' });
      return errorResponse;
    }
  }

  /**
   * Parse A2A SDK response
   */
  private parseA2AResponse(response: SendMessageResponse): A2AResponse {
    this.logger.debug(`Parsing A2A response`);

    // Check for error response
    if ('error' in response && response.error) {
      this.logger.debug(`A2A response contains error: ${response.error.message}`);
      return {
        success: false,
        error: response.error.message || 'A2A request failed',
      };
    }

    // Get the result from success response
    const result = (response as { result: Message | Task }).result;
    this.logger.debug(`A2A result kind: ${result.kind}`);

    // Direct message response (no task)
    if (result.kind === 'message') {
      const message = result as Message;
      this.logger.debug(`A2A message response: contextId=${message.contextId}, parts=${message.parts?.length}`);
      return {
        success: true,
        message: this.extractTextFromParts(message.parts),
        contextId: message.contextId,
        state: 'completed',
      };
    }

    // Task response
    if (result.kind === 'task') {
      const task = result as Task;
      const state = task.status?.state || 'working';
      this.logger.debug(
        `A2A task response: id=${task.id}, state=${state}, contextId=${task.contextId}, artifacts=${task.artifacts?.length}`
      );

      return {
        success: true,
        taskId: task.id,
        contextId: task.contextId,
        state: state as A2AResponse['state'],
        artifacts: this.convertArtifacts(task.artifacts),
        message:
          state === 'completed'
            ? this.extractArtifactsText(task.artifacts)
            : task.status?.message
              ? this.extractTextFromParts(task.status.message.parts)
              : 'Task in progress...',
      };
    }

    this.logger.debug(`Unknown A2A response kind: ${(result as any).kind}`);
    return {
      success: false,
      error: 'Unknown response format from A2A server',
    };
  }

  /**
   * Convert SDK Artifacts to our A2AArtifact format
   */
  private convertArtifacts(artifacts?: Artifact[]): A2AArtifact[] | undefined {
    if (!artifacts || artifacts.length === 0) return undefined;

    return artifacts.map((a) => ({
      artifactId: a.artifactId,
      name: a.name,
      parts: a.parts.map((p): A2APart => {
        if (p.kind === 'text') {
          return { kind: p.kind, text: (p as TextPart).text };
        } else if (p.kind === 'file') {
          const filePart = p as FilePart;
          const fileData = filePart.file as { bytes?: string; mimeType?: string; name?: string };
          return {
            kind: p.kind,
            data: fileData.bytes,
            mimeType: fileData.mimeType,
            name: fileData.name,
          };
        } else if (p.kind === 'data') {
          const dataPart = p as DataPart;
          return {
            kind: p.kind,
            text: JSON.stringify(dataPart.data),
          };
        }
        return { kind: (p as Part).kind };
      }),
    }));
  }

  /**
   * Extract text from A2A parts
   */
  private extractTextFromParts(parts: Part[]): string {
    if (!parts || parts.length === 0) return '';
    return parts
      .filter((p): p is TextPart => p.kind === 'text' && 'text' in p)
      .map((p) => p.text)
      .join('\n');
  }

  /**
   * Extract text from artifacts
   */
  private extractArtifactsText(artifacts?: Artifact[]): string {
    if (!artifacts || artifacts.length === 0) return 'Task completed';
    return artifacts
      .map((a) => this.extractTextFromParts(a.parts))
      .filter((text) => text)
      .join('\n\n');
  }

  /**
   * Send message to A2A agent asynchronously with webhook callback
   * Returns immediately with task ID, A2A server will call webhook when complete
   */
  async sendMessageAsync(request: A2ARequest, accessToken: string): Promise<A2AResponse> {
    try {
      this.logger.info(`Sending async message to A2A server for user ${request.senderEmail}`);
      this.logger.debug(
        `A2A async request details: subject=${request.subject}, contextId=${request.contextId}, webhookUrl=${request.webhookUrl}`
      );

      // Create client with authenticated fetch
      const client = await A2AClient.fromCardUrl(this.agentCardUrl, {
        fetchImpl: createAuthenticatedFetch(accessToken),
      });

      const messageId = randomUUID();

      // Build message parts (text + optional files)
      const messageParts = this.buildMessageParts(request);

      // Build push notification config if webhook URL is provided
      let pushNotificationConfig: PushNotificationConfig | undefined;
      if (request.webhookUrl) {
        pushNotificationConfig = {
          url: request.webhookUrl,
          token: request.webhookToken || randomUUID(), // Token for validating incoming webhook requests
        };
        this.logger.debug(`Push notification configured: url=${request.webhookUrl}`);
      }

      // Build A2A message send params with proper push notification configuration
      const sendParams: MessageSendParams = {
        message: {
          messageId,
          kind: 'message',
          role: 'user',
          parts: messageParts,
          ...(request.contextId && { contextId: request.contextId }),
        },
        metadata: {
          senderEmail: request.senderEmail,
          emailSubject: request.subject,
          model: MODELS.ClaudeSonnet45,
          messageFormatting: 'markdown',
        },
        // Use proper A2A configuration for push notifications (not metadata)
        ...(pushNotificationConfig && {
          configuration: {
            blocking: false,
            pushNotificationConfig,
          },
        }),
      };

      this.logger.debug({ a2aSendParams: sendParams }, `Sending async message with params`);
      const response = await client.sendMessage(sendParams);
      this.logger.debug({ rawA2AResponse: response }, `Raw A2A async response`);

      this.logger.info(`A2A server accepted async request for user ${request.senderEmail}`);
      return this.parseA2AResponse(response);
    } catch (error) {
      this.logger.error(`A2A sendMessageAsync error: ${error}`);
      return this.handleError(error, request.senderEmail);
    }
  }

  /**
   * Get status of an A2A task
   */
  async getTaskStatus(taskId: string, accessToken: string): Promise<A2AResponse> {
    try {
      this.logger.debug(`Getting task status for taskId: ${taskId}`);

      // Create client with authenticated fetch
      const client = await A2AClient.fromCardUrl(this.agentCardUrl, {
        fetchImpl: createAuthenticatedFetch(accessToken),
      });

      this.logger.debug(`Calling getTask for taskId: ${taskId}`);
      const response: GetTaskResponse = await client.getTask({ id: taskId });
      this.logger.debug({ response }, `getTask response`);

      // Check for error response
      if ('error' in response && response.error) {
        this.logger.debug(`getTask error response: ${response.error.message}`);
        return {
          success: false,
          error: response.error.message || 'Failed to get task status',
        };
      }

      const task = (response as { result: Task }).result;
      const state = task.status?.state || 'working';
      this.logger.debug(`Task status: id=${task.id}, state=${state}, artifacts=${task.artifacts?.length}`);

      return {
        success: true,
        taskId: task.id,
        contextId: task.contextId,
        state: state as A2AResponse['state'],
        artifacts: this.convertArtifacts(task.artifacts),
        message:
          state === 'completed'
            ? this.extractArtifactsText(task.artifacts)
            : task.status?.message
              ? this.extractTextFromParts(task.status.message.parts)
              : 'Task in progress...',
      };
    } catch (error) {
      this.logger.debug(`getTaskStatus error: ${error}`);
      return this.handleError(error, taskId);
    }
  }

  /**
   * Handle errors from A2A server
   */
  private handleError(error: unknown, context: string): A2AResponse {
    if (error instanceof Error) {
      this.logger.error(`A2A error for ${context}: ${error.message}`);

      // Check for network errors
      if (error.message.includes('fetch') || error.message.includes('network')) {
        return {
          success: false,
          error: 'A2A server is not responding. Please try again later.',
        };
      }

      return {
        success: false,
        error: error.message,
      };
    }

    // Unknown error
    this.logger.error(`Unexpected error calling A2A server: ${error}`);
    return {
      success: false,
      error: 'An unexpected error occurred. Please try again.',
    };
  }
}
