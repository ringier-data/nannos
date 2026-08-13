/**
 * Dev harness — `npm run dev`.
 *
 * Renders the real ChatApp over a mock host page with fixture state instead of a
 * live socket, so the widget's look and its awkward states (wide tables, live
 * progress, approval card, dark mode) can be iterated on without a backend.
 *
 * Not shipped: excluded from the library build (see vite.config.ts entries) and
 * only reachable through vite.dev.config.ts.
 */
import { useEffect, useMemo, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { Toaster } from 'sonner';
import { createNannos } from '../src/core';
import { HostAdapterProvider } from '../src/ui/adapter';
import { ChatApp } from '../src/ui/chat/ChatApp';
import { ChatContext, SocketContext } from '../src/ui/chat/contexts';
import type { Message } from '../src/ui/chat/types';
import {
  FIXTURE_CONVERSATIONS,
  FIXTURE_ERROR_MESSAGE,
  FIXTURE_INTERRUPT,
  FIXTURE_MESSAGES,
  FIXTURE_WORKING_STEPS,
} from './fixtures';
import '../src/ui/theme.css';

type Scenario = 'answers' | 'working' | 'approval' | 'error' | 'empty' | 'loading';

const SCENARIOS: Array<{ id: Scenario; label: string }> = [
  { id: 'answers', label: 'Answers + tables' },
  { id: 'working', label: 'Working (live)' },
  { id: 'approval', label: 'Approval' },
  { id: 'error', label: 'Error' },
  { id: 'empty', label: 'Empty' },
  { id: 'loading', label: 'Loading' },
];

const noop = () => {};
const unsubscribe = () => () => {};

function useFixtureChat(scenario: Scenario) {
  return useMemo(() => {
    const isWorking = scenario === 'working';
    let messages: Message[] = FIXTURE_MESSAGES;
    if (scenario === 'empty') messages = [];
    else if (scenario === 'error') messages = [...FIXTURE_MESSAGES, FIXTURE_ERROR_MESSAGE];
    else if (scenario === 'approval' || isWorking) messages = FIXTURE_MESSAGES.slice(0, 3);

    return {
      conversations: FIXTURE_CONVERSATIONS,
      activeConversationId: 'c1',
      messages,
      tasks: [],
      settings: null,
      userSettings: null,
      isLoadingConversations: false,
      isLoadingMessages: scenario === 'loading',
      activeConversationReadOnly: false,
      isConnected: true,
      isWaiting: isWorking,
      streamingMessage: null,
      liveWorkingSteps: isWorking ? FIXTURE_WORKING_STEPS : [],
      liveSubagentThoughts: [],
      liveStatusHistory: [],
      liveTimeline: [],
      pendingInterrupt: scenario === 'approval' ? FIXTURE_INTERRUPT : null,
      pendingFeedbackRequest: null,
      createConversation: noop,
      selectConversation: noop,
      sendMessage: noop,
      sendSilentMessage: noop,
      interruptTask: noop,
      dismissInterrupt: noop,
      dismissFeedbackRequest: noop,
      updateSettings: async () => true,
      loadConversations: async () => {},
    };
  }, [scenario]);
}

const FIXTURE_SOCKET = {
  isConnected: true,
  isSocketReady: true,
  agentInfo: {
    name: 'Nannos Assistant',
    description: 'Campaign operations assistant',
    version: '1.4.0',
    skills: [{ id: 'health', name: 'campaign-health-check', description: 'Audit pacing, budget and creatives' }],
    capabilities: { streaming: true },
  },
  initializeClient: async () => true,
  sendMessage: noop,
  cancelTask: noop,
  onAgentResponse: unsubscribe,
  subscribeConversation: noop,
  unsubscribeConversation: noop,
  onConversationSnapshot: unsubscribe,
  onEvent: unsubscribe,
};

const FIXTURE_ADAPTER = {
  api: {
    getConversationFeedback: async () => [],
    submitConversationFeedback: async () => true,
    uploadFiles: async () => [],
  },
  agentName: 'Nannos Assistant',
};

/** Stand-in for the host page the widget floats over. */
function MockHostPage() {
  return (
    <div style={{ padding: '32px 40px', fontFamily: 'system-ui, sans-serif' }}>
      <h1 style={{ fontSize: 20, margin: '0 0 4px' }}>Host application</h1>
      <p style={{ margin: 0, color: '#64748B', fontSize: 14 }}>
        Any page the assistant is embedded into. Present so panel contrast, shadow and z-index can be judged.
      </p>
      <div style={{ marginTop: 24, display: 'grid', gap: 8, maxWidth: 620 }}>
        {['Campaigns', 'Line items', 'Creatives', 'Reports'].map((row) => (
          <div
            key={row}
            style={{
              border: '1px solid #E5E9F0',
              borderRadius: 10,
              padding: '14px 16px',
              background: '#fff',
              fontSize: 14,
            }}
          >
            {row}
          </div>
        ))}
      </div>
    </div>
  );
}

// States are addressable (?scenario=approval&dark=1&expanded=1) so a specific one
// can be linked, reloaded, or screenshotted by a headless browser.
const params = new URLSearchParams(window.location.search);
const initialScenario = (params.get('scenario') as Scenario | null) ?? 'answers';

function Harness() {
  const [scenario, setScenario] = useState<Scenario>(initialScenario);
  const [dark, setDark] = useState(params.has('dark'));
  const chat = useFixtureChat(scenario);

  // A real core (never connected — no backendUrl, and we never call connect) so the
  // header's expand/close buttons drive genuine core state instead of a stub, and
  // the panel below mirrors it exactly as NannosWidget does.
  const core = useMemo(() => createNannos({}), []);
  const [open, setOpen] = useState(true);
  const [expanded, setExpanded] = useState(false);
  useEffect(() => core.onOpenChange(setOpen), [core]);
  useEffect(() => core.onExpandChange(setExpanded), [core]);
  useEffect(() => {
    core.open();
    if (params.has('expanded')) core.expand();
  }, [core]);

  return (
    <div style={{ minHeight: '100vh', background: dark ? '#0F172A' : '#F1F5F9' }}>
      <div
        style={{
          position: 'fixed',
          top: 16,
          left: 16,
          zIndex: 10,
          display: 'flex',
          gap: 6,
          flexWrap: 'wrap',
          alignItems: 'center',
          fontFamily: 'system-ui, sans-serif',
          fontSize: 13,
        }}
      >
        {SCENARIOS.map((s) => (
          <button
            key={s.id}
            onClick={() => setScenario(s.id)}
            style={{
              padding: '6px 10px',
              borderRadius: 999,
              border: '1px solid #CBD5E1',
              cursor: 'pointer',
              background: scenario === s.id ? '#6D28D9' : '#fff',
              color: scenario === s.id ? '#fff' : '#1E293B',
            }}
          >
            {s.label}
          </button>
        ))}
        <button
          onClick={() => setDark((v) => !v)}
          style={{ padding: '6px 10px', borderRadius: 999, border: '1px solid #CBD5E1', cursor: 'pointer' }}
        >
          {dark ? 'Light' : 'Dark'}
        </button>
        <button
          onClick={() => core.toggleExpanded()}
          style={{ padding: '6px 10px', borderRadius: 999, border: '1px solid #CBD5E1', cursor: 'pointer' }}
        >
          {expanded ? '400×640' : 'Expanded'}
        </button>
        {!open && (
          <button
            onClick={() => core.open()}
            style={{ padding: '6px 10px', borderRadius: 999, border: '1px solid #CBD5E1', cursor: 'pointer' }}
          >
            Reopen panel
          </button>
        )}
      </div>

      <MockHostPage />

      {open && (
        <div
          className={`nannos-tokens nannos-chat${dark ? ' dark' : ''}`}
          style={{
            position: 'fixed',
            right: 24,
            bottom: 24,
            width: expanded ? 'min(720px, calc(100vw - 48px))' : 400,
            height: expanded ? 'min(900px, calc(100vh - 112px))' : 640,
            maxHeight: 'calc(100vh - 48px)',
            borderRadius: 12,
            overflow: 'hidden',
            boxShadow: '0 12px 48px rgba(0,0,0,0.22)',
            transition: 'width 180ms ease, height 180ms ease',
          }}
        >
          <HostAdapterProvider core={core} adapter={FIXTURE_ADAPTER}>
            <SocketContext.Provider value={FIXTURE_SOCKET}>
              <ChatContext.Provider value={chat}>
                <ChatApp compact />
              </ChatContext.Provider>
            </SocketContext.Provider>
          </HostAdapterProvider>
        </div>
      )}
      <Toaster position="top-right" />
    </div>
  );
}

createRoot(document.getElementById('root')!).render(<Harness />);
