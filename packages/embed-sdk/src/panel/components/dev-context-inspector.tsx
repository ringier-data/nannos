/**
 * Developer-mode inspector: a collapsible bar above the composer that shows,
 * LIVE, exactly what crosses the wire between the host and the agent —
 *
 *   - `pageContext`: the merged + sanitized snapshot that rides the next send
 *     as `metadata.pageContext` (what the composer chip only hints at);
 *   - the active conversation's `contextKey` (conversation scoping);
 *   - the client-object registry manifest that rides sends as `clientObjects`;
 *   - the wire log: every raw agent_response event received and every payload
 *     sent, each expandable to its full JSON, and the whole log copyable as
 *     one JSON array for a bug report (see transport/wire-log.ts). Live
 *     traffic, this browser's stored record of earlier turns (localStorage,
 *     dev mode only), and — on demand — the backend's own persisted record,
 *     which is the only way to inspect a conversation this browser never ran.
 *   - the client-action log: every directive the agent sent, which path
 *     delivered it, and what the SDK answered — plus a runner that fires a
 *     hand-written directive through the same executor, so the host side can
 *     be proven without waiting on an agent (see core/client-action-log.ts).
 *
 * It also carries dev mode's own ON/OFF switch. That lives HERE rather than in
 * the panel header because the header is optional (`<AssistantPanel
 * header={false}>` — the console's chat page does exactly that), and a switch
 * on a surface that may not exist is no switch at all. The bar is the one piece
 * of dev chrome that is always on screen while dev mode is available, so it is
 * the one place the switch can always be found.
 *
 * Switched OFF, everything below collapses and the bar alone remains: the exact
 * end-user view, with one click back. The bar is the price of dev mode being
 * available at all — hosts turn it off through the `devMode` prop.
 *
 * Deliberately NOT translated — this is tooling for the integrating developer,
 * never end-user chrome. Enabled via panel/dev-mode.tsx.
 */
import { useEffect, useMemo, useReducer, useState, useSyncExternalStore } from 'react';
import {
  ArrowDownLeftIcon,
  ArrowLeftRightIcon,
  ArrowRightIcon,
  ArrowUpRightIcon,
  AudioWaveformIcon,
  BugIcon,
  CheckIcon,
  ChevronDownIcon,
  CopyIcon,
  DownloadIcon,
  ExternalLinkIcon,
  PlayIcon,
  Trash2Icon,
} from 'lucide-react';
import { Switch } from '../../components/ui/switch';
import { cn } from '../../lib/utils';
import { YamlView } from './yaml-view';
import { useAssistant } from '../../react';
import type { ClientActionLogEntry } from '../../core';
import { fetchWireHistory, type WireLogEntry } from '../../transport';
import { useChatEngineOptional } from '../engine';
import type { UseNannosChatValue } from '../hooks/use-nannos-chat';
import { useDevModeControls } from '../dev-mode';

/** Log rows and toolbar controls repeat verbatim in both logs; naming them
 *  keeps the two in step and the contrast rules in one place. Secondary text
 *  is `text-muted-foreground` ONCE — never muted text dimmed again with
 *  `opacity-*`, which is what made this panel unreadable. */
const logRowClass =
  'flex cursor-pointer select-none items-center gap-1.5 rounded px-1.5 py-1 font-mono text-[11px] hover:bg-muted [&::-webkit-details-marker]:hidden [list-style:none]';

/** The body is three independent stories — what rides the wire, what the host
 *  executed, what actually crossed — and stacking all three made the bar taller
 *  than the thread it sits above. One at a time; the labels carry counts so the
 *  two that are hidden still announce themselves. */
const TABS = [
  { id: 'context', label: 'context' },
  { id: 'actions', label: 'actions' },
  { id: 'wire', label: 'wire' },
] as const;

type TabId = (typeof TABS)[number]['id'];

