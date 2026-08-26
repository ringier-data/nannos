/**
 * `useNannosPageContext` — declare a LAYER of the current page/context to the
 * assistant for as long as the calling component is mounted. Layers are merged
 * in mount order (later wins, `view` merges key by key) and sanitized before
 * anything is shown or sent — see core/page-context.ts.
 *
 * The host's router bridge publishes the base (`setPageContext`: key + title);
 * a page, tab or dialog layers what only it knows on top:
 *
 *   // the campaign details page:
 *   useNannosPageContext({ entity: { type: 'Campaign', id, name } });
 *   // a tab inside it:
 *   useNannosPageContext({ view: { tab: 'targetings' } });
 *
 * Passing null/undefined declares nothing (and removes this component's layer).
 * Outside the provider it is a no-op, like the rest of `useAssistant`.
 */
import { useEffect, useRef } from 'react';
import type { NannosPageContext } from '../core';
import { useAssistant, type PageContextLayerHandle } from './provider';

export function useNannosPageContext(context: NannosPageContext | null | undefined): void {
  const { registerPageContextLayer } = useAssistant();
  const handleRef = useRef<PageContextLayerHandle | null>(null);
  const registrarRef = useRef(registerPageContextLayer);

  // Re-publish on VALUE change, not identity — callers build the object inline
  // each render. The latest object rides a ref so the effect never publishes a
  // stale one when only its serialization triggered.
  const latest = useRef(context);
  latest.current = context;
  const serialized = context ? JSON.stringify(context) : null;

  useEffect(() => {
    // A handle from a REMOUNTED provider's predecessor is dead — drop it and
    // register afresh (rare: the registrar is stable across renders).
    if (registrarRef.current !== registerPageContextLayer) {
      handleRef.current?.dispose();
      handleRef.current = null;
      registrarRef.current = registerPageContextLayer;
    }
    const value = latest.current;
    if (!value) {
      handleRef.current?.dispose();
      handleRef.current = null;
      return;
    }
    if (handleRef.current) {
      handleRef.current.update(value);
    } else {
      handleRef.current = registerPageContextLayer(value);
    }
  }, [registerPageContextLayer, serialized]);

  // Removed on unmount only — value changes update the layer in place above,
  // keeping its position in the stack.
  useEffect(
    () => () => {
      handleRef.current?.dispose();
      handleRef.current = null;
    },
    [],
  );
}
