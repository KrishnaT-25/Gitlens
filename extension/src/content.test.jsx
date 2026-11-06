// @vitest-environment jsdom
// @vitest-environment-options {"url":"https://github.com/owner/repo"}
import { expect, test, vi } from 'vitest';
import { api, send } from './bridge.js';

vi.mock('./bridge.js', () => ({
  send: vi.fn(async () => ({ signedIn: false })),
  api: vi.fn(),
}));

const tick = () => new Promise(resolve => setTimeout(resolve, 10));

test('one widget follows GitHub navigation and closes with Escape', async () => {
  const addListener = vi.fn();
  globalThis.chrome = { runtime: { onMessage: { addListener, removeListener: vi.fn() } } };
  const attach = Element.prototype.attachShadow;
  vi.spyOn(Element.prototype, 'attachShadow').mockImplementation(function () { return attach.call(this, { mode: 'open' }); });
  await import('./content.jsx');
  await tick();
  let host = document.querySelector('#gitlens-root');
  expect(host).not.toBeNull();
  expect(document.querySelectorAll('#gitlens-root')).toHaveLength(1);
  host.shadowRoot.querySelector('.launcher').click();
  await tick();
  expect(host.shadowRoot.querySelector('.panel')).not.toBeNull();
  addListener.mock.calls[0][0]({ type: 'API_STATUS', status: 'waking' });
  await tick();
  expect(host.shadowRoot.querySelector('[role="status"]').textContent).toContain('Waking up');
  addListener.mock.calls[0][0]({ type: 'API_STATUS', status: 'ready' });
  await tick();
  expect(host.shadowRoot.querySelector('[role="status"]')).toBeNull();
  host.shadowRoot.querySelector('.panel').dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await tick();
  expect(host.shadowRoot.querySelector('.panel')).toBeNull();
  history.pushState({}, '', '/settings/profile');
  dispatchEvent(new Event('turbo:load'));
  await tick();
  expect(document.querySelector('#gitlens-root')).toBeNull();
  history.pushState({}, '', '/owner/repo/blob/main/app.py');
  dispatchEvent(new Event('turbo:load'));
  await tick();
  host = document.querySelector('#gitlens-root');
  expect(host).not.toBeNull();
  expect(document.querySelectorAll('#gitlens-root')).toHaveLength(1);
  vi.restoreAllMocks();
});

test('shows the resolved ref and full SHA through polling and blocks PR submission', async () => {
  const attach = Element.prototype.attachShadow;
  vi.spyOn(Element.prototype, 'attachShadow').mockImplementation(function () { return attach.call(this, { mode: 'open' }); });
  send.mockResolvedValue({ signedIn: true });
  const sha = 'a'.repeat(40);
  api.mockImplementation(async (method, path) => {
    if (method === 'POST' && path === '/api/repositories') return { snapshotId: 'snapshot', sha, status: 'queued', ref: 'refs/heads/feature/search' };
    if (path.endsWith('/status')) return { snapshotId: 'snapshot', sha, status: 'ready' };
    if (path.endsWith('/summary')) return { summary: { purpose: 'Selected branch' } };
    throw new Error(`Unexpected call ${path}`);
  });
  history.pushState({}, '', '/owner/repo/tree/feature/search');
  dispatchEvent(new Event('turbo:load'));
  await tick();
  let host = document.querySelector('#gitlens-root');
  host.shadowRoot.querySelector('.launcher').click();
  await vi.waitFor(() => expect(host.shadowRoot.querySelector('.summary')).not.toBeNull());
  expect(host.shadowRoot.querySelector('.repo-sha').textContent).toContain('refs/heads/feature/search');
  expect(host.shadowRoot.querySelector('.repo-sha').title).toContain(sha);
  expect(api).toHaveBeenCalledWith('POST', '/api/repositories', { url: 'https://github.com/owner/repo/tree/feature/search' });

  vi.clearAllMocks();
  history.pushState({}, '', '/owner/repo/pull/12');
  dispatchEvent(new Event('turbo:load'));
  await tick();
  host = document.querySelector('#gitlens-root');
  host.shadowRoot.querySelector('.launcher').click();
  await tick();
  expect(host.shadowRoot.querySelector('[role="alert"]').textContent).toContain('Pull request');
  expect(api).not.toHaveBeenCalled();
  expect(send).not.toHaveBeenCalled();
  vi.restoreAllMocks();
});
