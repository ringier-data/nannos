/**
 * Answering a scheduled run that parked on its owner's authorization.
 *
 * A scheduled run whose tool needs the owner's credential stops and asks, as an
 * in-task-auth card in the job's delivery channel. The answer cannot travel the
 * route an interactive card's answer takes (`handleIncomingMessage` → the
 * orchestrator): that is a chat turn, and a chat turn is new work. The
 * orchestrator would propose a fresh delegation task id on a sub-agent thread
 * that is already parked, and the executor would reject it. See
 * docs/adr/0009-authorization-parks-a-scheduled-run-it-does-not-fail-it.md.
 *
 * So the answer goes to console-backend, which owns the run: it re-resolves the
 * owner's offline token, addresses the parked agent-runner task directly, and
 * records the continued work as a run of its own. That last part is why the
 * scheduler brokers it rather than this client calling agent-runner — a resumed
 * run is minutes of tool calls that must be heartbeated and swept if the process
 * executing them dies, and a floating promise in a Slack handler owns none of it.
 *
 * The URL is built from THIS client's own configuration. The ask declares a
 * logical target (`{service, endpoint}`), never an address: the payload arrives
 * over a webhook, and a reply target taken on trust from one would be a
 * credential-forwarding primitive.
 */

import { Logger } from '../utils/logger.js';
import type { Config } from '../config/config.js';
import type { UserAuthService } from './userAuthService.js';

const logger = Logger.getLogger('scheduledRunResumeService');

/** The only service/endpoint pair this client will post an answer to. */
const RESUME_TARGET = { service: 'console-backend', endpoint: 'scheduled_run_resume' } as const;

export type AuthDecision = 'approved' | 'declined';

/**
 * Where a parked run says its answer goes, as carried in the scheduler payload.
 * Logical on purpose — see the module docstring.
 */
export interface ReplyTo {
  service?: string;
  endpoint?: string;
  scheduled_job_id?: number;
  scheduled_job_run_id?: number;
}

export type ResumeOutcome =
  | { kind: 'resumed' }
  /** The run was not waiting any more: already answered, superseded, or closed. */
  | { kind: 'already-handled' }
  | { kind: 'failed'; reason: string };

/** Whether *replyTo* names the one target this client is willing to post to. */
export function isResumeTarget(replyTo: ReplyTo | undefined): replyTo is ReplyTo &
  Required<Pick<ReplyTo, 'scheduled_job_id' | 'scheduled_job_run_id'>> {
  return (
    !!replyTo &&
    replyTo.service === RESUME_TARGET.service &&
    replyTo.endpoint === RESUME_TARGET.endpoint &&
    typeof replyTo.scheduled_job_id === 'number' &&
    typeof replyTo.scheduled_job_run_id === 'number'
  );
}

export class ScheduledRunResumeService {
  private readonly userAuthService: UserAuthService;
  private readonly consoleBackendUrl: string;
  private readonly audience: string;

  constructor(userAuthService: UserAuthService, config: Config) {
    if (!config.consoleBackend) {
      throw new Error('CONSOLE_BACKEND_URL is required for ScheduledRunResumeService');
    }
    this.userAuthService = userAuthService;
    this.consoleBackendUrl = config.consoleBackend.url.replace(/\/+$/, '');
    this.audience = config.consoleBackend.audience;
  }

  /**
   * Deliver the owner's decision to a parked run.
   *
   * Returns 202 as `resumed` — the run continues in the background and its result
   * is delivered to this channel like any other run's, so there is nothing to wait
   * for here. A 409 is an ordinary outcome rather than an error: cards are durable
   * and clicks are late, so the run may already have been answered.
   */
  async resume(
    userId: string,
    projectId: string,
    replyTo: ReplyTo,
    decision: AuthDecision,
  ): Promise<ResumeOutcome> {
    if (!isResumeTarget(replyTo)) {
      logger.warn(`Refusing to post an authorization answer to an unrecognised target: ${JSON.stringify(replyTo)}`);
      return { kind: 'failed', reason: 'unrecognised reply target' };
    }

    try {
      const accessToken = await this.userAuthService.getTokenForAudience(userId, projectId, this.audience);
      if (!accessToken) {
        logger.warn(
          `Cannot resume scheduled run: no console-backend token for user ${userId} (audience ${this.audience})`
        );
        return { kind: 'failed', reason: 'no console-backend token' };
      }

      const url =
        `${this.consoleBackendUrl}/api/v1/scheduler/jobs/${replyTo.scheduled_job_id}` +
        `/runs/${replyTo.scheduled_job_run_id}/resume`;

      const response = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${accessToken}` },
        body: JSON.stringify({ decision }),
      });

      if (response.status === 409) {
        logger.info(`Scheduled run ${replyTo.scheduled_job_run_id} was no longer waiting for an answer`);
        return { kind: 'already-handled' };
      }

      if (!response.ok) {
        const details = await response.text().catch(() => '');
        logger.warn(
          `Resuming scheduled run ${replyTo.scheduled_job_run_id} failed: ${response.status} ${response.statusText}${details ? ` — ${details}` : ''}`
        );
        return { kind: 'failed', reason: `${response.status} ${response.statusText}` };
      }

      logger.info(
        `Authorization ${decision} delivered for job ${replyTo.scheduled_job_id} run ${replyTo.scheduled_job_run_id}`
      );
      return { kind: 'resumed' };
    } catch (error) {
      logger.error(error, `Failed to resume scheduled run ${replyTo.scheduled_job_run_id}: ${error}`);
      return { kind: 'failed', reason: String(error) };
    }
  }
}
