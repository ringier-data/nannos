/**
 * The fields that define a watch: what to check, when to trigger, and what happens then.
 *
 * Shared by the create dialog and the job detail page, because they ask the same
 * questions and drifted when they answered them separately — the detail page never got
 * the judge mode, the condition tester or the schema-driven arguments, so a judged watch
 * could not be edited there at all.
 *
 * `mode` is the difference between the two callers. In "read" it renders values as text
 * rather than disabled inputs: a greyed-out field reads as an empty placeholder and is
 * the lowest-contrast thing on the page, which is exactly wrong for the thing you came
 * to look at.
 *
 * The check runs from here in edit mode only. It is a real tool call with real side
 * effects, so it does not belong on a page whose default state is "just looking".
 */
import { useEffect, useMemo, useState } from 'react';
import { AlertCircle, ChevronDown } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { McpToolSelect } from '@/components/McpToolSelect';
import { McpToolArgsFields, McpToolArgsJson } from '@/components/McpToolArgsFields';
import { JsonPathPicker } from '@/components/JsonPathPicker';
import { CelExpressionEditor } from '@/components/CelExpressionEditor';
import { ConditionTester } from '@/components/ConditionTester';
import { AgentActionFields, type AgentAction } from '@/components/AgentActionFields';
import {
  SectionHeader,
  AiBadge,
  ChoiceLabel,
  FieldError,
  HintTip,
  OptionCard,
  ReadValue,
  Segmented,
} from '@/components/formChrome';
import { toolServer, toolShortName, parseToolSchema } from '@/lib/mcpTools';
import { type CheckCall, missingRequiredArgs, resolveArgs, sameCheckCall } from '@/lib/watchArgs';
import { jsonPathToCel } from '@/lib/watchCondition';
import { cn } from '@/lib/utils';
import { type ConditionMode, type MessageMode, type WatchChoices } from '@/lib/watchChoices';
import { McpToolRiskError, invokeMcpTool, validateArgsExpr } from '@/api/scheduler';
import type { McpTool } from '@/api/generated/types.gen';

/** Everything a watch job's fields read and write. A superset of the agent action. */
export interface WatchFieldsValue extends AgentAction, WatchChoices {
  check_tool: string;
  check_args: Record<string, unknown>;
  check_args_text: string;
  args_mode: 'fields' | 'json';
  /** Per-argument CEL expressions (`= …` values), resolved and merged on every run. */
  check_args_exprs: Record<string, string>;
  /** CEL expression: extracts the evidence and gates the trigger in one. Optional. */
  cel_expr: string;
  /** Judged by a model — alone over the whole response, or on what cel_expr returned. */
  llm_condition: string;
  destroy_after_trigger: boolean;
  outcome: 'notify' | 'agent';
  notification_message: string;
}

