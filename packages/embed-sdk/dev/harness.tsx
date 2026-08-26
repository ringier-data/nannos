/**
 * Dev harness — the SDK's live verification page.
 *
 * Two views over the same environment config:
 *  - PANEL — the real `<NannosProvider>` + `<AssistantPanel>` end to end, in
 *    shadow or light-DOM mode, with a dark toggle and a "restyle smoke" that
 *    exercises the host theming contract (accent token + a data-slot override
 *    sheet through `ShadowPortal styles`).
 *  - RAW — the bare transport driven headless (Phase-0 spike view): parts
 *    rendered verbatim, useful for wire debugging.
 *
 * Environments: local (same-origin, vite-proxied to localhost:5001) or a
 * remote backendUrl + pasted bearer token. Remote backends must allowlist this
 * origin (EMBED_ALLOWED_ORIGINS; http://localhost:3000 is allowlisted).
 */
import { StrictMode, useMemo, useRef, useState, useSyncExternalStore } from 'react';
import { createRoot } from 'react-dom/client';
import { Chat, useChat } from '@ai-sdk/react';
import { TransportClient } from '../src/core/client';
import { generateUUID } from '../src/core/protocol';
import type { NannosConfig } from '../src/core';
import { NannosProvider, useAssistant, useNannosPageContext } from '../src/react';
import { AssistantPanel } from '../src/panel';
import {
  A2AChatTransport,
  lastAssistantMessageIsCompleteWithApprovalResponses,
  type NannosUIMessage,
  type SendContext,
} from '../src/transport';

// ---------- environment config (persisted) ----------------------------------

interface EnvConfig {
  name: string;
  backendUrl: string; // '' = same-origin (vite proxy → localhost:5001)
  token: string;
  subAgentId: string;
}

const PRESETS: Record<string, Partial<EnvConfig>> = {
  local: { backendUrl: '' },
  stg: { backendUrl: 'https://console.stg.nannos.ringier.ch' },
  prod: { backendUrl: 'https://console.nannos.ringier.ch' },
  custom: {},
};

const CFG_KEY = 'nannos-harness-config';

function loadCfg(): EnvConfig {
  try {
    return { name: 'local', backendUrl: '', token: '', subAgentId: '', ...JSON.parse(localStorage.getItem(CFG_KEY) ?? '{}') };
  } catch {
    return { name: 'local', backendUrl: '', token: '', subAgentId: '' };
  }
}

function toNannosConfig(cfg: EnvConfig): NannosConfig {
  return {
    ...(cfg.backendUrl && { backendUrl: cfg.backendUrl }),
    ...(cfg.token && { getToken: () => cfg.token }),
    ...(cfg.subAgentId && { subAgentId: cfg.subAgentId }),
  };
}

// ---------- PANEL view --------------------------------------------------------

/** The restyle-smoke override sheet: brand accent + a data-slot-targeted rule. */
const RESTYLE_SHEET = `
:host { --nannos-accent: oklch(0.65 0.25 350); --nannos-radius: 1rem; }
[data-slot="nannos-composer"] { outline: 2px dashed oklch(0.65 0.25 350); outline-offset: 2px; }
`;

/** Fake pages, simulating host navigation: the composer chip must follow, and
 *  each send must carry the current one as `metadata.pageContext`. */
const FAKE_PAGES = ['/campaigns/7', '/campaigns/7/targetings', '/customers/42'];

function PanelControls() {
  const assistant = useAssistant();
  const [page, setPage] = useState('');
  useNannosPageContext(page ? { key: page, title: `Demo — ${page}`, view: { section: page.split('/')[1] ?? '' } } : null);
  return (
    <div style={{ display: 'grid', gap: 4, fontSize: 12 }}>
      <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
        <button onClick={() => assistant.open()}>Open</button>
        <button onClick={() => assistant.open('Draft: what can you do on this page?')}>Open + draft</button>
        <button onClick={() => assistant.open('List your capabilities.', { sendOnOpen: true, displayText: 'Capabilities' })}>
          Open + send (chip)
        </button>
        <button onClick={assistant.close}>Close</button>
        <span>status: {assistant.status} · open: {String(assistant.isOpen)} · pinned: {String(assistant.isPinned)}</span>
      </div>
      <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
        <span>page context:</span>
        <label>
          <input type="radio" checked={page === ''} onChange={() => setPage('')} /> none
        </label>
        {FAKE_PAGES.map((p) => (
          <label key={p}>
            <input type="radio" checked={page === p} onChange={() => setPage(p)} /> {p}
          </label>
        ))}
      </div>
    </div>
  );
}

