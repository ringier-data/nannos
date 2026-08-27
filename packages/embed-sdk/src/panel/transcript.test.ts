import { describe, expect, it } from 'vitest';
import type { NannosUIMessage } from '../transport';
import { formatTranscript, messagePlainText, slugifyFilename } from './transcript';

const LABELS = { user: 'User', assistant: 'Assistant', truncated: 'may be incomplete' };

const MESSAGES: NannosUIMessage[] = [
  {
    id: 'u1',
    role: 'user',
    parts: [{ type: 'text', text: 'what is that?' }],
    metadata: { attachments: [{ uri: 'https://x/a.png', mimeType: 'image/png', name: 'sheet.png' }] },
  },
  {
    id: 'a1',
    role: 'assistant',
    parts: [
      { type: 'data-activity', id: 'x', data: { text: 'Delegating…' } },
      { type: 'text', text: 'A 2x2 grid of' },
      { type: 'text', text: 'cartoon gymnasts.' },
    ] as NannosUIMessage['parts'],
  },
  { id: 'u2', role: 'user', parts: [] }, // a HITL resume row: nothing readable
];

describe('transcript', () => {
  it('reads only the text parts of a message', () => {
    expect(messagePlainText(MESSAGES[1])).toBe('A 2x2 grid of\ncartoon gymnasts.');
  });

  it('writes speaker blocks, names files, skips empty messages', () => {
    const text = formatTranscript('Gymnast sheet', MESSAGES, { labels: LABELS });
    expect(text).toBe(
      [
        'Gymnast sheet',
        '=============',
        '',
        'User:',
        'what is that?',
        '[sheet.png]',
        '',
        'Assistant:',
        'A 2x2 grid of\ncartoon gymnasts.',
        '',
      ].join('\n'),
    );
  });

  it('flags a truncated export', () => {
    const text = formatTranscript('T', MESSAGES, { truncated: true, labels: LABELS });
    expect(text.split('\n')[3]).toBe('⚠ may be incomplete');
  });

  it('slugifies titles', () => {
    expect(slugifyFilename('  Marleen SF: Aktualisierung! ')).toBe('marleen-sf-aktualisierung');
    expect(slugifyFilename('???')).toBe('conversation');
  });
});
