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
 *     one JSON array for a bug report (see transport/wire-log.ts).
 *
 * Deliberately NOT translated — this is tooling for the integrating developer,
 * never end-user chrome. Enabled via panel/dev-mode.tsx.
 */
import { useEffect, useMemo, useReducer, useState, useSyncExternalStore } from 'react';
import {
  ArrowDownLeftIcon,
  ArrowUpRightIcon,
  AudioWaveformIcon,
  BugIcon,
  CheckIcon,
  ChevronDownIcon,
  CopyIcon,
  Trash2Icon,
} from 'lucide-react';
import { cn } from '../../lib/utils';
import { useAssistant } from '../../react';
import type { WireLogEntry } from '../../transport';
import { useChatEngineOptional } from '../engine';
import type { UseNannosChatValue } from '../hooks/use-nannos-chat';

function Section({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="flex items-baseline gap-2 px-0.5 pb-0.5">
        <span className="font-medium">{title}</span>
        {hint && <span className="truncate opacity-70">{hint}</span>}
      </div>
      {children}
    </div>
  );
}

function Json({ value, className }: { value: unknown; className?: string }) {
  return (
    <pre
      className={cn(
        'max-h-48 overflow-auto rounded-md bg-muted/60 p-2 font-mono text-[11px] leading-snug',
        className,
      )}
    >
      {value === undefined || value === null ? '—' : JSON.stringify(value, null, 2)}
    </pre>
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
  return (
    <details onToggle={(e) => setOpen((e.target as HTMLDetailsElement).open)}>
      <summary className="flex cursor-pointer select-none items-center gap-1.5 rounded px-1 py-0.5 font-mono text-[11px] hover:bg-muted/60 [&::-webkit-details-marker]:hidden [list-style:none]">
        <span className="shrink-0 opacity-60">{timeOf(entry.ts)}</span>
        <Dir
          aria-label={entry.dir === 'in' ? 'received' : 'sent'}
          className={cn('size-3 shrink-0', entry.dir === 'in' ? 'text-sky-600' : 'text-emerald-600')}
        />
        <span className="min-w-0 truncate">{entry.label}</span>
      </summary>
      {open && <Json value={entry.payload} className="max-h-64" />}
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
      className="inline-flex items-center gap-1 rounded px-1 py-0.5 hover:bg-muted/60 disabled:opacity-40 disabled:hover:bg-transparent"
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

/** The received/sent raw traffic of the ACTIVE conversation, newest first. */
function WireSection({ conversationId }: { conversationId: string }) {
  const engine = useChatEngineOptional();
  const empty = useMemo<WireLogEntry[]>(() => [], []);
  const entries = useSyncExternalStore(
    engine?.wireLog.subscribe ?? (() => () => {}),
    engine?.wireLog.getSnapshot ?? (() => empty),
  );
  // Chronological for the clipboard, newest-first for the eye.
  const mine = entries.filter((e) => !e.conversationId || e.conversationId === conversationId);
  const shown = [...mine].reverse();

  return (
    <Section
      title="wire"
      hint={`${shown.length} events this conversation · raw socket traffic, newest first`}
    >
      <div className="flex max-h-64 flex-col overflow-y-scroll rounded-md border border-border/60 [&::-webkit-scrollbar-thumb]:rounded-full [&::-webkit-scrollbar-thumb]:bg-border [&::-webkit-scrollbar-track]:bg-transparent [&::-webkit-scrollbar]:w-2">
        {shown.length === 0 ? (
          <span className="p-2 opacity-60">no traffic yet — send a message</span>
        ) : (
          shown.map((entry) => <WireRow key={entry.seq} entry={entry} />)
        )}
      </div>
      <div className="mt-1 flex items-center gap-2">
        <CopyWireButton entries={mine} />
        <button
          type="button"
          className="inline-flex items-center gap-1 rounded px-1 py-0.5 hover:bg-muted/60"
          onClick={() => engine?.wireLog.clear()}
        >
          <Trash2Icon className="size-3" /> clear
        </button>
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
      className="flex shrink-0 border border-amber-500/50 cursor-pointer select-none items-center gap-1 rounded px-1 py-0.5 hover:bg-amber-500/10 hover:underline"
      onClick={(e) => {
        // Inside a <summary>: the click must open the trace, not toggle the bar.
        e.preventDefault();
        e.stopPropagation();
        open();
      }}
    >
      <AudioWaveformIcon aria-hidden="true" className="size-3 shrink-0" />
      Open in LangChain
    </button>
  );
}

export interface DevContextInspectorProps {
  chat: UseNannosChatValue;
  className?: string;
}

export function DevContextInspector({ chat, className }: DevContextInspectorProps) {
  const assistant = useAssistant();
  const engine = useChatEngineOptional();

  // The manifest lives outside React — re-read it whenever the registry emits.
  const [registryVersion, bumpRegistry] = useReducer((v: number) => v + 1, 0);
  const registry = engine?.core.registry;
  useEffect(() => registry?.onChange(bumpRegistry), [registry]);
  const clientObjects = useMemo(
    () => registry?.manifest() ?? [],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [registry, registryVersion],
  );

  const pageContext = assistant.pageContext;
  const conversationKey = engine?.conversations.contextKeyOf(chat.conversationId);
  const layerCount = pageContext ? Object.keys(pageContext).length : 0;

  return (
    <div className={cn('px-1.5', className)}>
      <details
        data-slot="nannos-dev-inspector"
        className="group rounded-lg border border-amber-500/50 bg-amber-500/5 text-muted-foreground text-xs"
      >
        <summary className="flex cursor-pointer select-none items-center gap-1.5 px-2 py-1 [&::-webkit-details-marker]:hidden [list-style:none]">
          <BugIcon aria-hidden="true" className="size-3 shrink-0 text-amber-600" />
          <span className="font-medium text-amber-700 dark:text-amber-500">dev</span>
          <span className="min-w-0 flex-1 truncate">
            {pageContext?.key ?? 'no page context'}
            {' · '}
            {clientObjects.length} object{clientObjects.length === 1 ? '' : 's'}
          </span>
          <TraceLink conversationId={chat.conversationId} hasMessages={chat.messages.length > 0} />
          <ChevronDownIcon
            aria-hidden="true"
            className="size-3 shrink-0 transition-transform group-open:rotate-180"
          />
        </summary>
        <div className="flex flex-col gap-2 border-t border-amber-500/30 px-2 py-2">
          <Section
            title="pageContext"
            hint={pageContext ? `${layerCount} fields · rides every send as metadata.pageContext` : 'no layer provides a key'}
          >
            <Json value={pageContext} />
          </Section>
          <Section title="contextKey" hint="conversation scope">
            <Json value={conversationKey ?? pageContext?.key} />
          </Section>
          <Section title="clientObjects" hint="registry manifest, rides every send">
            <Json value={clientObjects.length ? clientObjects : undefined} />
          </Section>
          <WireSection conversationId={chat.conversationId} />
        </div>
      </details>
    </div>
  );
}