function PanelView({ cfg }: { cfg: EnvConfig }) {
  const [shadow, setShadow] = useState(true);
  const [dark, setDark] = useState(false);
  const [restyle, setRestyle] = useState(false);
  const [showList, setShowList] = useState(true);
  const config = useMemo(() => toNannosConfig(cfg), [cfg]);

  return (
    <NannosProvider config={config} storagePrefix="harness" defaultPinned={false} shortcut="mod+j">
      <div style={{ display: 'grid', gap: 8 }}>
        <div style={{ display: 'flex', gap: 12, fontSize: 12 }}>
          <label><input type="checkbox" checked={shadow} onChange={(e) => setShadow(e.target.checked)} /> shadow DOM</label>
          <label><input type="checkbox" checked={dark} onChange={(e) => setDark(e.target.checked)} /> dark</label>
          <label><input type="checkbox" checked={restyle} onChange={(e) => setRestyle(e.target.checked)} /> restyle smoke</label>
          <label><input type="checkbox" checked={showList} onChange={(e) => setShowList(e.target.checked)} /> conversation list</label>
        </div>
        <PanelControls />
        <div style={{ height: 640, border: '1px solid #ccc', overflow: 'hidden', background: dark ? '#111' : '#fff' }}>
          <AssistantPanel
            key={`${shadow}-${restyle}`}
            shadow={shadow}
            hostClassName={dark ? 'dark' : undefined}
            styles={restyle ? [RESTYLE_SHEET] : undefined}
            showConversationList={showList}
          />
        </div>
      </div>
    </NannosProvider>
  );
}

// ---------- RAW transport view (Phase-0 spike harness) ------------------------

interface Bundle {
  client: TransportClient;
  transport: A2AChatTransport;
}

function createBundle(cfg: EnvConfig, onLog: (line: string) => void): Bundle {
  const client = new TransportClient(toNannosConfig(cfg));
  client.onError((e) => onLog(`[error:${e.type}] ${e.message}`));

  const sessionId = (() => {
    const k = 'a2a-session-id';
    let v = localStorage.getItem(k);
    if (!v) {
      v = generateUUID();
      localStorage.setItem(k, v);
    }
    return v;
  })();

  const whenReady = async (): Promise<boolean> => {
    if (client.getState().initialized) return true;
    await client.connect();
    const connected = await new Promise<boolean>((resolve) => {
      if (client.getState().socketConnected) return resolve(true);
      const timer = setTimeout(() => {
        dispose();
        resolve(false);
      }, 10_000);
      const dispose = client.subscribe((s) => {
        if (s.socketConnected) {
          clearTimeout(timer);
          dispose();
          resolve(true);
        }
      });
    });
    if (!connected) return false;
    if (client.getState().initialized) return true;
    const base = cfg.backendUrl || window.location.origin;
    const agentUrl = await fetch(`${base}/api/v1/config`, {
      credentials: 'include',
      headers: cfg.token ? { Authorization: `Bearer ${cfg.token}` } : {},
    })
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => j?.orchestratorUrl ?? '')
      .catch(() => '');
    onLog(`[init] agentUrl=${agentUrl || '(none)'}`);
    return client.initializeClient({ agentUrl, model: '' }, sessionId);
  };

  const getSendContext = (): SendContext => ({
    sessionId,
    ...(cfg.subAgentId ? { executeOnlySubAgentId: cfg.subAgentId } : {}),
  });

  const transport = new A2AChatTransport({
    client,
    whenReady,
    getSendContext,
    onTurnEvent: (e) => onLog(`[turn:${e.type}] ${e.conversationId}${e.preview ? ` — ${e.preview.slice(0, 60)}` : ''}`),
  });

  return { client, transport };
}