const barButtonClass =
  'inline-flex items-center gap-1 rounded border border-border bg-background/70 px-1.5 py-0.5 hover:bg-muted disabled:opacity-50 disabled:hover:bg-background/70';

function Section({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="flex items-baseline gap-2 px-0.5 pb-0.5">
        <span className="font-medium text-foreground">{title}</span>
        {hint && <span className="truncate text-muted-foreground">{hint}</span>}
      </div>
      {children}
    </div>
  );
}

/** Payload view, rendered as YAML — half the lines of pretty-printed JSON in
 *  a panel this narrow. The clipboard exports stay JSON (see the copy button):
 *  YAML is for reading, JSON is for pasting into a bug report. */
function Yaml({ value, className }: { value: unknown; className?: string }) {
  return (
    <YamlView
      // '—' for a cleared slot: `undefined` already renders so via the emitter.
      value={value === null ? undefined : value}
      className={cn(
        'max-h-48 overflow-auto rounded-md border border-border bg-muted p-2 font-mono text-[11px] leading-snug text-foreground',
        className,
      )}
    />
  );
}

function timeOf(ts: number): string {
  const d = new Date(ts);
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

/** One wire event: a summary row expanding to the raw payload JSON. */
function WireRow({ entry }: { entry: WireLogEntry }) {
  // Lazy: the payload is only serialized once the row is opened.
  const [open, setOpen] = useState(false);
  const Dir = entry.dir === 'in' ? ArrowDownLeftIcon : ArrowUpRightIcon;
  // Live entries are unmarked; a replayed one says which record it came from.
  const from = entry.source === 'stored' ? 'local' : entry.source === 'server' ? 'server' : null;
  return (
    <details onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary className={logRowClass}>
        <span className="shrink-0 text-muted-foreground">{timeOf(entry.ts)}</span>
        <Dir
          aria-label={entry.dir === 'in' ? 'received' : 'sent'}
          className={cn(
            'size-3 shrink-0',
            entry.dir === 'in'
              ? 'text-sky-700 dark:text-sky-400'
              : 'text-emerald-700 dark:text-emerald-400',
          )}
        />
        <span className="min-w-0 flex-1 truncate text-foreground">{entry.label}</span>
        {from && <span className="shrink-0 text-muted-foreground">{from}</span>}
      </summary>
      {open && <Yaml value={entry.payload} className="max-h-64" />}
    </details>
  );
}

/** The whole log as one JSON array, oldest first — the order a reader expects
 *  in a pasted transcript, and the reverse of the newest-first view. Payloads
 *  are wire JSON, so the plain encode all but always works; a payload that
 *  does carry a cycle falls back to an encode that marks the repeat instead of
 *  throwing away the copy. */
function serializeEntries(entries: WireLogEntry[]): string {
  const rows = entries.map((e) => ({
    seq: e.seq,
    at: new Date(e.ts).toISOString(),
    dir: e.dir,
    source: e.source ?? 'live',
    conversationId: e.conversationId,
    label: e.label,
    payload: e.payload,
  }));
  try {
    return JSON.stringify(rows, null, 2);
  } catch {
    const seen = new WeakSet<object>();
    return JSON.stringify(
      rows,
      (_key, value) => {
        if (typeof value !== 'object' || value === null) return value;
        if (seen.has(value)) return '[circular]';
        seen.add(value);
        return value;
      },
      2,
    );
  }
}

/** Clipboard API first; a dev host served over plain http has none, so fall
 *  back to a throwaway textarea + execCommand rather than nothing. */
async function writeClipboard(text: string): Promise<boolean> {
  if (typeof window === 'undefined') return false;
  if (navigator?.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // fall through to the legacy path
    }
  }
  const area = document.createElement('textarea');
  area.value = text;
  area.setAttribute('readonly', '');
  area.style.cssText = 'position:fixed;top:-1000px;opacity:0';
  document.body.appendChild(area);
  area.select();
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch {
    ok = false;
  }
  area.remove();
  return ok;
}

