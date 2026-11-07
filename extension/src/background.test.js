import { afterEach, beforeEach, expect, test, vi } from 'vitest';

let listener;
let timeout;

beforeEach(async () => {
  vi.resetModules();
  vi.stubGlobal('__API_BASE_URL__', 'https://api.example.test');
  vi.stubGlobal('chrome', {
    storage: { local: { get: vi.fn(async () => ({ token: 'test-session' })) } },
    runtime: { onMessage: { addListener: fn => { listener = fn; } } },
  });
  timeout = vi.spyOn(AbortSignal, 'timeout');
  vi.stubGlobal('fetch', vi.fn(async url => new Response(JSON.stringify(url.endsWith('/readyz') ? { ok: true } : { answer: 'ok' }), {
    status: 200, headers: { 'content-type': 'application/json' },
  })));
  await import('./background.js');
});

afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

const call = (method, path) => new Promise(resolve => listener({ type: 'API', method, path }, {}, resolve));

test('chat waits 70 seconds while readiness and other calls retain their own timeouts', async () => {
  expect(await call('POST', '/api/snapshots/snapshot/chat')).toEqual({ ok: true, data: { answer: 'ok' } });
  expect(timeout.mock.calls.map(([ms]) => ms)).toEqual([20_000, 70_000]);
  timeout.mockClear();
  await call('GET', '/api/snapshots/snapshot/status');
  expect(timeout).toHaveBeenCalledExactlyOnceWith(28_000);
});

test('server chat_timeout errors reach the widget intact', async () => {
  fetch.mockImplementation(async url => new Response(JSON.stringify(url.endsWith('/readyz') ? { ok: true }
    : { error: { code: 'chat_timeout', message: 'Repository analysis timed out. Please try a more focused question.' } }), {
    status: url.endsWith('/readyz') ? 200 : 504, headers: { 'content-type': 'application/json' },
  }));
  const result = await call('POST', '/api/snapshots/snapshot/chat');
  expect(result.ok).toBe(false);
  expect(result.error.message).toContain('Repository analysis timed out');
});

test('client timeout also covers reading the response body', async () => {
  const controller = new AbortController();
  timeout.mockImplementation(ms => ms === 70_000 ? controller.signal : new AbortController().signal);
  fetch.mockImplementation(async url => {
    if (url.endsWith('/readyz')) return new Response('{"ok":true}', { headers: { 'content-type': 'application/json' } });
    return { ok: true, body: { getReader: () => ({ read: async () => {
      controller.abort();
      throw new DOMException('Timed out', 'TimeoutError');
    } }) } };
  });
  const result = await call('POST', '/api/snapshots/snapshot/chat');
  expect(result).toEqual({ ok: false, error: { message: 'The chat request timed out. Please try a more focused question.' } });
});
