import { describe, expect, it } from 'vitest';
import {
  extractPartTexts,
  getFileInfo,
  getPartKind,
  getTaskState,
  isTaskComplete,
  shouldDisplayMessageParts,
  shouldShowTaskProgress,
} from './protocol';

describe('getTaskState', () => {
  it('maps A2A v1.0 protobuf enum names to short wire strings', () => {
    expect(getTaskState('TASK_STATE_COMPLETED')).toBe('completed');
    expect(getTaskState('TASK_STATE_INPUT_REQUIRED')).toBe('input-required');
    expect(getTaskState({ state: 'TASK_STATE_WORKING' })).toBe('working');
  });

  it('passes v0.3 short names through unchanged', () => {
    expect(getTaskState('completed')).toBe('completed');
    expect(getTaskState({ state: 'input-required' })).toBe('input-required');
  });

  it('falls back to unknown', () => {
    expect(getTaskState(undefined)).toBe('unknown');
    expect(getTaskState({})).toBe('unknown');
  });
});

describe('task status predicates', () => {
  it('isTaskComplete covers terminal states, case-insensitively', () => {
    for (const s of ['completed', 'FAILED', 'succeeded', 'cancelled']) expect(isTaskComplete(s)).toBe(true);
    expect(isTaskComplete('running')).toBe(false);
    expect(isTaskComplete(null)).toBe(false);
  });

  it('shouldShowTaskProgress only for running/in_progress', () => {
    expect(shouldShowTaskProgress('running')).toBe(true);
    expect(shouldShowTaskProgress('in_progress')).toBe(true);
    expect(shouldShowTaskProgress('completed')).toBe(false);
  });
});

describe('part shape normalization', () => {
  it('getPartKind handles v0.3 kind discriminator and v1.0 flat fields', () => {
    expect(getPartKind({ kind: 'text', text: 'x' })).toBe('text');
    expect(getPartKind({ text: 'x' })).toBe('text');
    expect(getPartKind({ data: {} })).toBe('data');
    expect(getPartKind({ url: 'https://x' })).toBe('file');
    expect(getPartKind('nope')).toBeUndefined();
  });

  it('getFileInfo normalizes both file shapes', () => {
    expect(getFileInfo({ file: { uri: 'u', mimeType: 'm', name: 'n' } })).toEqual({ uri: 'u', mimeType: 'm', name: 'n' });
    expect(getFileInfo({ url: 'u', mediaType: 'm', filename: 'n' })).toEqual({ uri: 'u', mimeType: 'm', name: 'n' });
    expect(getFileInfo({})).toBeNull();
  });

  it('extractPartTexts handles root-wrapped, legacy, and plain-string parts', () => {
    expect(extractPartTexts([{ root: { text: 'a' } }, { text: 'b' }, 'c', { other: 1 } as never])).toEqual([
      'a',
      'b',
      'c',
      '',
    ]);
    expect(extractPartTexts(undefined)).toEqual([]);
  });

  it('shouldDisplayMessageParts requires at least one non-empty text', () => {
    expect(shouldDisplayMessageParts([{ text: '  ' }, { text: '' }])).toBe(false);
    expect(shouldDisplayMessageParts([{ text: 'hello' }])).toBe(true);
    expect(shouldDisplayMessageParts([])).toBe(false);
  });
});