/** Copies every event of the section, not only the expanded ones. */
function CopyWireButton({ entries }: { entries: WireLogEntry[] }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle');

  useEffect(() => {
    if (state === 'idle') return;
    const id = window.setTimeout(() => setState('idle'), 2000);
    return () => window.clearTimeout(id);
  }, [state]);

  const Icon = state === 'copied' ? CheckIcon : CopyIcon;
  return (
    <button
      type="button"
      disabled={entries.length === 0}
      title="Copy every event of this conversation as JSON"
      className={barButtonClass}
      onClick={() => {
        void writeClipboard(serializeEntries(entries)).then((ok) =>
          setState(ok ? 'copied' : 'failed'),
        );
      }}
    >
      <Icon className="size-3" />
      {state === 'copied' ? 'copied' : state === 'failed' ? 'copy failed' : `copy all (${entries.length})`}
    </button>
  );
}

/** Pulls the backend's persisted record of the conversation into the log —
 *  the only source for one this browser never ran (another machine, another
 *  day, or before dev mode was on). A reconstruction: only events the backend
 *  stores, so streamed deltas arrive as their stored final. */
function LoadServerButton({ conversationId }: { conversationId: string }) {
  const engine = useChatEngineOptional();
  const [state, setState] = useState<'idle' | 'loading' | 'failed'>('idle');

  // A conversation switch must not carry the previous pull's outcome.
  useEffect(() => setState('idle'), [conversationId]);

  const already = engine?.wireLog.hasReplay(conversationId) ?? false;
  return (
    <button
      type="button"
      disabled={!engine || state === 'loading'}
      title="Load this conversation's traffic from the backend's stored record"
      className={barButtonClass}
      onClick={() => {
        if (!engine) return;
        setState('loading');
        void fetchWireHistory(engine.adapter.api.fetch, conversationId).then((entries) => {
          if (!entries) {
            setState('failed');
            return;
          }
          engine.wireLog.replay(conversationId, entries);
          setState('idle');
        });
      }}
    >
      <DownloadIcon className="size-3" />
      {state === 'loading'
        ? 'loading…'
        : state === 'failed'
          ? 'load failed'
          : already
            ? 'reload from server'
            : 'load from server'}
    </button>
  );
}

/** The received/sent raw traffic of the ACTIVE conversation, newest first:
 *  what is happening live, merged with whatever record the log can produce for
 *  it (this browser's stored one, and the backend's once pulled). */
function WireSection({ conversationId }: { conversationId: string }) {
  const engine = useChatEngineOptional();
  const empty = useMemo<WireLogEntry[]>(() => [], []);
  const entries = useSyncExternalStore(
    engine?.wireLog.subscribe ?? (() => () => {}),
    engine?.wireLog.getSnapshot ?? (() => empty),
  );
  // Opening a conversation reads back what this browser stored for it — that
  // is what makes an older conversation inspectable after a reload.
  const wireLog = engine?.wireLog;
  useEffect(() => wireLog?.hydrate(conversationId), [wireLog, conversationId]);

  // Chronological for the clipboard, newest-first for the eye.
  const mine = entries.filter((e) => !e.conversationId || e.conversationId === conversationId);
  const shown = [...mine].reverse();
  const live = mine.filter((e) => !e.source).length;
  const stored = mine.filter((e) => e.source === 'stored').length;
  const server = mine.filter((e) => e.source === 'server').length;

  return (
    <Section
      title="wire"
      hint={`${shown.length} events · ${live} live · ${stored} local · ${server} server · newest first`}
    >
      <div className="flex max-h-64 flex-col divide-y divide-border overflow-y-scroll rounded-md border border-border bg-background/40 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-border [&::-webkit-scrollbar-track]:bg-transparent [&::-webkit-scrollbar]:w-2">
        {shown.length === 0 ? (
          <span className="p-2 text-muted-foreground">
            {wireLog?.persists
              ? 'nothing recorded here — send a message, or load the backend\u2019s record'
              : 'no traffic yet — send a message'}
          </span>
        ) : (
          shown.map((entry) => <WireRow key={entry.id} entry={entry} />)
        )}
      </div>
      <div className="mt-1 flex items-center gap-2">
        <CopyWireButton entries={mine} />
        <LoadServerButton conversationId={conversationId} />
        <button
          type="button"
          title="Drop the live log and every conversation stored in this browser"
          className={barButtonClass}
          onClick={() => engine?.wireLog.clear()}
        >
          <Trash2Icon className="size-3" /> clear
        </button>
      </div>
    </Section>
  );
}

