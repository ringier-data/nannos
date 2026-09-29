/**
 * Tells console-backend that a scheduled run's notification reached nobody (#191).
 *
 * The webhook acknowledges a push before it looks the recipient up, so the scheduler
 * would otherwise record the run as delivered. This is a separate call, made as this
 * client itself (client credentials, the same identity that registered the delivery
 * channel), after the drop. Best effort: a report that fails is logged and dropped,
 * since there is no one left to tell.
 */

import { Logger } from '../utils/logger.js';

const logger = Logger.getLogger('deliveryReport');

/**
 * `no_recipient`: this installation holds no sign-in for the subscriber, which only they
 * can fix, so the scheduler holds the job until they sign in here. `send_failed`: a
 * recipient was found but posting failed; the run is marked, the job keeps running.
 */
export type UndeliveredReason = 'no_recipient' | 'send_failed';

export interface UndeliveredReport {
  runId: number;
  /** The installation that received the push, as its delivery channel is registered. */
  installationId: string;
  reason: UndeliveredReason;
  detail?: string;
}

export type DeliveryReporter = (report: UndeliveredReport) => Promise<void>;

export interface DeliveryReporterOptions {
  /** console-backend's base URL for this client's own calls. */
  baseUrl: string;
  /** The audience of this client's client-credentials token (console-backend's client). */
  audience: string;
  getServiceToken: (audience: string) => Promise<string>;
}

export function createDeliveryReporter(options: DeliveryReporterOptions): DeliveryReporter {
  return async (report) => {
    try {
      const token = await options.getServiceToken(options.audience);
      const response = await fetch(`${options.baseUrl.replace(/\/+$/, '')}/api/v1/delivery-channels/undelivered`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${token}` },
        body: JSON.stringify({
          run_id: report.runId,
          installation_id: report.installationId,
          reason: report.reason,
          // Uncut: the backend cuts it, and refusing it over length would lose the report.
          ...(report.detail ? { detail: report.detail } : {}),
        }),
      });
      if (!response.ok) {
        const text = await response.text().catch(() => '');
        logger.error(`Delivery report for run ${report.runId} refused: ${response.status} ${text}`);
      }
    } catch (error) {
      logger.error(error, `Could not report run ${report.runId} as undelivered: ${error}`);
    }
  };
}
