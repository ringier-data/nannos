/**
 * Files the agent produces mid-turn become `file` parts as they arrive — not
 * only after a history reload. The same file is persisted several times over
 * (artifact, full message, terminal status); the thread gets it once.
 */
import { describe, expect, it } from 'vitest';
import { createDemuxState, demux } from './demux';
import { fileName } from './ai-types';
import type { AgentResponseData } from '../core/wire';

const FILE = { kind: 'file', file: { uri: 'https://s3/report.pdf?sig=1', mimeType: 'application/pdf', name: 'report.pdf' } };
const files = (chunks: unknown[]) => chunks.filter((c) => (c as { type: string }).type === 'file');

describe('demux: agent files', () => {
  it('emits a file part from an artifact, named from the wire', () => {
    const state = createDemuxState('t-');
    const result = demux(state, {
      kind: 'artifact-update',
      taskId: 'task-1',
      artifact: { parts: [FILE] },
    } as unknown as AgentResponseData);
    const [part] = files(result.chunks) as Array<{ url: string; mediaType: string; providerMetadata?: never }>;
    expect(part).toMatchObject({ type: 'file', url: FILE.file.uri, mediaType: 'application/pdf' });
    expect(fileName(part)).toBe('report.pdf');
  });

  it('emits the same file only once across the events that repeat it', () => {
    const state = createDemuxState('t-');
    const a = demux(state, { kind: 'artifact-update', taskId: 'task-1', artifact: { parts: [FILE, { kind: 'text', text: 'Here.' }] } } as unknown as AgentResponseData);
    const b = demux(state, { role: 'agent', parts: [{ kind: 'text', text: 'Here.' }, FILE] } as unknown as AgentResponseData);
    const c = demux(state, {
      kind: 'status-update',
      taskId: 'task-1',
      status: { state: 'TASK_STATE_COMPLETED', message: { role: 'ROLE_AGENT', parts: [{ kind: 'text', text: 'Here.' }, FILE] } },
    } as unknown as AgentResponseData);
    expect(files([...a.chunks, ...b.chunks, ...c.chunks])).toHaveLength(1);
  });

  it('falls back to the URL as the name when the wire carries none', () => {
    expect(fileName({ url: 'https://s3/x.bin' })).toBe('https://s3/x.bin');
    expect(fileName({ url: 'https://s3/x.bin', filename: 'x.bin' })).toBe('x.bin');
  });
});
