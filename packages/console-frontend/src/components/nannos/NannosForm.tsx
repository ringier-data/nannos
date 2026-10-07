import { useCallback, useEffect, useRef } from 'react';
import {
  useStateFieldsAdapter,
  type FormLike,
  type ObjectAction,
  type RouteId,
  type StateField,
  type SubmitOutcome,
} from '@nannos/embed-sdk';
import type { consoleObjectTypes } from './consoleObjects';
import { useConsoleForm } from './useConsoleForm';

type NannosFormProps = {
  /** Registered object type (a key of `consoleObjectTypes`). */
  type: keyof typeof consoleObjectTypes & string;
  /** The object's id; absent while creating. */
  id: RouteId;
  parentId?: RouteId;
  /**
   * The form's own Save, for the assistant's `submit` (the user approves it first).
   * Run exactly what the Save button runs; resolve `false` or `{ ok: false, detail }`
   * when nothing was saved (validation refused, the request failed), so the assistant
   * is told the truth. Leave it out where saving needs a decision only the user can
   * make (a confirmation, a change summary they must write).
   */
  submit?: () => SubmitOutcome | Promise<SubmitOutcome>;
  /** What the user could click inside the form that the assistant may do too (open a
   *  sub-dialog, run a check) — `invoke` actions. Never something that saves. */
  actions?: Record<string, ObjectAction>;
  /** The form holds unsaved edits — what the user typed as well as the assistant's
   *  fills. Lets the assistant see them: it will not refresh over them or navigate
   *  away without asking. Pass the page's own dirty flag where it has one. */
  dirty?: boolean;
} & (
  | {
      /** One `[value, setter]` pair per agent-settable field (per-field `useState` forms). */
      fields: Record<string, StateField>;
      form?: never;
    }
  | {
      /** A `getValues`/`setValue` pair, e.g. `useObjectStateAdapter` over a single state object. */
      form: FormLike;
      fields?: never;
    }
);

/**
 * Registers a console form as a client object for as long as it is mounted —
 * so mount it exactly where the form is live: inside the `DialogContent` of a
 * create/edit dialog, or under the edit-mode condition of a page. A form the
 * user can't see (or can't change) must not be offered to the assistant.
 */
export function NannosForm(props: NannosFormProps) {
  const adapted = useStateFieldsAdapter(props.fields ?? {});
  const dirtyRef = useRef(props.dirty);
  useEffect(() => {
    dirtyRef.current = props.dirty;
  }, [props.dirty]);
  const tracksDirty = props.dirty !== undefined;
  const isDirty = useCallback(() => dirtyRef.current === true, []);
  useConsoleForm({
    form: props.form ?? adapted,
    type: props.type,
    id: props.id,
    parentId: props.parentId,
    submit: props.submit,
    actions: props.actions,
    ...(tracksDirty ? { isDirty } : {}),
  });
  return null;
}