function RawPart({ part, onApprove, onReject }: { part: NannosUIMessage['parts'][number]; onApprove: (id: string) => void; onReject: (id: string) => void }) {
  switch (part.type) {
    case 'text':
      return <div style={{ whiteSpace: 'pre-wrap', margin: '4px 0' }}>{part.text}</div>;
    case 'data-activity':
      return <div style={{ color: '#666', fontSize: 12 }}>⚙ {part.data.source ? `${part.data.source} › ` : ''}{part.data.text}</div>;
    case 'data-workplan':
      return (
        <div style={{ fontSize: 12, background: '#eef', padding: 6, borderRadius: 6, margin: '4px 0' }}>
          {part.data.todos.map((t, i) => (
            <div key={i}>{t.state === 'completed' ? '☑' : t.state === 'working' ? '◐' : '☐'} {t.source ? `[${t.source}] ` : ''}{t.name}</div>
          ))}
        </div>
      );
    case 'data-agent-thought':
      return (
        <details style={{ fontSize: 12, color: '#557', margin: '4px 0' }} open={!part.data.complete}>
          <summary>💭 {part.data.agent}{part.data.complete ? '' : ' (thinking…)'}</summary>
          <div style={{ whiteSpace: 'pre-wrap' }}>{part.data.text}</div>
        </details>
      );
    case 'dynamic-tool': {
      const state = part.state;
      return (
        <div style={{ border: '1px solid #d0a', borderRadius: 8, padding: 8, margin: '6px 0', fontSize: 13 }}>
          <b>🔧 {part.toolName}</b> <code style={{ fontSize: 11 }}>{state}</code>
          <pre style={{ fontSize: 11, overflow: 'auto', maxHeight: 120 }}>{JSON.stringify(part.input, null, 1)}</pre>
          {state === 'approval-requested' && (
            <span>
              <button onClick={() => onApprove(part.approval!.id)}>✓ Approve</button>{' '}
              <button onClick={() => onReject(part.approval!.id)}>✗ Reject</button>
            </span>
          )}
          {state === 'output-available' && <span style={{ color: 'green' }}>✓ approved & executed</span>}
          {state === 'output-denied' && <span style={{ color: 'crimson' }}>✗ denied</span>}
        </div>
      );
    }
    case 'step-start':
      return <hr style={{ border: 'none', borderTop: '1px dashed #ccc' }} />;
    default:
      return <div style={{ fontSize: 11, color: '#999' }}>[{part.type}]</div>;
  }
}

function RawChatView({ chat, transport }: { chat: Chat<NannosUIMessage>; transport: A2AChatTransport }) {
  const { messages, status, sendMessage, stop, resumeStream, addToolApprovalResponse, setMessages, error } =
    useChat<NannosUIMessage>({ chat });
  const [input, setInput] = useState('');

  const send = () => {
    const text = input.trim();
    if (!text) return;
    setInput('');
    if (transport.hasActiveTurn(chat.id) || status === 'streaming' || status === 'submitted') {
      const steered = transport.steer(chat.id, text);
      if (steered) {
        setMessages((prev) => {
          const last = prev[prev.length - 1];
          return last?.role === 'assistant'
            ? [...prev.slice(0, -1), steered.userMessage, last]
            : [...prev, steered.userMessage];
        });
      }
      return;
    }
    void sendMessage({ text });
  };

  return (
    <div>
      <div style={{ background: '#fff', borderRadius: 8, padding: 12, minHeight: 240 }}>
        {messages.map((m) => (
          <div key={m.id} style={{ margin: '8px 0', paddingLeft: m.role === 'user' ? 40 : 0 }}>
            <div style={{ fontSize: 11, color: '#888' }}>{m.role}{m.metadata?.persistedMessageId ? ` · ${m.metadata.persistedMessageId}` : ''}</div>
            {m.parts.map((p, i) => (
              <RawPart
                key={i}
                part={p}
                onApprove={(id) => void addToolApprovalResponse({ id, approved: true })}
                onReject={(id) => void addToolApprovalResponse({ id, approved: false, reason: JSON.stringify({ v: 1, type: 'reject' }) })}
              />
            ))}
          </div>
        ))}
        {error && <div style={{ color: 'crimson' }}>⚠ {error.message}</div>}
      </div>
      <div style={{ display: 'flex', gap: 6, marginTop: 8 }}>
        <input
          style={{ flex: 1, padding: 8 }}
          value={input}
          placeholder={status === 'streaming' ? 'streaming — sending will STEER' : 'message…'}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && send()}
        />
        <button onClick={send}>Send</button>
        <button onClick={() => void stop()} disabled={status !== 'streaming' && status !== 'submitted'}>Stop</button>
        <button onClick={() => void resumeStream()}>Resume</button>
      </div>
      <div style={{ fontSize: 11, color: '#888', marginTop: 4 }}>status: {status} · conv: {chat.id}</div>
    </div>
  );
}

