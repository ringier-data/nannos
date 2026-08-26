/**
 * How a conversation row READS — the small decisions shared by every surface
 * that shows a conversation: the sidebar list, the history overlay, and the
 * thread's "continue where you left off" card. They live here so the three
 * never drift apart.
 */
import type { ConversationMeta } from '../transport';

/**
 * Where the conversation started, in one short label: the entity if the host
 * named one ('Campaign 42'), else the page title, else its route key. This is
 * the conversation's ORIGIN, fixed for its life — not where the user is now.
 */
export function originLabel(origin: ConversationMeta['origin']): string | undefined {
  if (!origin) return undefined;
  const entity = origin.entity;
  if (entity) return entity.name ? `${entity.type} ${entity.name}` : `${entity.type} ${entity.id}`;
  return origin.title ?? origin.key;
}

/**
 * A conversation nothing has happened in yet: no name, no preview, no summary.
 * Its row would read as a bare "New conversation" with nothing under it — which
 * is the fresh chat the panel is already sitting on, so the list hides it. It
 * appears as soon as the first message names it.
 */
export function isUntouched(conversation: ConversationMeta): boolean {
  return !conversation.title && !conversation.summary && !conversation.lastMessage;
}

/**
 * What the conversation is ABOUT, in one line: the backend's summary is the
 * durable answer; the streamed last message is what we have until it lands
 * (and on rows written before the feature).
 */
export function previewLine(conversation: ConversationMeta): string {
  return conversation.summary || conversation.lastMessage;
}

/** Locale-aware relative time ("2 hours ago") without hardcoded chrome strings. */
export function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: 'auto' });
  let value = Math.round((then - Date.now()) / 1000);
  const divisions: Array<[number, Intl.RelativeTimeFormatUnit]> = [
    [60, 'second'],
    [60, 'minute'],
    [24, 'hour'],
    [7, 'day'],
    [4.34524, 'week'],
    [12, 'month'],
  ];
  for (const [amount, unit] of divisions) {
    if (Math.abs(value) < amount) return rtf.format(value, unit);
    value = Math.round(value / amount);
  }
  return rtf.format(value, 'year');
}