/** Colour + wording of an entry's end state. `refused` is the SDK saying no on
 *  purpose (invalid payload, target not registered, no host hook); `threw` is a
 *  host handler that broke, which is an integration bug. */
function outcomeLabel(entry: ClientActionLogEntry): { text: string; className: string } {
  switch (entry.outcome) {
    case 'ok':
      return { text: 'ok', className: 'text-emerald-700 dark:text-emerald-400' };
    case 'refused':
      return {
        text: entry.result && !entry.result.ok ? entry.result.reason : 'refused',
        className: 'text-red-700 dark:text-red-400',
      };
    case 'threw':
      return { text: 'host threw', className: 'text-red-700 dark:text-red-400' };
    default:
      return { text: 'running…', className: 'text-muted-foreground' };
  }
}

/** One executed directive: a summary row expanding to the full picture — what
 *  was sent, what came back, and (for a refused target) what the registry held. */
function ClientActionRow({ entry }: { entry: ClientActionLogEntry }) {
  const [open, setOpen] = useState(false);
  const outcome = outcomeLabel(entry);
  // Round trip = the agent's tool is parked waiting for this answer;
  // fire-and-forget = nothing goes back, so this row is its only record.
  const Dir = entry.path === 'round-trip' ? ArrowLeftRightIcon : ArrowRightIcon;
  return (
    <details onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary className={logRowClass}>
        <span className="shrink-0 text-muted-foreground">{timeOf(entry.ts)}</span>
        <span className="shrink-0" title={entry.path}>
          <Dir aria-label={entry.path} className="size-3 text-sky-700 dark:text-sky-400" />
        </span>
        <span className="shrink-0 font-medium text-foreground">{entry.kind ?? '(no kind)'}</span>
        <span className="min-w-0 flex-1 truncate text-muted-foreground">{entry.target ?? ''}</span>
        <span className={cn('shrink-0 font-medium', outcome.className)}>{outcome.text}</span>
        {entry.durationMs !== undefined && (
          <span className="shrink-0 text-muted-foreground">{entry.durationMs}ms</span>
        )}
      </summary>
      {open && (
        <div className="flex flex-col gap-1 px-1 pb-1">
          <Section title="directive" hint={entry.path}>
            <Yaml value={entry.directive} className="max-h-40" />
          </Section>
          {entry.result && (
            <Section title="result" hint="what the agent was told">
              <Yaml value={entry.result} className="max-h-40" />
            </Section>
          )}
          {entry.error && (
            <Section title="error" hint="a host handler threw">
              <Yaml value={entry.error} className="max-h-24" />
            </Section>
          )}
          {(entry.kind === 'apply' || entry.kind === 'highlight') && (
            <Section
              title="registered targets"
              hint={
                entry.knownTargets.length
                  ? 'what the target was matched against'
                  : 'nothing registered — apply/highlight can only fail'
              }
            >
              <Yaml value={entry.knownTargets.length ? entry.knownTargets : undefined} className="max-h-24" />
            </Section>
          )}
        </div>
      )}
    </details>
  );
}

/** Fires a hand-written directive through `runClientAction` — the SAME executor
 *  the agent's round-trip directives go through, host hooks and registry
 *  included. It answers "is the client side wired at all?" without an agent,
 *  a manifest, or a turn: the outcome lands in the log above like any other. */