export function WatchFields({
  value,
  onChange,
  mode,
  mcpTools,
  subAgents,
  storedResult,
  storedCall,
  fieldErrors,
  aiFilled,
  onError,
  onLiveResult,
  sectionOffset = 2,
}: {
  value: WatchFieldsValue;
  onChange: (patch: Partial<WatchFieldsValue>) => void;
  mode: 'edit' | 'read';
  mcpTools: McpTool[];
  subAgents: { id: number; name: string; type?: string | null }[];
  /** A response from a previous real run, so a condition can be tested with no new call. */
  storedResult?: Record<string, unknown> | null;
  /**
   * The call that produced `storedResult` — the saved job's. While the form's call
   * differs, the stored response is not offered for testing (it stays `prev`, which is
   * what the next run binds). Omitted, the stored response is taken to match.
   */
  storedCall?: CheckCall;
  fieldErrors?: Record<string, string>;
  /** Field keys an AI fill wrote, marked so a generated value is not taken for a typed one. */
  aiFilled?: Set<string>;
  onError?: (message: string) => void;
  /**
   * This session's check response for the call as it now stands, or undefined once the
   * tool or arguments change. For a caller that sends the response elsewhere — the AI
   * edit — and must not send one this form has already stopped trusting.
   */
  onLiveResult?: (result: Record<string, unknown> | undefined) => void;
  /** Section numbers continue the caller's own numbering. */
  sectionOffset?: number;
}) {
  const patch = onChange;
  const errors = fieldErrors ?? {};
  const filled = aiFilled ?? new Set<string>();
  const editing = mode === 'edit';

  const [check, setCheck] = useState<{
    loading: boolean;
    result?: Record<string, unknown>;
    elapsedMs?: number;
    truncated?: boolean;
    isError?: boolean;
    error?: string;
    /** The call this result came from, so it can be discarded when it stops describing one. */
    signature?: string;
  }>({ loading: false });
  const [submitMissingArgs, setMissingArgs] = useState<Set<string>>(new Set());
  const [riskPrompt, setRiskPrompt] = useState<string | null>(null);
  // The full response is for seeding an expression by clicking a path. With one already
  // written it is reference, not the next step, so it arrives folded.
  const [showResponse, setShowResponse] = useState(false);
  const [argsPreview, setArgsPreview] = useState<{
    resolved?: Record<string, unknown>;
    error?: string;
  }>({});

  // Resolve the `= …` arguments as they are typed, so "what will the tool actually
  // be called with?" is answered here rather than on the first live run. Debounced,
  // like the condition tester, and keyed on the static args too — they are part of
  // the merged result.
  const argsExprsKey = JSON.stringify(value.check_args_exprs);
  const hasArgExprs = Object.keys(value.check_args_exprs).length > 0;
  const staticArgsKey = JSON.stringify(value.check_args);
  // prev is bound as on the run, to the stored last result — a cursor-style argument
  // (`prev.next_page`) otherwise previews as if the job had never run.
  // Memoised: a stored result can be tens of kilobytes, and this runs in the render body.
  const prevKey = useMemo(() => JSON.stringify(storedResult ?? null), [storedResult]);
  useEffect(() => {
    if (!hasArgExprs) {
      setArgsPreview({});
      return;
    }
    let cancelled = false;
    const timer = setTimeout(() => {
      validateArgsExpr({
        check_args_exprs: value.check_args_exprs,
        check_args: value.check_args,
        prev: storedResult ?? null,
      })
        .then((res) => {
          if (cancelled) return;
          setArgsPreview(
            res.valid
              ? { resolved: res.resolved ?? {} }
              : { error: res.error ?? 'The expression could not be resolved.' },
          );
        })
        .catch((e: unknown) => {
          if (!cancelled) setArgsPreview({ error: e instanceof Error ? e.message : String(e) });
        });
    }, 500);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
    // The keys stand in for their objects.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [argsExprsKey, hasArgExprs, staticArgsKey, prevKey]);

  const selectedTool = mcpTools.find((t) => t.name === value.check_tool);
  const toolSchema = useMemo(() => parseToolSchema(selectedTool), [selectedTool]);
  const missingArgs = submitMissingArgs;

  // A result describes one specific call. When the tool or its arguments change it stops
  // describing anything, so it is dropped here rather than every caller remembering to.
  // argsExprsKey belongs in it: runCheck resolves the `= …` expressions into the call, so
  // editing one changes what the tool is called with. Without it the old payload survived
  // the edit and went on feeding the picker, the CEL editor and the tester — testing the
  // condition against a response the job would no longer produce.
  const callSignature = `${value.check_tool}|${JSON.stringify(value.check_args)}|${value.check_args_text}|${argsExprsKey}`;
  const liveResult = check.signature === callSignature ? check.result : undefined;

  useEffect(() => {
    onLiveResult?.(liveResult);
    // Keyed on the result alone: a caller passing a fresh callback each render must not
    // re-fire it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [liveResult]);

  /** The response a condition is tested against: this session's call, else the last run's. */
  // Unparseable JSON mid-edit is no known call, so it matches nothing; "no arguments"
  // (args undefined, no error) is a call like any other.
  const formArgs = resolveArgs(value);
  const storedMatches =
    !storedCall ||
    (!formArgs.error &&
      sameCheckCall(storedCall, {
        check_tool: value.check_tool,
        check_args: formArgs.args,
        check_args_exprs: value.check_args_exprs,
      }));
  const storedTestable = storedMatches ? (storedResult ?? undefined) : undefined;
  const testable = liveResult ?? storedTestable;

  // The exclusive choices live on the value, not here: switching only changes what is
  // shown, every field keeps its text, and the save applies the choice
  // (`resolveWatchChoices`). So a flip and a flip back cost nothing.
  const conditionMode = value.condition_mode;
  const messageMode = value.message_mode;
  const hasMessage = Boolean(value.notification_message.trim());

  function toggleArgsMode() {
    if (value.args_mode === 'fields') {
      const text = Object.keys(value.check_args).length
        ? JSON.stringify(value.check_args, null, 2)
        : '';
      patch({ args_mode: 'json', check_args_text: text });
      return;
    }
    const { args, error } = resolveArgs(value);
    if (error) {
      onError?.(error);
      return;
    }
    patch({ args_mode: 'fields', check_args: args ?? {} });
  }

  /**
   * Call the tool and show its response, so the condition can be written against a real
   * payload instead of a guess. The call is real; the backend answers 428 for anything it
   * cannot confirm is read-only, which surfaces here as an explicit confirmation.
   */
  async function runCheck(acknowledgeRisk: boolean) {
    const { args, error } = resolveArgs(value);
    if (error) {
      setCheck({ loading: false, error });
      return;
    }
    const missing = missingRequiredArgs(selectedTool, value, args);
    if (missing.size > 0) {
      setMissingArgs(missing);
      setCheck({ loading: false, error: 'Fill the required arguments first.' });
      return;
    }
    setMissingArgs(new Set());
    setRiskPrompt(null);
    setCheck({ loading: true });
    try {
      // The test call uses the same argument resolution the scheduler will: static
      // args plus the `= …` expressions, or it is not testing the real job.
      let callArgs = args ?? {};
      if (Object.keys(value.check_args_exprs).length > 0) {
        const dyn = await validateArgsExpr({
          check_args_exprs: value.check_args_exprs,
          check_args: callArgs,
          prev: storedResult ?? null,
        });
        if (!dyn.valid) {
          setCheck({ loading: false, error: `Dynamic arguments failed: ${dyn.error ?? 'unresolvable'}` });
          return;
        }
        callArgs = dyn.resolved ?? callArgs;
      }
      const response = await invokeMcpTool(value.check_tool, callArgs, {
        serverSlug: selectedTool?.server ?? null,
        acknowledgeRisk,
      });
      setCheck({
        loading: false,
        result: response.result,
        elapsedMs: response.elapsed_ms,
        truncated: response.truncated,
        isError: response.is_error,
        signature: callSignature,
      });
      setShowResponse(!value.cel_expr.trim());
    } catch (e) {
      if (e instanceof McpToolRiskError) {
        setCheck({ loading: false });
        setRiskPrompt(e.message);
        return;
      }
      setCheck({ loading: false, error: e instanceof Error ? e.message : String(e) });
    }
  }

  if (!editing) return <WatchFieldsRead value={value} tool={selectedTool} subAgents={subAgents} sectionOffset={sectionOffset} />;

  return (
    <>
      <SectionHeader n={sectionOffset} title="What to check" />

              <div className="grid gap-1.5">
                <Label htmlFor="check_tool">
                  Tool
                  {filled.has('check_tool') && <AiBadge />}
                </Label>
                <McpToolSelect
                  id="check_tool"
                  tools={mcpTools}
                  value={value.check_tool}
                  invalid={Boolean(errors.check_tool)}
                  onChange={(toolName) => {
                    // A different tool returns a different shape, so the expression and
                    // the previous result stop describing anything real.
                    patch({
                      check_tool: toolName,
                      check_args: {},
                      check_args_text: '',
                      args_mode: 'fields',
                      check_args_exprs: {},
                      cel_expr: '',
                    });
                    setCheck({ loading: false });
                  }}
                />
                {selectedTool?.description && (
                  <p className="text-muted-foreground text-xs">{selectedTool.description}</p>
                )}
                {errors.check_tool && <FieldError>{errors.check_tool}</FieldError>}
              </div>

              {value.check_tool && (
                <div className="grid gap-1.5">
                  <div className="flex items-center justify-between gap-2">
                    <Label>
                      Arguments
                      <HintTip>
                        Start a value with <code>=</code> to compute it on every run, e.g.{' '}
                        <code>= strftime(now - duration('168h'), '%Y-%m-%d')</code> for a rolling
                        7-day window.
                      </HintTip>
                      {filled.has('check_args') && <AiBadge />}
                    </Label>
                    <Button type="button" variant="ghost" size="sm" onClick={toggleArgsMode}>
                      {value.args_mode === 'json' ? 'Use fields' : 'Edit as JSON'}
                    </Button>
                  </div>
                  {value.args_mode === 'fields' ? (
                    <>
                      <McpToolArgsFields
                        tool={selectedTool}
                        values={value.check_args}
                        exprs={value.check_args_exprs}
                        missingRequired={missingArgs}
                        onChange={(next) => {
                          patch({ check_args: next });
                        }}
                        onExprsChange={(next) => {
                          patch({ check_args_exprs: next });
                        }}
                      />
                      {toolSchema.params.length === 0 && toolSchema.complex.length === 0 && (
                        <p className="text-muted-foreground text-xs">
                          No argument schema — use “Edit as JSON” if it needs any.
                        </p>
                      )}
                      {toolSchema.complex.length > 0 && (
                        <p className="text-muted-foreground text-xs">
                          {toolSchema.complex.join(', ')}{' '}
                          {toolSchema.complex.length === 1 ? 'takes' : 'take'} a nested value — use
                          “Edit as JSON” to set {toolSchema.complex.length === 1 ? 'it' : 'them'}.
                        </p>
                      )}
                      {errors.check_args && <FieldError>{errors.check_args}</FieldError>}
                    </>
                  ) : (
                    <McpToolArgsJson
                      text={value.check_args_text}
                      error={errors.check_args}
                      onChange={(text) => {
                        patch({ check_args_text: text });
                      }}
                    />
                  )}

                  {Object.keys(value.check_args_exprs).length > 0 ? (
                    argsPreview.error ? (
                      <FieldError>{argsPreview.error}</FieldError>
                    ) : argsPreview.resolved ? (
                      <p className="text-muted-foreground font-mono text-[11px]">
                        right now → {JSON.stringify(argsPreview.resolved)}
                      </p>
                    ) : null
                  ) : null}
                </div>
              )}

      <SectionHeader n={sectionOffset + 1} title="Trigger when" />

              {/* Two halves of one condition, at least one required: the expression is
                  the deterministic gate, the judgement the semantic stage the model
                  applies to what the gate matched. */}
              <div className="grid gap-2">
                <ChoiceLabel>Decide with</ChoiceLabel>
                <Segmented<ConditionMode>
                  label="Decide with"
                  value={conditionMode}
                  onChange={(next) => patch({ condition_mode: next })}
                  options={[
                    { value: 'cel', label: 'An expression' },
                    { value: 'judge', label: 'AI judgement' },
                    { value: 'both', label: 'Expression, then AI' },
                  ]}
                />
                <p className="text-muted-foreground text-xs">
                  {conditionMode === 'cel'
                    ? 'Deterministic and free: the expression alone decides.'
                    : conditionMode === 'judge'
                      ? 'A small model reads the whole response on every run and decides.'
                      : 'The expression filters for free; the model only judges what it matched.'}
                </p>
              </div>

              {conditionMode !== 'judge' && (
              <div className="grid gap-1.5">
                <Label htmlFor="cel_expr">
                  Expression
                  <span className="text-muted-foreground text-xs font-normal">CEL</span>
                  <HintTip>
                    Over <code>result</code> (the response), <code>now</code> (current time, job
                    timezone) and <code>prev</code> (the previous result). A boolean gates
                    directly; anything else triggers when non-empty.
                  </HintTip>
                  {filled.has('cel_expr') && <AiBadge />}
                </Label>
                <CelExpressionEditor
                  value={value.cel_expr}
                  llmCondition={conditionMode === 'both' ? value.llm_condition : ''}
                  onChange={(next) =>
                    // Refining can add a judgement; it must not land in a hidden field.
                    patch({
                      ...next,
                      ...(next.llm_condition?.trim() && conditionMode === 'cel'
                        ? { condition_mode: 'both' as const }
                        : {}),
                    })
                  }
                  payload={testable}
                  checkTool={value.check_tool}
                  invalid={Boolean(errors.cel_expr)}
                />
                {errors.cel_expr ? (
                  <FieldError>{errors.cel_expr}</FieldError>
                ) : (
                  // The one line worth keeping inline: it changes what people write.
                  <p className="text-muted-foreground text-xs">
                    Return the matching items — they are what the notification is written from
                    and what an agent is handed.
                  </p>
                )}
              </div>
              )}

              {conditionMode !== 'cel' && (
              <div className="grid gap-1.5">
                <Label htmlFor="llm_condition">
                  {conditionMode === 'both' ? 'Then AI judges' : 'AI judges'}
                  {filled.has('llm_condition') && <AiBadge />}
                </Label>
                <Textarea
                  id="llm_condition"
                  rows={2}
                  value={value.llm_condition}
                  aria-invalid={Boolean(errors.llm_condition) || undefined}
                  placeholder="e.g. an attendee looks external to the company"
                  onChange={(e) => {
                    patch({ llm_condition: e.target.value });
                  }}
                />
                {/* No standing hint: the "Decide with" caption above already says what the
                    model judges and when. */}
                {errors.llm_condition && <FieldError>{errors.llm_condition}</FieldError>}
              </div>
              )}

              {/* One place answers "does this condition work on real data": the call,
                  the verdict, and the response to pick a path from. It used to be three
                  widgets across two sections. */}
              <div className="grid gap-2">
                <ConditionTester
                  liveResult={testable}
                  prev={storedResult ?? null}
                  celExpr={conditionMode === 'judge' ? '' : value.cel_expr}
                  llmCondition={conditionMode === 'cel' ? '' : value.llm_condition}
                  // liveResult, not check.result: once the tool or its arguments change,
                  // a result describes a call the job would no longer make.
                  resultSource={
                    liveResult
                      ? `this check${check.isError ? ' (the tool reported an error)' : ''}${check.truncated ? ' (truncated)' : ''}`
                      : storedTestable
                        ? "the last run's response"
                        : undefined
                  }
                  onRun={value.check_tool ? () => void runCheck(false) : undefined}
                  running={check.loading}
                />
                {riskPrompt && (
                  <div className="grid gap-2 rounded-md border border-amber-500/40 p-3">
                    <span className="flex items-start gap-1.5 text-xs">
                      <AlertCircle className="mt-0.5 size-3.5 shrink-0" />
                      {riskPrompt}
                    </span>
                    <div className="flex gap-2">
                      <Button type="button" size="sm" variant="outline" onClick={() => runCheck(true)}>
                        Run it anyway
                      </Button>
                      <Button type="button" size="sm" variant="ghost" onClick={() => setRiskPrompt(null)}>
                        Cancel
                      </Button>
                    </div>
                  </div>
                )}
                {check.error && <FieldError>{check.error}</FieldError>}
                {liveResult && (
                  <Collapsible open={showResponse} onOpenChange={setShowResponse}>
                    <CollapsibleTrigger asChild>
                      <Button type="button" variant="ghost" size="sm" className="text-muted-foreground h-6 w-fit px-2 text-xs">
                        <ChevronDown className={cn('size-3 transition-transform', showResponse && 'rotate-180')} />
                        Full response · {check.elapsedMs}ms
                        <span className="font-normal">— click a value to watch it</span>
                      </Button>
                    </CollapsibleTrigger>
                    <CollapsibleContent>
                      <div className="mt-1 overflow-hidden rounded-md border">
                        <JsonPathPicker
                          value={liveResult}
                          onPick={(path) => {
                            // A click seeds the expression with that location in CEL's
                            // spelling; refining it into a filter is then typing, not
                            // translating.
                            patch({
                              cel_expr: jsonPathToCel(path),
                              ...(conditionMode === 'judge' && { condition_mode: 'both' as const }),
                            });
                          }}
                        />
                      </div>
                    </CollapsibleContent>
                  </Collapsible>
                )}
              </div>

              <div className="flex items-center gap-2.5">
                <Checkbox
                  id="destroy_after_trigger"
                  checked={value.destroy_after_trigger}
                  onCheckedChange={(checked) => patch({ destroy_after_trigger: checked === true })}
                />
                <Label htmlFor="destroy_after_trigger" className="cursor-pointer font-normal">
                  Pause the job after it triggers once
                </Label>
                <HintTip>Leave this off to keep watching and be told every time the condition holds.</HintTip>
              </div>

      <SectionHeader n={sectionOffset + 2} title="When it triggers" />

              {/* Exclusive: a sub-agent's reply is delivered instead of the
                  notification, so showing both would leave one of them inert. */}
              <ChoiceLabel>Outcome</ChoiceLabel>
              <div role="radiogroup" aria-label="Outcome" className="grid gap-2 sm:grid-cols-2">
                {/* Nothing is cleared on a flip: the brief and the agent's instruction are
                    separate fields, and the save sends only the chosen outcome's. */}
                <OptionCard
                  selected={value.outcome === 'notify'}
                  title="Send a notification"
                  description="Deliver a message you write, or one written from the result."
                  onClick={() => patch({ outcome: 'notify' })}
                />
                <OptionCard
                  selected={value.outcome === 'agent'}
                  title="Run a sub-agent"
                  description="Hand it the result; its reply is delivered instead."
                  onClick={() => patch({ outcome: 'agent' })}
                />
              </div>

              {value.outcome === 'notify' ? (
                <div className="grid gap-2">
                  <ChoiceLabel>Message</ChoiceLabel>
                  <Segmented<MessageMode>
                    label="Message"
                    value={messageMode}
                    onChange={(next) => patch({ message_mode: next })}
                    options={[
                      { value: 'written', label: 'Written from what matched' },
                      { value: 'fixed', label: 'Fixed text' },
                    ]}
                  />
                  {messageMode === 'written' ? (
                    <div className="grid gap-1.5 pt-1">
                      <Label htmlFor="notification_brief">
                        How to write it
                        <span className="text-muted-foreground text-xs font-normal">optional</span>
                        <HintTip>
                          A brief for the model that writes the message from the matched items:
                          what to include, how to build a link from their fields, how to lay it
                          out. It is written for the delivery channel, so a list or a table renders
                          where the channel supports it.
                        </HintTip>
                        {filled.has('notification_brief') && <AiBadge />}
                      </Label>
                      <Textarea
                        id="notification_brief"
                        rows={3}
                        value={value.notification_brief}
                        placeholder={
                          'e.g. One line per item, linking to its campaign: https://example.com/campaigns/{campaignId}. Mention the execution date.'
                        }
                        onChange={(e) => {
                          patch({ notification_brief: e.target.value });
                        }}
                      />
                      <p className="text-muted-foreground text-xs">
                        Empty, it writes one or two sentences on what changed.
                      </p>
                    </div>
                  ) : (
                    <div className="grid gap-1.5 pt-1">
                      <Label htmlFor="notification_message">
                        Text to send
                        {filled.has('notification_message') && <AiBadge />}
                      </Label>
                      <Textarea
                        id="notification_message"
                        rows={3}
                        value={value.notification_message}
                        placeholder="Sent exactly as written every time the job triggers."
                        onChange={(e) => {
                          patch({ notification_message: e.target.value });
                        }}
                      />
                      {!hasMessage && (
                        <p className="text-muted-foreground text-xs">
                          Left empty, a message is written from what matched instead.
                        </p>
                      )}
                    </div>
                  )}
                </div>
              ) : (
                <AgentActionFields
                  value={value}
                  onChange={patch}
                  subAgents={subAgents}
                  mcpTools={mcpTools}
                  instructionLabel="Instruction"
                  instructionPlaceholder="e.g. Summarize the failure and email it to the account owner…"
                  instructionHint="Invoked with what the condition matched (the whole check result when it returned a boolean or a single value), plus this instruction. If empty, the agent is asked to take appropriate action on it."
                  onLimitExceeded={onError}
                  fieldErrors={errors}
                />
              )}
    </>
  );
}

/**
 * The same fields, read-only. Deliberately a different rendering rather than the edit
 * form with everything disabled: values are what the reader came for, and a disabled
 * input renders them as the faintest thing on the page.
 */
function WatchFieldsRead({
  value,
  tool,
  subAgents,
  sectionOffset,
}: {
  value: WatchFieldsValue;
  tool: McpTool | undefined;
  subAgents: { id: number; name: string }[];
  sectionOffset: number;
}) {
  const args = Object.entries(value.check_args);
  const agent = subAgents.find((a) => String(a.id) === value.sub_agent_id);

  return (
    <>
      <SectionHeader n={sectionOffset} title="What to check" />
      <ReadValue label="Tool" hint={tool?.description ?? undefined}>
        {tool ? (
          <span className="inline-flex items-center gap-2">
            <Badge variant="outline" className="text-[10.5px]">
              {toolServer(tool)}
            </Badge>
            {toolShortName(tool)}
          </span>
        ) : (
          value.check_tool || undefined
        )}
      </ReadValue>
      <ReadValue label="Arguments" mono empty="No arguments">
        {args.length ? args.map(([k, v]) => `${k}: ${String(v)}`).join('  ·  ') : undefined}
      </ReadValue>
      {Object.keys(value.check_args_exprs).length > 0 && (
        <ReadValue
          label="Dynamic arguments"
          mono
          hint="Resolved against the current time on every run, merged over the arguments."
        >
          {Object.entries(value.check_args_exprs)
            .map(([k, v]) => `${k}: = ${v}`)
            .join('  ·  ')}
        </ReadValue>
      )}

      <SectionHeader n={sectionOffset + 1} title="Trigger when" />
      <ReadValue label="Decided by">
        {
          {
            cel: 'An expression',
            judge: 'An AI judgement',
            both: 'An expression, then an AI judgement on what it matched',
          }[value.condition_mode]
        }
      </ReadValue>
      {value.condition_mode !== 'judge' ? (
        <>
          <ReadValue
            label="Expression"
            hint="CEL — a boolean gates directly; anything else triggers when non-empty."
          >
            {/* A pre rather than a span: the author's own line breaks and indentation
                are how a multi-clause expression stays readable, and a span collapses
                them into one wrapped line. */}
            <pre className="bg-muted mt-0.5 overflow-x-auto rounded-md border px-3 py-2 font-mono text-xs leading-5 whitespace-pre-wrap break-words">
              {value.cel_expr}
            </pre>
          </ReadValue>
          {value.condition_mode === 'both' && (
            <ReadValue
              label="Then AI judges"
              hint="A model judges what the expression returned, only when it matched something."
            >
              {value.llm_condition}
            </ReadValue>
          )}
        </>
      ) : (
        <ReadValue label="Condition" hint="Judged by a small model on each run.">
          {value.llm_condition || undefined}
        </ReadValue>
      )}
      <ReadValue label="After it triggers">
        {value.destroy_after_trigger
          ? 'The job pauses itself'
          : 'The job keeps watching and reports every time'}
      </ReadValue>

      <SectionHeader n={sectionOffset + 2} title="When it triggers" />
      {value.outcome === 'agent' ? (
        <>
          <ReadValue label="Outcome">
            Runs the sub-agent{' '}
            <span className="font-medium">{agent?.name ?? `#${value.sub_agent_id}`}</span>, whose
            reply is delivered
          </ReadValue>
          <ReadValue label="Instruction" empty="The agent decides what to do with the result">
            {value.prompt || undefined}
          </ReadValue>
        </>
      ) : (
        <>
          <ReadValue label="Outcome">Sends a notification</ReadValue>
          {value.message_mode === 'fixed' ? (
            <ReadValue label="Message (fixed text)">{value.notification_message || undefined}</ReadValue>
          ) : (
            <ReadValue
              label="Message (written from what matched) — how to write it"
              empty="No brief: one or two sentences on what changed"
            >
              {value.notification_brief || undefined}
            </ReadValue>
          )}
        </>
      )}
    </>
  );
}
