import { describe, expect, it } from 'vitest';
import { codePointLength, sliceFromCodePoint } from './offsets';

describe('code-point offsets (server counts Python len, JS counts UTF-16)', () => {
  it('codePointLength counts astral characters as ONE', () => {
    expect(codePointLength('👋👋')).toBe(2); // .length would say 4
    expect(codePointLength('a👋b')).toBe(3);
    expect(codePointLength('')).toBe(0);
  });

  it('sliceFromCodePoint slices by code points, not UTF-16 units', () => {
    expect(sliceFromCodePoint('👋👋🌍!', 2)).toBe('🌍!');
    expect(sliceFromCodePoint('a👋b', 2)).toBe('b');
    expect(sliceFromCodePoint('plain', 2)).toBe('ain');
  });

  it('boundary cases: zero, exact end, past end', () => {
    expect(sliceFromCodePoint('👋x', 0)).toBe('👋x');
    expect(sliceFromCodePoint('👋x', 2)).toBe('');
    expect(sliceFromCodePoint('👋x', 99)).toBe('');
  });

  it('combining marks stay intact (they are separate code points, like Python)', () => {
    const flag = '🇨🇭'; // two regional indicators = 2 code points
    expect(codePointLength(flag)).toBe(2);
    expect(sliceFromCodePoint(`${flag}ok`, 2)).toBe('ok');
  });
});