function DirectiveRunner() {
  const engine = useChatEngineOptional();
  const [text, setText] = useState('{ "kind": "read_current_page" }');
  const [error, setError] = useState<string | null>(null);
  const targets = engine?.core.registry.keys() ?? [];
  const first = targets[0]?.split(':') as [string, string] | undefined;

  const presets: Array<[string, unknown]> = [
    ['read_current_page', { kind: 'read_current_page' }],
    ['navigate', { kind: 'navigate', to: '/' }],
    ...(first
      ? ([
          ['highlight', { kind: 'highlight', target: { type: first[0], id: first[1] } }],
          ['apply', { kind: 'apply', target: { type: first[0], id: first[1] }, values: {} }],
        ] as Array<[string, unknown]>)
      : []),
  ];

  return (
    <Section title="run a directive" hint="executes host-side, exactly as the agent's would">
      <div className="flex flex-wrap items-center gap-1 pb-1">
        {presets.map(([label, directive]) => (
          <button
            key={label}
            type="button"
            className="rounded border border-border bg-background/70 px-1.5 py-0.5 font-mono text-[11px] text-foreground hover:bg-muted"
            onClick={() => {
              setError(null);
              setText(JSON.stringify(directive));
            }}
          >
            {label}
          </button>
        ))}
        {!first && (
          <span className="text-muted-foreground">no objects registered — only navigate/read work</span>
        )}
      </div>
      <textarea
        value={text}
        spellCheck={false}
        rows={2}
        onChange={(e) => {
          setError(null);
          setText(e.target.value);
        }}
        className="w-full resize-y rounded-md border border-border bg-background p-2 font-mono text-[11px] leading-snug text-foreground outline-none focus:border-amber-500"
      />
      <div className="mt-1 flex items-center gap-2">
        <button
          type="button"
          disabled={!engine}
          className={barButtonClass}
          onClick={() => {
            let directive: unknown;
            try {
              directive = JSON.parse(text);
            } catch (e) {
              setError(e instanceof Error ? e.message : 'not valid JSON');
              return;
            }
            setError(null);
            void engine?.core.runClientAction(directive);
          }}
        >
          <PlayIcon className="size-3" /> run
        </button>
        {error && <span className="min-w-0 truncate font-medium text-red-700 dark:text-red-400">{error}</span>}
      </div>
    </Section>
  );
}

/** Every directive this panel executed, newest first. The fire-and-forget kinds
 *  (navigate/highlight) appear NOWHERE else — they are not chat content — and a
 *  round trip only shows the agent's side in the thread, never the browser's
 *  answer. An empty list during a turn that should have acted means the
 *  directive never arrived: check the wire log below it. */
function ClientActionsSection() {
  const engine = useChatEngineOptional();
  const empty = useMemo<ClientActionLogEntry[]>(() => [], []);
  const entries = useSyncExternalStore(
    engine?.core.clientActions.subscribe ?? (() => () => {}),
    engine?.core.clientActions.getSnapshot ?? (() => empty),
  );
  const shown = [...entries].reverse();
  const failed = entries.filter((e) => e.outcome === 'refused' || e.outcome === 'threw').length;

  return (
    <Section
      title="client actions"
      hint={`${entries.length} executed${failed ? ` · ${failed} failed` : ''} · newest first`}
    >
      <div className="flex max-h-56 flex-col divide-y divide-border overflow-y-auto rounded-md border border-border bg-background/40">
        {shown.length === 0 ? (
          <span className="p-2 text-muted-foreground">
            nothing executed yet — ask the agent to act on the page, or run a directive below
          </span>
        ) : (
          shown.map((entry) => <ClientActionRow key={entry.id} entry={entry} />)
        )}
      </div>
      {entries.length > 0 && (
        <div className="mt-1 flex items-center gap-2">
          <button
            type="button"
            className={barButtonClass}
            onClick={() => engine?.core.clientActions.clear()}
          >
            <Trash2Icon className="size-3" /> clear
          </button>
        </div>
      )}
      <div className="mt-2">
        <DirectiveRunner />
      </div>
    </Section>
  );
}

