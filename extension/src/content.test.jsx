// @vitest-environment jsdom
// @vitest-environment-options {"url":"https://github.com/owner/repo"}
import { expect, test, vi } from 'vitest';

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
