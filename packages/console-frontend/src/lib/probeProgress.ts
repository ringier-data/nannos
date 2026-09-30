/**
 * State of a model Test's progress stream (nannos#318), folded from the NDJSON events the Test
 * endpoint sends. Rendered by components/admin/ProbeProgress.
 */
import type { ProbeEvent } from '@/api/model-gateway';

export type ShapeStatus = 'pending' | 'running' | 'ok' | 'limited' | 'failed' | 'inconclusive';

export interface ShapeRow {
  shape: string;
  label: string;
  status: ShapeStatus;
  /** The request in flight while running ("trying thinking: disabled"). */
  step?: string;
  /** The provider's reason or the probe's note once the verdict is in. */
  detail?: string;
}

export interface ProbeState {
  model: string;
  rows: ShapeRow[];
  /** No plan arrived: an embedding model, tested with a single ping. */
  ping: boolean;
  finished: boolean;
  error: string | null;
}

export function initialProbeState(model: string): ProbeState {
  return { model, rows: [], ping: true, finished: false, error: null };
}

export function reduceProbe(state: ProbeState, event: ProbeEvent): ProbeState {
  switch (event.type) {
    case 'plan':
      return {
        ...state,
        ping: false,
        rows: event.shapes.map((s) => ({ shape: s.shape, label: s.label, status: 'pending' })),
      };
    case 'step':
      return {
        ...state,
        rows: state.rows.map((r) =>
          r.shape === event.shape
            ? { ...r, status: 'running', step: event.step === event.shape ? undefined : event.label }
            : r,
        ),
      };
    case 'result': {
      const status: ShapeStatus = event.ok
        ? 'ok'
        : event.inconclusive
          ? 'inconclusive'
          : event.unavoidable
            ? 'failed'
            : 'limited';
      return {
        ...state,
        rows: state.rows.map((r) =>
          r.shape === event.shape
            ? { ...r, status, step: undefined, detail: (event.ok ? event.note : event.error) || undefined }
            : r,
        ),
      };
    }
    case 'done':
      return { ...state, finished: true };
    case 'error':
      return {
        ...state,
        finished: true,
        error: event.detail,
        // A shape still marked running when the verdict came is where it stopped.
        rows: state.rows.map((r) => (r.status === 'running' ? { ...r, status: 'failed', step: undefined } : r)),
      };
  }
}