/** Deep-links the active conversation's LangSmith trace. The host's
 *  `links.trace` wins when supplied (the console routes it through its own
 *  config); otherwise the URL is derived from the nannos backend's
 *  `/api/v1/config`, so an embedded host needs no wiring at all. Renders only
 *  once the conversation has a message AND a trace target actually resolves —
 *  the URL is derived, never verified, so an empty just-created chat
 *  (client-minted UUID, no LangSmith run yet) must not offer a dead link; a
 *  backend without LangSmith ids yields no link rather than a broken one.
 *  Lives inside the inspector's summary row so it costs the thread no height
 *  and never covers its last line. */
function TraceLink({ conversationId, hasMessages }: { conversationId: string; hasMessages: boolean }) {
  const engine = useChatEngineOptional();
  const hostTrace = engine?.adapter.links.trace;
  const [derivedUrl, setDerivedUrl] = useState<string | null>(null);

  useEffect(() => {
    // A conversation switch must not keep offering the previous chat's target.
    setDerivedUrl(null);
    // The host callback needs no lookup; only the fallback path fetches — and
    // only once the conversation can have a LangSmith run at all.
    if (!engine || hostTrace || !hasMessages) return;
    let alive = true;
    void engine.core
      .resolveTraceUrl(engine.adapter.api.fetch, conversationId)
      .then((url) => {
        if (alive) setDerivedUrl(url);
      });
    return () => {
      alive = false;
    };
  }, [engine, hostTrace, conversationId, hasMessages]);

  if (!hasMessages) return null;
  const open = hostTrace
    ? () => hostTrace(conversationId)
    : derivedUrl
      ? () => window.open(derivedUrl, '_blank', 'noopener,noreferrer')
      : null;
  if (!open) return null;

  return (
    <button
      data-slot="nannos-dev-trace"
      type="button"
      title="Open this conversation's LangSmith trace"
      aria-label="Open LangSmith trace"
      className="flex shrink-0 cursor-pointer select-none items-center gap-1 rounded border border-amber-500/70 bg-amber-500/10 px-1.5 py-0.5 font-medium text-amber-800 hover:bg-amber-500/20 dark:text-amber-300"
      onClick={(e) => {
        // Inside a <summary>: the click must open the trace, not toggle the bar.
        e.preventDefault();
        e.stopPropagation();
        open();
      }}
    >
      <AudioWaveformIcon aria-hidden="true" className="size-3 shrink-0" />
      LangSmith Trace
      <ExternalLinkIcon aria-hidden="true" className="size-2.5 shrink-0" />
    </button>
  );
}

/**
 * Dev mode's ON/OFF switch. Off previews the exact end-user view — the panel
 * keeps NO dev chrome except this bar, which has to stay or there would be no
 * way back.
 */
function DevModeSwitch() {
  const dev = useDevModeControls();
  return (
    // A span, not a label: a <label> forwards clicks to the control it wraps,
    // and this one sits inside a <summary> that would answer them too.
    // `preventDefault` is what stops the bar toggling — the same guard the
    // trace link uses, and the only one that works: the <details> toggle is a
    // default action, which propagation alone does not cancel.
    <span
      data-slot="nannos-dev-switch"
      className="flex shrink-0 items-center"
      title={
        dev.active
          ? 'Dev view on — flip to preview the end-user view'
          : 'Dev view off — showing the end-user view'
      }
      onClick={(event) => {
        event.preventDefault();
        event.stopPropagation();
      }}
    >
      <Switch
        checked={dev.active}
        onCheckedChange={dev.setActive}
        aria-label="Toggle developer view"
        className="h-3.5 w-6 data-[state=checked]:bg-amber-600 [&_[data-slot=switch-thumb]]:size-3"
      />
    </span>
  );
}

