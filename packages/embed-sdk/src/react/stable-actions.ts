import type { ObjectAction } from '../core';

/**
 * Registration-time actions that always call the LATEST render's handlers.
 *
 * Hosts build `actions` inline (closures over component state), so their identity
 * changes every render; re-registering on identity would churn the manifest. The
 * registration keeps the names, labels and params it was made with (a change to those
 * re-registers — see `actionsSignature`) and forwards `run` through `current`.
 */
export function stableActions(
  current: { current: Record<string, ObjectAction> | undefined },
): Record<string, ObjectAction> | undefined {
  const snapshot = current.current;
  if (!snapshot || !Object.keys(snapshot).length) return undefined;
  return Object.fromEntries(
    Object.entries(snapshot).map(([name, action]) => [
      name,
      {
        label: action.label,
        ...(action.description ? { description: action.description } : {}),
        ...(action.params?.length ? { params: action.params } : {}),
        ...(action.requiresApproval ? { requiresApproval: true } : {}),
        run: (args: Record<string, unknown>) => {
          const latest = current.current?.[name];
          if (!latest) return { ok: false, detail: 'This action is no longer available on the page.' };
          return latest.run(args);
        },
      } satisfies ObjectAction,
    ]),
  );
}

/** What the manifest shows of a set of actions — the re-register trigger. */
export function actionsSignature(actions: Record<string, ObjectAction> | undefined): string {
  if (!actions) return '';
  return JSON.stringify(
    Object.entries(actions).map(([name, a]) => [name, a.label, a.description ?? '', a.params ?? [], !!a.requiresApproval]),
  );
}
