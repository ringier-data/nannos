/**
 * `useNannosPageReader` — answer the agent's `read_current_page` pull for as
 * long as the calling component is mounted. The push half (`useNannosPageContext`)
 * carries a small snapshot with EVERY send; a reader is the on-demand
 * complement for what a fixed shape cannot carry — the rows on screen, the
 * active filters, an unsaved form:
 *
 *   useNannosPageReader('lineItems', () => rows.map(({ id, name, state }) => ({ id, name, state })));
 *   useNannosPageReader('unsavedForm', () => form.getValues());
 *
 * `key` names the field in the agent's answer (one reader per key; a remount
 * replaces its predecessor). The answer is sanitized before it leaves the
 * browser — same secret deny list as the page context, at every depth, plus
 * size caps (core/page-read.ts) — but declare NAMED slices rather than handing
 * over whole fetched objects. Outside the provider it is a no-op.
 */
import { useEffect, useRef } from 'react';
import type { NannosPageReader } from '../core';
import { useAssistant } from './provider';

export function useNannosPageReader(key: string, reader: NannosPageReader): void {
  const { registerPageReader } = useAssistant();

  // Callers pass inline closures — keep the LATEST via a ref so registration
  // survives re-renders without churning the registry.
  const latest = useRef(reader);
  latest.current = reader;

  useEffect(
    () => registerPageReader(key, () => latest.current()),
    [registerPageReader, key],
  );
}