export interface DevContextInspectorProps {
  chat: UseNannosChatValue;
  className?: string;
}

export function DevContextInspector({ chat, className }: DevContextInspectorProps) {
  const assistant = useAssistant();
  const engine = useChatEngineOptional();
  const dev = useDevModeControls();

  // The manifest lives outside React — re-read it whenever the registry emits.
  const [registryVersion, bumpRegistry] = useReducer((v: number) => v + 1, 0);
  const registry = engine?.core.registry;
  useEffect(() => registry?.onChange(bumpRegistry), [registry]);
  const clientObjects = useMemo(
    () => registry?.manifest() ?? [],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [registry, registryVersion],
  );

  // Console handle: dev tooling that a <pre> cannot replace — `__nannos.run(d)`
  // fires a directive from the console, `.clientActions.getSnapshot()` reads the
  // log, `.objects()` lists what a target can resolve to. Installed while dev
  // mode is AVAILABLE (this bar is), including while the developer previews the
  // end-user view — that preview is about the panel's chrome, not about pulling
  // the tooling out from under them. It never exists for an end user.
  useEffect(() => {
    if (!engine) return;
    const w = window as unknown as { __nannos?: unknown };
    w.__nannos = {
      core: engine.core,
      clientActions: engine.core.clientActions,
      run: (directive: unknown) => engine.core.runClientAction(directive),
      objects: () => engine.core.registry.keys(),
    };
    return () => {
      delete w.__nannos;
    };
  }, [engine]);

  const [tab, setTab] = useState<TabId>('context');

  const pageContext = assistant.pageContext;
  const conversationKey = engine?.conversations.contextKeyOf(chat.conversationId);
  const layerCount = pageContext ? Object.keys(pageContext).length : 0;

  // Surfaced on the COLLAPSED bar: a failed directive is otherwise invisible
  // until someone thinks to open the inspector.
  const noActions = useMemo<ClientActionLogEntry[]>(() => [], []);
  const actions = useSyncExternalStore(
    engine?.core.clientActions.subscribe ?? (() => () => {}),
    engine?.core.clientActions.getSnapshot ?? (() => noActions),
  );
  const failedActions = actions.filter(
    (a) => a.outcome === 'refused' || a.outcome === 'threw',
  ).length;

  // The wire tab's badge must be honest while that tab is closed, so the count
  // is read here — and this browser's stored record is pulled in here too
  // (hydrate is idempotent, WireSection keeps its own call and stays standalone).
  const noWire = useMemo<WireLogEntry[]>(() => [], []);
  const wireEntries = useSyncExternalStore(
    engine?.wireLog.subscribe ?? (() => () => {}),
    engine?.wireLog.getSnapshot ?? (() => noWire),
  );
  const wireLog = engine?.wireLog;
  useEffect(() => wireLog?.hydrate(chat.conversationId), [wireLog, chat.conversationId]);
  const wireCount = wireEntries.filter(
    (e) => !e.conversationId || e.conversationId === chat.conversationId,
  ).length;

  const counts: Record<TabId, number> = {
    context: clientObjects.length,
    actions: actions.length,
    wire: wireCount,
  };

  // Previewing the end-user view: the bar and nothing else. It cannot go too —
  // it carries the only switch back. Kept to one line, in the same amber, so it
  // reads as dev mode idling rather than as panel chrome.
  if (!dev.active) {
    return (
      <div className={cn('px-1.5', className)}>
        <div
          data-slot="nannos-dev-inspector"
          data-inactive="true"
          className="flex items-center gap-1.5 rounded-lg border border-amber-500/60 bg-amber-500/5 px-2 py-1 text-foreground text-xs"
        >
          <BugIcon
            aria-hidden="true"
            className="size-3 shrink-0 text-amber-700 dark:text-amber-400"
          />
          <span className="font-medium text-amber-800 dark:text-amber-300">dev</span>
          <span className="min-w-0 flex-1 truncate text-muted-foreground">
            off
          </span>
          <DevModeSwitch />
        </div>
      </div>
    );
  }

  return (
    <div className={cn('px-1.5', className)}>
      <details
        data-slot="nannos-dev-inspector"
        className="group rounded-lg border border-amber-500/60 bg-amber-500/5 text-foreground text-xs"
      >
        <summary className="flex cursor-pointer select-none items-center gap-1.5 rounded-lg px-2 py-1 hover:bg-amber-500/10 [&::-webkit-details-marker]:hidden [list-style:none]">
          <BugIcon aria-hidden="true" className="size-3 shrink-0 text-amber-700 dark:text-amber-400" />
          <span className="font-medium text-amber-800 dark:text-amber-300">dev</span>
          <span className="min-w-0 flex-1 truncate">
            {pageContext?.key ?? 'no page context'}
            {' · '}
            {clientObjects.length} object{clientObjects.length === 1 ? '' : 's'}
            {actions.length > 0 && ` · ${actions.length} action${actions.length === 1 ? '' : 's'}`}
          </span>
          {failedActions > 0 && (
            <span className="shrink-0 font-mono font-medium text-red-700 dark:text-red-400">
              {failedActions} failed
            </span>
          )}
          <TraceLink conversationId={chat.conversationId} hasMessages={chat.messages.length > 0} />
          <DevModeSwitch />
          <ChevronDownIcon
            aria-hidden="true"
            className="size-3 shrink-0 transition-transform group-open:rotate-180"
          />
        </summary>
        <div className="flex flex-col gap-2 border-t border-amber-500/50 px-2 py-2">
          <div role="tabpanel" className="flex flex-col gap-3">
            {tab === 'context' && (
              <>
                <Section
                  title="pageContext"
                  hint={pageContext ? `${layerCount} fields · rides every send as metadata.pageContext` : 'no layer provides a key'}
                >
                  <Yaml value={pageContext} />
                </Section>
                <Section title="contextKey" hint="conversation scope">
                  <Yaml value={conversationKey ?? pageContext?.key} />
                </Section>
                <Section title="clientObjects" hint="registry manifest, rides every send">
                  <Yaml value={clientObjects.length ? clientObjects : undefined} />
                </Section>
              </>
            )}
            {tab === 'actions' && <ClientActionsSection />}
            {tab === 'wire' && <WireSection conversationId={chat.conversationId} />}
          </div>
          {/* The switcher sits UNDER the panel, hard against the composer: the
              body above it changes height with the tab, the row itself never
              moves. DOM order is reading order, so it follows what it labels. */}
          <div
            role="tablist"
            aria-label="inspector sections"
            className="-mx-2 mt-1 flex items-center gap-1 border-t border-amber-500/30 px-2 pt-2"
          >
            {TABS.map((t) => (
              <button
                key={t.id}
                type="button"
                role="tab"
                aria-selected={tab === t.id}
                className={cn(
                  'inline-flex items-center gap-1.5 rounded border px-2 py-0.5 font-medium',
                  tab === t.id
                    ? 'border-amber-500/70 bg-amber-500/15 text-amber-800 dark:text-amber-200'
                    : 'border-border bg-background/70 text-muted-foreground hover:bg-muted hover:text-foreground',
                )}
                onClick={() => setTab(t.id)}
              >
                {t.label}
                <span className="font-mono text-[10px] tabular-nums">{counts[t.id]}</span>
                {t.id === 'actions' && failedActions > 0 && (
                  <span className="font-mono text-[10px] text-red-700 dark:text-red-400">
                    !{failedActions}
                  </span>
                )}
              </button>
            ))}
          </div>
        </div>
      </details>
    </div>
  );
}
