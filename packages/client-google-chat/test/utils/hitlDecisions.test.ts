/**
 * Approval cards on Google Chat, mirroring client-slack: a card hides the server's own
 * args, names the calls it answers, and is settled when the user answers it in words
 * instead of clicking. Without that, a typed answer left the card's buttons live and a
 * later click ran as an empty turn (or answered a newer card).
 */
import { describe, test, expect, jest } from '@jest/globals';
import { GoogleChatService } from '../../src/services/googleChatService.js';
import { Config } from '../../src/config/config.js';
import { callIdsOf, forCalls, typedDecisionText } from '../../src/utils/hitlDecisions.js';
import { settleTypedDecisions } from '../../src/handlers/messageHandler.js';
import type { IHitlCardStore } from '../../src/storage/types.js';
import { Logger } from '../../src/utils/logger.js';

const config = { baseUrl: 'https://chat.nannos.example' } as Config;
const service = Object.create(GoogleChatService.prototype) as GoogleChatService;

const draft = {
  name: 'gmail_create_draft',
  args: {
    _call_id: 'gmail_create_draft:78c5@437f:0',
    _summary: 'Create a draft email to you with the subject "QA-A2".',
    _risk_metadata: { source: 'risk_score', score: 1, threshold: 0.8 },
    subject: 'QA-A2',
  },
};

function widgetTexts(card: any): string {
  return card.card.sections[0].widgets
    .map((w: any) => w.textParagraph?.text)
    .filter(Boolean)
    .join('\n');
}

function buttonParams(card: any, action: string): any {
  const buttons = card.card.sections[0].widgets.find((w: any) => w.buttonList)?.buttonList.buttons ?? [];
  const btn = buttons.find((b: any) =>
    b.onClick?.action?.parameters?.some((p: any) => p.key === 'action' && p.value === action)
  );
  return JSON.parse(btn.onClick.action.parameters.find((p: any) => p.key === 'parameters').value);
}

describe('approval card display', () => {
  test('server-added args are not shown as arguments; the summary is shown as text', () => {
    const card = service.buildHitlCard(config, 'gmail_create_draft', 'risky', { taskId: 't' }, undefined, [draft]);
    const text = widgetTexts(card);
    expect(text).toContain('<b>subject:</b> QA-A2');
    expect(text).not.toContain('_call_id');
    expect(text).not.toContain('_summary');
    expect(text).toContain('Create a draft email to you');
  });

  test('the multi-call card hides them too and falls back to the summary for its reason', () => {
    const second = { ...draft, args: { ...draft.args, _call_id: 'gmail_create_draft:99@1:0' } };
    const card = service.buildMultiHitlCard(config, { taskId: 't' }, [draft, second]);
    const text = widgetTexts(card);
    expect(text).not.toContain('_call_id');
    expect(text).toContain('Create a draft email to you');
  });
});

describe('call ids on the buttons', () => {
  test('every single-card button names the calls it answers', () => {
    const card = service.buildHitlCard(config, 'gmail_create_draft', 'risky', { taskId: 't' }, undefined, [draft]);
    for (const action of ['approve', 'reject', 'approve_bypass_tool']) {
      expect(buttonParams(card, action).callIds).toEqual(['gmail_create_draft:78c5@437f:0']);
    }
    expect(buttonParams(card, 'approve').taskId).toBe('t');
  });

  test('the multi-call card carries them for Approve all / Reject all', () => {
    const second = { ...draft, args: { ...draft.args, _call_id: 'gmail_create_draft:99@1:0' } };
    const card = service.buildMultiHitlCard(config, { taskId: 't' }, [draft, second]);
    expect(buttonParams(card, 'approve').callIds).toEqual(['gmail_create_draft:78c5@437f:0', 'gmail_create_draft:99@1:0']);
  });

  test('callIdsOf skips calls without an id', () => {
    expect(callIdsOf([draft, { name: 'x', args: {} }, undefined])).toEqual(['gmail_create_draft:78c5@437f:0']);
    expect(callIdsOf(undefined)).toEqual([]);
  });

  test('forCalls sends one decision per call, or the bare decision for an old card', () => {
    expect(forCalls({ type: 'approve' }, ['a', 'b'])).toEqual([
      { type: 'approve', id: 'a' },
      { type: 'approve', id: 'b' },
    ]);
    expect(forCalls({ type: 'reject' }, undefined)).toEqual([{ type: 'reject' }]);
  });
});

describe('settleTypedDecisions', () => {
  const logger = Logger.getLogger('test');

  function fakes(cards: string[]) {
    const store = {
      set: jest.fn(async () => undefined),
      take: jest.fn(async () => cards),
    } as unknown as IHitlCardStore & { take: jest.Mock };
    const chat = { updateMessage: jest.fn(async () => ({})) } as unknown as GoogleChatService & {
      updateMessage: jest.Mock;
    };
    return { store, chat };
  }

  test('settles the card the words decided, by its stored message name', async () => {
    const { store, chat } = fakes(['spaces/S/messages/M1']);
    await settleTypedDecisions(chat, store, 'P', [{ id: 'c-1', type: 'reject', intent: 'reject' }], logger);

    expect(store.take).toHaveBeenCalledWith('P', ['c-1']);
    expect(chat.updateMessage).toHaveBeenCalledWith({
      projectId: 'P',
      messageName: 'spaces/S/messages/M1',
      text: '❌ Rejected in your reply',
      cardsV2: [],
    });
  });

  test('a decision without ids settles nothing; a failing store never throws', async () => {
    const { store, chat } = fakes(['spaces/S/messages/M1']);
    await settleTypedDecisions(chat, store, 'P', [{ type: 'approve' }], logger);
    expect(store.take).not.toHaveBeenCalled();

    const broken = { set: jest.fn(), take: jest.fn(async () => Promise.reject(new Error('db down'))) } as unknown as IHitlCardStore;
    await expect(
      settleTypedDecisions(chat, broken, 'P', [{ id: 'c-1', type: 'reject' }], logger)
    ).resolves.toBeUndefined();
  });

  test('texts say what the words did; the gate type wins over the intent', () => {
    expect(typedDecisionText({ type: 'approve', intent: 'approve' })).toBe('✅ Approved in your reply');
    expect(typedDecisionText({ type: 'reject', intent: 'change' })).toContain('Changes requested');
    expect(typedDecisionText({ type: 'reject', intent: 'none' })).toContain('Not run');
    // A typed yes to a save reads approve but did not run.
    expect(typedDecisionText({ type: 'reject', intent: 'approve' })).toContain('Not run');
  });
});
