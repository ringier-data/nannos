import { describe, expect, it } from 'vitest';
import { decodeApproval, encodeApproval, type Decision } from './approval-codec';

const roundTrip = (d: Decision): Decision => {
  const { approved, reason } = encodeApproval(d);
  return decodeApproval(d.id ?? 'call-x', { approved, reason });
};

describe('approval codec round-trips', () => {
  it('plain approve', () => {
    expect(roundTrip({ id: 'c1', type: 'approve' })).toEqual({ id: 'c1', type: 'approve' });
    expect(encodeApproval({ type: 'approve' })).toEqual({ approved: true }); // no envelope needed
  });

  it('approve with bypass flags', () => {
    expect(roundTrip({ id: 'c1', type: 'approve', bypass: true, bypass_all: true, bypass_pattern: 'x*' }))
      .toEqual({ id: 'c1', type: 'approve', bypass: true, bypass_all: true, bypass_pattern: 'x*' });
  });

  it('plain reject and reject with message', () => {
    expect(roundTrip({ id: 'c1', type: 'reject' })).toEqual({ id: 'c1', type: 'reject' });
    expect(roundTrip({ id: 'c1', type: 'reject', message: 'not now' })).toEqual({
      id: 'c1',
      type: 'reject',
      message: 'not now',
    });
  });

  it('edit (request changes) keeps its message', () => {
    expect(roundTrip({ id: 'c1', type: 'edit', message: 'only the drafts' })).toEqual({
      id: 'c1',
      type: 'edit',
      message: 'only the drafts',
    });
  });
});

describe('degrade semantics (never a dropped decision)', () => {
  it('a plain human string in reason becomes the decision message', () => {
    expect(decodeApproval('c1', { approved: false, reason: 'just no' })).toEqual({
      id: 'c1',
      type: 'reject',
      message: 'just no',
    });
  });

  it('malformed / wrong-version JSON degrades to approve/reject with the raw reason as message', () => {
    expect(decodeApproval('c1', { approved: false, reason: '{"v":99,"type":"edit"}' })).toEqual({
      id: 'c1',
      type: 'reject',
      message: '{"v":99,"type":"edit"}',
    });
    expect(decodeApproval('c1', { approved: true, reason: '{broken' })).toEqual({
      id: 'c1',
      type: 'approve',
      message: '{broken',
    });
  });

  it('no reason at all is the plain decision', () => {
    expect(decodeApproval('c1', { approved: true })).toEqual({ id: 'c1', type: 'approve' });
    expect(decodeApproval('c1', { approved: false })).toEqual({ id: 'c1', type: 'reject' });
  });
});
