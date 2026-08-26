// @vitest-environment happy-dom
/**
 * Apply mode: which form writes the panel may answer for the user. The default
 * has to be the cautious one, the choice has to survive a reload, and a host
 * that fixed the mode must not be overridable from the panel.
 */
import { act, render } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import {
  ApplyModeProvider,
  readStoredApplyMode,
  useApplyModeControls,
  type ApplyModeValue,
} from './apply-mode';
import type { ApplyMode } from './apply-mode';

function mount(mode?: ApplyMode) {
  let value: ApplyModeValue | null = null;
  function Probe() {
    value = useApplyModeControls();
    return null;
  }
  render(
    <ApplyModeProvider mode={mode}>
      <Probe />
    </ApplyModeProvider>,
  );
  return () => value!;
}

beforeEach(() => {
  localStorage.clear();
});

describe('ApplyModeProvider', () => {
  it('starts manual — the assistant asks until told otherwise', () => {
    const get = mount();
    expect(get().mode).toBe('manual');
    expect(get().locked).toBe(false);
  });

  it('remembers the viewer’s choice in this browser', () => {
    const get = mount();
    act(() => get().setMode('allow-edits'));
    expect(get().mode).toBe('allow-edits');
    expect(readStoredApplyMode()).toBe('allow-edits');

    // A fresh mount (reload) reads it back.
    expect(mount()().mode).toBe('allow-edits');
  });

  it('a host-set mode wins and reports itself locked', () => {
    localStorage.setItem('nannos:applyMode', 'allow-edits');
    const get = mount('manual');
    expect(get().mode).toBe('manual');
    // `locked` is what hides the header control: a fixed mode must not look
    // adjustable.
    expect(get().locked).toBe(true);
  });

  it('an unreadable store still answers manual', () => {
    const original = localStorage.getItem;
    localStorage.getItem = () => {
      throw new Error('site data blocked');
    };
    try {
      expect(readStoredApplyMode()).toBe('manual');
    } finally {
      localStorage.getItem = original;
    }
  });

  it('a junk stored value is not allow-edits', () => {
    localStorage.setItem('nannos:applyMode', 'yolo');
    expect(readStoredApplyMode()).toBe('manual');
  });
});
