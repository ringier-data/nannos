/**
 * The authoring stage for a watch's CEL expression.
 *
 * A bare textarea was the wrong tool for the hardest field in the form: no brackets,
 * no highlighting, and nothing to help someone who has never written CEL. This stage
 * gives two ways in: describe it (a model writes the expression against the real
 * payload and the backend verifies it by actually evaluating it), or write it
 * directly in a real editor — with the payload's top-level keys one click away.
 *
 * CEL is close enough to JavaScript that its grammar highlights correctly; what JS
 * highlighting gets wrong about CEL (nothing an expression this size hits) is a fair
 * trade for not shipping a custom language mode.
 */
import { useState } from 'react';
import CodeMirror, { EditorView } from '@uiw/react-codemirror';
import { javascript } from '@codemirror/lang-javascript';
import { AlertCircle, Check, Loader2, Sparkles, WrapText } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { generateCondition } from '@/api/scheduler';
import { formatCel } from '@/lib/watchCondition';
import { cn } from '@/lib/utils';

/** The bare CEL editor, shared with smaller inputs (dynamic arguments). */
export function CelCodeMirror({
  value,
  onChange,
  placeholder,
  minHeight = '96px',
  invalid,
}: {
  value: string;
  onChange: (next: string) => void;
  placeholder?: string;
  minHeight?: string;
  invalid?: boolean;
}) {
  return (
    <div className={cn('overflow-hidden rounded-md border', invalid && 'border-destructive')}>
      <CodeMirror
        value={value}
        minHeight={minHeight}
        placeholder={placeholder}
        basicSetup={{
          // The gutter is half the point: numbered lines read as code, and wrapped
          // continuations hang visibly off their line instead of blending together.
          lineNumbers: true,
          foldGutter: false,
          highlightActiveLine: false,
          highlightActiveLineGutter: false,
          autocompletion: false,
        }}
        // Long expressions wrap instead of disappearing behind a horizontal
        // scrollbar — the reason a one-line condition was unreadable here.
        extensions={[javascript(), EditorView.lineWrapping]}
        onChange={onChange}
        className="text-[13px] leading-relaxed [&_.cm-content]:py-2 [&_.cm-editor]:bg-transparent [&_.cm-editor.cm-focused]:outline-none [&_.cm-gutters]:border-r-0 [&_.cm-gutters]:bg-transparent [&_.cm-line]:pr-3 [&_.cm-line]:pl-2"
      />
    </div>
  );
}

export function CelExpressionEditor({
  value,
  llmCondition,
  onChange,
  /** The payload to write against — enables key chips and lets the AI verify. */
  payload,
  checkTool,
  invalid,
}: {
  value: string;
  llmCondition: string;
  /** Patches both halves: the AI may propose a semantic stage alongside the expression. */
  onChange: (patch: { cel_expr?: string; llm_condition?: string }) => void;
  payload?: Record<string, unknown>;
  checkTool?: string;
  invalid?: boolean;
}) {
  const [aiQuery, setAiQuery] = useState('');
  const [aiBusy, setAiBusy] = useState(false);
  const [aiNotes, setAiNotes] = useState<string[]>([]);
  const [aiError, setAiError] = useState<string | null>(null);
  const [aiVerified, setAiVerified] = useState<boolean | null>(null);

  const payloadKeys = payload ? Object.keys(payload).slice(0, 8) : [];

  async function runAi() {
    if (!aiQuery.trim() || aiBusy) return;
    setAiBusy(true);
    setAiError(null);
    setAiNotes([]);
    setAiVerified(null);
    try {
      const generated = await generateCondition({
        query: aiQuery.trim(),
        current_cel_expr: value.trim() || null,
        current_llm_condition: llmCondition.trim() || null,
        result: payload,
        check_tool: checkTool || null,
      });
      const patch: { cel_expr?: string; llm_condition?: string } = {};
      // Generated expressions arrive as one long line; break them on their logical
      // seams before they land in the editor.
      if (generated.cel_expr != null) patch.cel_expr = formatCel(generated.cel_expr);
      if (generated.llm_condition) patch.llm_condition = generated.llm_condition;
      onChange(patch);
      setAiNotes(generated.notes ?? []);
      setAiVerified(generated.verified);
      setAiQuery('');
    } catch (e) {
      setAiError(e instanceof Error ? e.message : String(e));
    } finally {
      setAiBusy(false);
    }
  }

  return (
    <div className="grid gap-2">
      {/* Describe-it entry point above the editor it writes into, same reasoning as
          the job-level AI fill: a box that writes into a field is read before it. */}
      <div className="flex gap-2">
        <Input
          className="flex-1 text-xs"
          placeholder={
            value.trim()
              ? 'Refine it: e.g. also ignore attendees who declined'
              : 'Describe it: e.g. a meeting starts within the hour and has outside attendees'
          }
          value={aiQuery}
          onChange={(e) => setAiQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') {
              e.preventDefault();
              void runAi();
            }
          }}
        />
        <Button type="button" variant="outline" size="sm" disabled={aiBusy || !aiQuery.trim()} onClick={() => void runAi()}>
          {aiBusy ? <Loader2 className="size-3.5 animate-spin" /> : <Sparkles className="size-3.5" />}
          {value.trim() ? 'Refine' : 'Generate'}
        </Button>
      </div>
      {aiError && (
        <span className="text-destructive flex items-start gap-1.5 text-xs">
          <AlertCircle className="mt-0.5 size-3.5 shrink-0" />
          {aiError}
        </span>
      )}
      {aiVerified !== null &&
        (aiVerified && payload ? (
          <span className="flex items-center gap-1.5 text-xs text-green-700 dark:text-green-400">
            <Check className="size-3.5" />
            Verified against the real response — see the tester below.
          </span>
        ) : null)}
      {aiNotes.map((note) => (
        <span key={note} className="text-muted-foreground flex items-start gap-1.5 text-xs">
          <AlertCircle className="mt-0.5 size-3.5 shrink-0" />
          {note}
        </span>
      ))}

      <CelCodeMirror
        value={value}
        invalid={invalid}
        placeholder={"result.events.filter(e, timestamp(e.start.dateTime) - now < duration('1h'))"}
        onChange={(next) => onChange({ cel_expr: next })}
      />

      <div className="flex flex-wrap items-center gap-1.5">
        {value.trim() && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            className="h-6 px-2 text-[11px]"
            onClick={() => onChange({ cel_expr: formatCel(value) })}
          >
            <WrapText className="size-3" />
            Format
          </Button>
        )}
        {payloadKeys.map((key) => (
          <Button
            key={`key-${key}`}
            type="button"
            variant="ghost"
            size="sm"
            className="text-muted-foreground h-6 px-2 font-mono text-[11px]"
            onClick={() => onChange({ cel_expr: value.trim() ? `${value}\nresult.${key}` : `result.${key}` })}
          >
            result.{key}
          </Button>
        ))}
      </div>
    </div>
  );
}
