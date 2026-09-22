/**
 * Reading a shared scheduled job's sharing state (ADR-0010).
 *
 * A job splits into a shareable definition and one subscription per subscriber, but the
 * REST view folds them into one flat job that is already relative to the viewer:
 * `user_id` IS the viewer, so "is this mine" is a comparison on the row itself and needs
 * no account lookup.
 */
import type { ScheduledJob } from '@/api/generated/types.gen';

/**
 * How many people run this job, this viewer included. Defaulted server-side, so the
 * generated type has it optional; a job that reports nothing is the viewer's alone.
 */
export function subscriberCount(job: Pick<ScheduledJob, 'subscriber_count'>): number {
  return job.subscriber_count ?? 1;
}

/** Whether the viewer owns what this job does, rather than just running it. */
export function isOwnJob(job: Pick<ScheduledJob, 'owner_user_id' | 'user_id'>): boolean {
  return job.owner_user_id === job.user_id;
}