function RawView({ cfg, log, logLines }: { cfg: EnvConfig; log: (l: string) => void; logLines: string[] }) {
  const [bundle, setBundle] = useState<Bundle | null>(null);
  const [convId, setConvId] = useState<string>(() => generateUUID());
  const chatsRef = useRef(new Map<string, Chat<NannosUIMessage>>());

  const connState = useSyncExternalStore(
    (cb) => bundle?.client.subscribe(cb) ?? (() => {}),
    () => (bundle ? JSON.stringify(bundle.client.getState()) : 'not connected'),
  );

  const connect = () => {
    bundle?.transport.destroy();
    bundle?.client.disconnect();
    chatsRef.current.clear();
    const b = createBundle(cfg, log);
    void b.client.connect();
    setBundle(b);
    setConvId(generateUUID());
  };

  const chat = useMemo(() => {
    if (!bundle) return null;
    let c = chatsRef.current.get(convId);
    if (!c) {
      c = new Chat<NannosUIMessage>({
        id: convId,
        transport: bundle.transport,
        messages: [],
        sendAutomaticallyWhen: lastAssistantMessageIsCompleteWithApprovalResponses,
      });
      chatsRef.current.set(convId, c);
    }
    return c;
  }, [bundle, convId]);

  return (
    <div style={{ display: 'grid', gap: 8 }}>
      <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
        <button onClick={connect}>Connect (raw)</button>
        <button onClick={() => bundle?.client.reauth()} disabled={!bundle}>Reconnect socket</button>
        <button onClick={() => setConvId(generateUUID())} disabled={!bundle}>New conversation</button>
        <code style={{ fontSize: 11 }}>{connState}</code>
      </div>
      {chat && bundle && <RawChatView key={chat.id} chat={chat} transport={bundle.transport} />}
      <pre style={{ fontSize: 11, color: '#666', background: '#eee', padding: 8, borderRadius: 6 }}>{logLines.join('\n') || '(log)'}</pre>
    </div>
  );
}

// ---------- shell ---------------------------------------------------------------

function App() {
  const [cfg, setCfg] = useState<EnvConfig>(loadCfg);
  const [applied, setApplied] = useState<EnvConfig | null>(null);
  const [view, setView] = useState<'panel' | 'raw'>('panel');
  const [version, setVersion] = useState(0);
  const [logLines, setLogLines] = useState<string[]>([]);
  const log = (line: string) => setLogLines((prev) => [...prev.slice(-30), `${new Date().toISOString().slice(11, 19)} ${line}`]);

  const apply = () => {
    localStorage.setItem(CFG_KEY, JSON.stringify(cfg));
    setApplied({ ...cfg });
    setVersion((v) => v + 1);
  };

  return (
    <div style={{ maxWidth: 860, margin: '0 auto', padding: 16 }}>
      <h2>embed-sdk harness</h2>
      <div style={{ background: '#fff', borderRadius: 8, padding: 12, marginBottom: 12, display: 'grid', gap: 6 }}>
        <div>
          {Object.keys(PRESETS).map((name) => (
            <label key={name} style={{ marginRight: 12 }}>
              <input
                type="radio"
                checked={cfg.name === name}
                onChange={() => setCfg({ ...cfg, name, ...(PRESETS[name].backendUrl !== undefined ? { backendUrl: PRESETS[name].backendUrl! } : {}) })}
              />{' '}
              {name}
            </label>
          ))}
          <span style={{ marginLeft: 24 }}>
            view:{' '}
            {(['panel', 'raw'] as const).map((v) => (
              <label key={v} style={{ marginRight: 8 }}>
                <input type="radio" checked={view === v} onChange={() => setView(v)} /> {v}
              </label>
            ))}
          </span>
        </div>
        <input placeholder="backendUrl (empty = same-origin proxy → localhost:5001)" value={cfg.backendUrl} onChange={(e) => setCfg({ ...cfg, backendUrl: e.target.value })} />
        <input placeholder="bearer token (paste; empty = cookie auth)" value={cfg.token} onChange={(e) => setCfg({ ...cfg, token: e.target.value })} />
        <input placeholder="subAgentId (optional, execute-only)" value={cfg.subAgentId} onChange={(e) => setCfg({ ...cfg, subAgentId: e.target.value })} />
        <div>
          <button onClick={apply}>Apply & (re)mount</button>
        </div>
      </div>
      {applied && view === 'panel' && <PanelView key={`p-${version}`} cfg={applied} />}
      {applied && view === 'raw' && <RawView key={`r-${version}`} cfg={applied} log={log} logLines={logLines} />}
      {!applied && <div style={{ color: '#777' }}>Pick an environment and press “Apply”.</div>}
    </div>
  );
}

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
