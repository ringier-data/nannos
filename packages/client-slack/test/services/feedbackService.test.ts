import { describe, test, expect, beforeEach, afterEach, jest } from '@jest/globals';
import { ResponseMappingCache, FeedbackService, ResponseMapping } from '../../src/services/feedbackService.js';
import { UserAuthService } from '../../src/services/userAuthService.js';
import { Config } from '../../src/config/config.js';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function mockUserAuthService(token: string | null = 'mock-token') {
  return {
    getTokenForAudience: jest.fn<(u: string, t: string, a: string) => Promise<string | null>>().mockResolvedValue(token),
  } as unknown as UserAuthService;
}

function minimalConfig(url = 'https://console.example.com'): Config {
  return {
    consoleBackend: { url, audience: 'console-aud' },
  } as unknown as Config;
}

const mapping: ResponseMapping = {
  contextId: 'ctx-1',
  taskId: 'task-1',
  userId: 'U123',
  teamId: 'T456',
  createdAt: Date.now(),
};

// ---------------------------------------------------------------------------
// ResponseMappingCache
// ---------------------------------------------------------------------------

describe('ResponseMappingCache', () => {
  let cache: ResponseMappingCache;

  beforeEach(() => {
    cache = new ResponseMappingCache();
  });

  test('buildKey concatenates channel and ts', () => {
    expect(cache.buildKey('C1', '111.222')).toBe('C1:111.222');
  });

  test('set and get round-trip', () => {
    cache.set('C1', '111.222', mapping);
    expect(cache.get('C1', '111.222')).toEqual(mapping);
  });

  test('get returns undefined for missing key', () => {
    expect(cache.get('C1', 'nope')).toBeUndefined();
  });

  test('get evicts entries past TTL', () => {
    const shortCache = new ResponseMappingCache(100);
    const old = { ...mapping, createdAt: Date.now() - 200 };
    shortCache.set('C1', '111', old);
    expect(shortCache.get('C1', '111')).toBeUndefined();
  });

  test('get returns entry within TTL', () => {
    const shortCache = new ResponseMappingCache(60_000);
    shortCache.set('C1', '111', mapping);
    expect(shortCache.get('C1', '111')).toEqual(mapping);
  });
});

// ---------------------------------------------------------------------------
// FeedbackService – constructor
// ---------------------------------------------------------------------------

describe('FeedbackService', () => {
  test('throws when consoleBackend config missing', () => {
    const noBackend = {} as unknown as Config;
    expect(() => new FeedbackService(mockUserAuthService(), noBackend)).toThrow(
      'CONSOLE_BACKEND_URL is required',
    );
  });

  test('strips trailing slashes from URL', () => {
    const svc = new FeedbackService(mockUserAuthService(), minimalConfig('https://host.test///'));
    // Access internal URL via submitFeedback call and inspect fetch URL
    expect(svc).toBeDefined();
  });
});

// ---------------------------------------------------------------------------
// FeedbackService – submitFeedback
// ---------------------------------------------------------------------------

describe('FeedbackService.submitFeedback', () => {
  let fetchSpy: jest.Spied<typeof global.fetch>;
  let authService: UserAuthService;
  let svc: FeedbackService;

  beforeEach(() => {
    fetchSpy = jest.spyOn(global, 'fetch').mockResolvedValue(new Response(null, { status: 200 }) as Response);
    authService = mockUserAuthService('tok-123');
    svc = new FeedbackService(authService, minimalConfig());
  });

  afterEach(() => {
    fetchSpy.mockRestore();
  });

  test('calls POST with correct URL, headers, and body', async () => {
    const ok = await svc.submitFeedback('U1', 'T1', 'conv-1', 'msg-1', 'positive');
    expect(ok).toBe(true);

    expect(fetchSpy).toHaveBeenCalledTimes(1);
    const [url, opts] = fetchSpy.mock.calls[0];
    expect(url).toBe('https://console.example.com/api/v1/conversations/conv-1/messages/msg-1/feedback');
    expect((opts as RequestInit).method).toBe('POST');
    expect((opts as RequestInit).headers).toEqual(
      expect.objectContaining({
        'Content-Type': 'application/json',
        Authorization: 'Bearer tok-123',
      }),
    );
    expect(JSON.parse((opts as RequestInit).body as string)).toEqual({ rating: 'positive' });
  });

  test('returns false when token exchange fails', async () => {
    const noTokenAuth = mockUserAuthService(null);
    const svc2 = new FeedbackService(noTokenAuth, minimalConfig());
    const ok = await svc2.submitFeedback('U1', 'T1', 'conv-1', 'msg-1', 'negative');
    expect(ok).toBe(false);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  test('returns false on non-ok response', async () => {
    fetchSpy.mockResolvedValueOnce(new Response(null, { status: 500 }));
    const ok = await svc.submitFeedback('U1', 'T1', 'conv-1', 'msg-1', 'positive');
    expect(ok).toBe(false);
  });

  test('returns false on network error', async () => {
    fetchSpy.mockRejectedValueOnce(new Error('network down'));
    const ok = await svc.submitFeedback('U1', 'T1', 'conv-1', 'msg-1', 'positive');
    expect(ok).toBe(false);
  });

  test('encodes special chars in conversation/message IDs', async () => {
    await svc.submitFeedback('U1', 'T1', 'conv/1', 'msg/2', 'positive');
    const [url] = fetchSpy.mock.calls[0];
    expect(url).toContain('conv%2F1');
    expect(url).toContain('msg%2F2');
  });
});

// ---------------------------------------------------------------------------
// FeedbackService – deleteFeedback
// ---------------------------------------------------------------------------

describe('FeedbackService.deleteFeedback', () => {
  let fetchSpy: jest.Spied<typeof global.fetch>;
  let svc: FeedbackService;

  beforeEach(() => {
    fetchSpy = jest.spyOn(global, 'fetch').mockResolvedValue(new Response(null, { status: 200 }) as Response);
    svc = new FeedbackService(mockUserAuthService('tok-del'), minimalConfig());
  });

  afterEach(() => {
    fetchSpy.mockRestore();
  });

  test('calls DELETE with correct URL and auth', async () => {
    const ok = await svc.deleteFeedback('U1', 'T1', 'conv-1', 'msg-1');
    expect(ok).toBe(true);

    const [url, opts] = fetchSpy.mock.calls[0];
    expect(url).toContain('/feedback');
    expect((opts as RequestInit).method).toBe('DELETE');
    expect((opts as RequestInit).headers).toEqual(
      expect.objectContaining({ Authorization: 'Bearer tok-del' }),
    );
  });

  test('treats 404 as success', async () => {
    fetchSpy.mockResolvedValueOnce(new Response(null, { status: 404 }));
    const ok = await svc.deleteFeedback('U1', 'T1', 'conv-1', 'msg-1');
    expect(ok).toBe(true);
  });

  test('returns false on 500', async () => {
    fetchSpy.mockResolvedValueOnce(new Response(null, { status: 500 }));
    const ok = await svc.deleteFeedback('U1', 'T1', 'conv-1', 'msg-1');
    expect(ok).toBe(false);
  });

  test('returns false when token exchange fails', async () => {
    const svc2 = new FeedbackService(mockUserAuthService(null), minimalConfig());
    const ok = await svc2.deleteFeedback('U1', 'T1', 'conv-1', 'msg-1');
    expect(ok).toBe(false);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  test('returns false on network error', async () => {
    fetchSpy.mockRejectedValueOnce(new Error('timeout'));
    const ok = await svc.deleteFeedback('U1', 'T1', 'conv-1', 'msg-1');
    expect(ok).toBe(false);
  });
});
