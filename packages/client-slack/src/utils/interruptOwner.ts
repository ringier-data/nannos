import type { WebClient } from '@slack/web-api';

/**
 * An interrupt card (tool approval, in-task authorization) belongs to the
 * speaker whose turn raised it. In a channel anyone can click it, so the card
 * carries its owner's Slack user id and every button checks it first.
 *
 * The orchestrator refuses a foreign answer on its own; this check is what keeps
 * a stranger's click from stripping the owner's buttons ("Approved") before that
 * refusal arrives. A card posted before owners were recorded carries none and
 * behaves as it always did.
 */
export async function refuseForeignClick(
  client: WebClient,
  decoded: { ownerUserId?: unknown },
  clickerId: string,
  channelId: string,
  threadTs?: string,
): Promise<boolean> {
  const owner = typeof decoded.ownerUserId === 'string' ? decoded.ownerUserId : '';
  if (!owner || owner === clickerId) return false;
  await client.chat.postEphemeral({
    channel: channelId,
    user: clickerId,
    ...(threadTs ? { thread_ts: threadTs } : {}),
    text: `Only <@${owner}> can answer this. Mention me outside this thread to start your own conversation.`,
  });
  return true;
}
