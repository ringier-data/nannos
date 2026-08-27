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

/** Compact relative age ("3m", "3h", "2d") — row metadata, not prose. */
export function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (seconds < 60) return 'now';
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d`;
  const weeks = Math.floor(days / 7);
  if (weeks < 5) return `${weeks}w`;
  const months = Math.floor(days / 30.44);
  if (months < 12) return `${months}mo`;
  return `${Math.floor(days / 365.25)}y`;
}
