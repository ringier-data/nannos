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
import { AlertCircle, Loader2, Zap } from 'lucide-react';

import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { McpToolSelect } from '@/components/McpToolSelect';
import { McpToolArgsFields, McpToolArgsJson } from '@/components/McpToolArgsFields';
import { JsonPathPicker } from '@/components/JsonPathPicker';
import { CelExpressionEditor } from '@/components/CelExpressionEditor';
import { ConditionTester } from '@/components/ConditionTester';
import { AgentActionFields, type AgentAction } from '@/components/AgentActionFields';
import { SectionHeader, AiBadge, FieldError, OptionCard, ReadValue } from '@/components/formChrome';
import { toolServer, toolShortName, parseToolSchema } from '@/lib/mcpTools';
import { missingRequiredArgs, resolveArgs } from '@/lib/watchArgs';
import { jsonPathToCel } from '@/lib/watchCondition';
import { McpToolRiskError, invokeMcpTool, validateArgsExpr } from '@/api/scheduler';
import type { McpTool } from '@/api/generated/types.gen';

/** Everything a watch job's fields read and write. A superset of the agent action. */
export interface WatchFieldsValue extends AgentAction {
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
  fieldErrors,
  aiFilled,
  onError,
  sectionOffset = 2,
}: {
  value: WatchFieldsValue;
  onChange: (patch: Partial<WatchFieldsValue>) => void;
  mode: 'edit' | 'read';
  mcpTools: McpTool[];
  subAgents: { id: number; name: string; type?: string | null }[];
  /** A response from a previous real run, so a condition can be tested with no new call. */
  storedResult?: Record<string, unknown> | null;
  fieldErrors?: Record<string, string>;
  /** Field keys an AI fill wrote, marked so a generated value is not taken for a typed one. */
  aiFilled?: Set<string>;
  onError?: (message: string) => void;
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
  useEffect(() => {
    if (!hasArgExprs) {
      setArgsPreview({});
      return;
    }
    let cancelled = false;
    const timer = setTimeout(() => {
      validateArgsExpr({ check_args_exprs: value.check_args_exprs, check_args: value.check_args })
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
  }, [argsExprsKey, hasArgExprs, staticArgsKey]);

  const selectedTool = mcpTools.find((t) => t.name === value.check_tool);
  const toolSchema = useMemo(() => parseToolSchema(selectedTool), [selectedTool]);
  const missingArgs = submitMissingArgs;

  // A result describes one specific call. When the tool or its arguments change it stops
  // describing anything, so it is dropped here rather than every caller remembering to.
  const callSignature = `${value.check_tool}|${JSON.stringify(value.check_args)}|${value.check_args_text}`;
  const liveResult = check.signature === callSignature ? check.result : undefined;

  /** The response a condition is tested against: this session's call, else the last run's. */
  const testable = liveResult ?? storedResult ?? undefined;

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
                          This tool publishes no argument schema. Use “Edit as JSON” if it needs
                          arguments.
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
                  ) : (
                    <p className="text-muted-foreground text-xs">
                      Start a value with <code>=</code> to compute it on every run, e.g.{' '}
                      <code>= strftime(now - duration('168h'), '%Y-%m-%d')</code> for a
                      rolling 7-day window.
                    </p>
                  )}
                </div>
              )}

              {/* Run the check and seed the expression from the real response, rather
                  than writing a condition against a payload nobody has seen. */}
              {value.check_tool && (
                <div className="grid gap-2">
                  <div className="flex flex-wrap items-center gap-3">
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      disabled={check.loading}
                      onClick={() => runCheck(false)}
                    >
                      {check.loading ? (
                        <Loader2 className="size-3.5 animate-spin" />
                      ) : (
                        <Zap className="size-3.5" />
                      )}
                      {liveResult ? 'Run again' : 'Run check now'}
                    </Button>
                    <span className="text-muted-foreground text-xs">
                      {check.result
                        ? `${check.isError ? 'Tool reported an error' : 'Responded'} in ${check.elapsedMs}ms${check.truncated ? ' · response truncated' : ''}`
                        : 'See the real response before writing a condition.'}
                    </span>
                  </div>
                  {riskPrompt && (
                    <div className="grid gap-2 rounded-md border border-dashed p-3">
                      <span className="flex items-start gap-1.5 text-xs">
                        <AlertCircle className="mt-0.5 size-3.5 shrink-0" />
                        {riskPrompt}
                      </span>
                      <div className="flex gap-2">
                        <Button type="button" size="sm" variant="outline" onClick={() => runCheck(true)}>
                          Run it anyway
                        </Button>
                        <Button
                          type="button"
                          size="sm"
                          variant="ghost"
                          onClick={() => setRiskPrompt(null)}
                        >
                          Cancel
                        </Button>
                      </div>
                    </div>
                  )}
                  {check.error && <FieldError>{check.error}</FieldError>}
                  {liveResult && (
                    <div className="overflow-hidden rounded-md border">
                      <div className="bg-muted flex items-center justify-between gap-2 border-b px-2.5 py-1.5">
                        <span className="text-muted-foreground text-[11px] font-semibold tracking-wide uppercase">
                          Result
                        </span>
                        <span className="text-muted-foreground text-[11px]">
                          click any value or object to watch it
                        </span>
                      </div>
                      <JsonPathPicker
                        value={liveResult}
                        allowObjects
                        selectedPath=""
                        onPick={(path) => {
                          // A click seeds the expression with that location in CEL's
                          // spelling; refining it into a filter is then typing, not
                          // translating.
                          patch({ cel_expr: jsonPathToCel(path) });
                        }}
                      />
                    </div>
                  )}
                </div>
              )}

      <SectionHeader n={sectionOffset + 1} title="Trigger when" />

              {/* Two optional halves of one condition, at least one required: the
                  expression is the deterministic gate, the judgement the semantic
                  stage the model applies to what the gate matched. */}
              <div className="grid gap-1.5">
                <Label htmlFor="cel_expr">
                  Expression
                  <span className="text-muted-foreground text-xs font-normal">
                    CEL · optional when a judgement is given
                  </span>
                  {filled.has('cel_expr') && <AiBadge />}
                </Label>
                <CelExpressionEditor
                  value={value.cel_expr}
                  llmCondition={value.llm_condition}
                  onChange={patch}
                  payload={testable}
                  checkTool={value.check_tool}
                  invalid={Boolean(errors.cel_expr)}
                />
                {errors.cel_expr ? (
                  <FieldError>{errors.cel_expr}</FieldError>
                ) : (
                  <p className="text-muted-foreground text-xs">
                    Over <code>result</code> (the response), <code>now</code> (current time,
                    job timezone) and <code>prev</code> (the previous result). A boolean
                    gates directly; anything else triggers when non-empty — return the
                    matching items, they become the evidence the run records.
                  </p>
                )}
              </div>

              <div className="grid gap-1.5">
                <Label htmlFor="llm_condition">
                  AI judges
                  <span className="text-muted-foreground text-xs font-normal">
                    optional when an expression is given
                  </span>
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
                {errors.llm_condition ? (
                  <FieldError>{errors.llm_condition}</FieldError>
                ) : (
                  <p className="text-muted-foreground text-xs">
                    {value.cel_expr.trim()
                      ? 'Runs only when the expression matched something, and judges what it returned — the mechanical part stays free, the model decides the semantic part.'
                      : 'Without an expression, a small model judges the whole response on every run.'}
                  </p>
                )}
              </div>

              <ConditionTester
                liveResult={testable}
                celExpr={value.cel_expr}
                llmCondition={value.llm_condition}
              />

              <div className="flex items-start gap-2.5">
                <Checkbox
                  id="destroy_after_trigger"
                  className="mt-0.5"
                  checked={value.destroy_after_trigger}
                  onCheckedChange={(checked) => patch({ destroy_after_trigger: checked === true })}
                />
                <div className="grid gap-1">
                  <Label htmlFor="destroy_after_trigger" className="cursor-pointer font-normal">
                    Pause the job after it triggers once
                  </Label>
                  <p className="text-muted-foreground text-xs">
                    Leave this off to keep watching and be told every time the condition holds.
                  </p>
                </div>
              </div>

      <SectionHeader n={sectionOffset + 2} title="When it triggers" />

              {/* Exclusive: a sub-agent's reply is delivered instead of the
                  notification, so showing both would leave one of them inert. */}
              <div className="grid gap-2 sm:grid-cols-2">
                <OptionCard
                  selected={value.outcome === 'notify'}
                  title="Send a notification"
                  description="Deliver a message you write, or one written from the result."
                  onClick={() => patch({ outcome: 'notify', sub_agent_id: '' })}
                />
                <OptionCard
                  selected={value.outcome === 'agent'}
                  title="Run a sub-agent"
                  description="Hand it the result; its reply is delivered instead."
                  onClick={() => patch({ outcome: 'agent' })}
                />
              </div>

              {value.outcome === 'notify' ? (
                <div className="grid gap-1.5">
                  <Label htmlFor="notification_message">
                    Message
                    <span className="text-muted-foreground text-xs font-normal">optional</span>
                    {filled.has('notification_message') && <AiBadge />}
                  </Label>
                  <Textarea
                    id="notification_message"
                    rows={3}
                    value={value.notification_message}
                    placeholder="Leave empty and a message is written from the check result."
                    onChange={(e) => {
                      patch({ notification_message: e.target.value });
                    }}
                  />
                </div>
              ) : (
                <AgentActionFields
                  value={value}
                  onChange={patch}
                  subAgents={subAgents}
                  mcpTools={mcpTools}
                  instructionLabel="Instruction"
                  instructionPlaceholder="e.g. Summarize the failure and email it to the account owner…"
                  instructionHint="Invoked with the check result as its input, plus this instruction. If empty, the agent is asked to take appropriate action based on the result."
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
      {value.cel_expr ? (
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
          {value.llm_condition && (
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
          <ReadValue label="Message" empty="Written from the check result when it triggers">
            {value.notification_message || undefined}
          </ReadValue>
        </>
      )}
    </>
  );
}
