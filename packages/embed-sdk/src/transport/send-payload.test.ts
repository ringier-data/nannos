/**
 * Send serialization: what rides `metadata` on each payload shape. The page
 * context must reach BOTH shapes — a HITL resume re-runs the orchestrator from
 * the top, so dropping it there would blind the resumed run to the page.
 */
import { describe, expect, it } from 'vitest';
import type { NannosUIMessage } from './ai-types';
import { buildHitlResumePayload, buildNewTurnPayload, type SendContext } from './send-payload';

const USER_MESSAGE: NannosUIMessage = {
  id: 'u1',
  role: 'user',
  parts: [{ type: 'text', text: 'what is on this page?' }],
};

const BASE_CTX: SendContext = { sessionId: 's1' };

const PAGE = {
  key: '/campaigns/123',
  title: 'Campaign 123',
  entity: { type: 'Campaign', id: '123' },
  view: { tab: 'targetings' },
};

describe('pageContext on outgoing payloads', () => {
  it('rides metadata on a new turn, verbatim', () => {
    const payload = buildNewTurnPayload({
      messageId: 'm1',
      conversationId: 'c1',
      userMessage: USER_MESSAGE,
      ctx: { ...BASE_CTX, pageContext: PAGE },
    });
    expect(payload.metadata?.pageContext).toEqual(PAGE);
  });

  it('rides metadata on a HITL resume', () => {
    const payload = buildHitlResumePayload({
      messageId: 'm1',
      conversationId: 'c1',
      decisions: [{ type: 'approve', id: 't1' }],
      ctx: { ...BASE_CTX, pageContext: PAGE },
    });
    expect(payload.metadata?.pageContext).toEqual(PAGE);
  });

  it('is absent (not null/undefined-keyed) when the host publishes none', () => {
    const payload = buildNewTurnPayload({
      messageId: 'm1',
      conversationId: 'c1',
      userMessage: USER_MESSAGE,
      ctx: BASE_CTX,
    });
    expect(payload.metadata && 'pageContext' in payload.metadata).toBe(false);
  });
});
