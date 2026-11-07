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
  const nextSha = 'b'.repeat(40);
  let resolveLateAnswer;
  const longAnswer = 'x'.repeat(6000);
  let initialPolls = 0;
  let failedFollowUp = false;
  api.mockImplementation(async (method, path, request) => {
    if (method === 'POST' && path === '/api/repositories') {
      if (request.url.includes('/commit/')) return { snapshotId: 'new-snapshot', sha: nextSha, status: 'ready' };
      if (request.url.endsWith('/another')) return { snapshotId: 'other-repository', sha, status: 'ready' };
      return { snapshotId: 'snapshot', sha, status: 'queued', ref: 'refs/heads/feature/search' };
    }
    if (path.endsWith('/status')) return { snapshotId: 'snapshot', sha, status: 'ready' };
    if (path.endsWith('/summary')) return { summary: { purpose: path.includes('new-snapshot') ? 'New commit' : 'Selected branch',
      suggestedQuestions: ['How does it work?', 'explain me here', 'Slow question'] } };
    if (path.endsWith('/chat')) {
      if (request.question === 'How does it work?' && initialPolls++ === 0) return { status: 'running' };
      if (request.question === 'Slow question') return new Promise(resolve => { resolveLateAnswer = resolve; });
      if (request.question === 'explain me here' && !failedFollowUp) {
        failedFollowUp = true;
        throw new Error('Invalid answer');
      }
      return { answer: longAnswer, citations: [] };
    }
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

  host.shadowRoot.querySelectorAll('.suggestion')[0].click();
  await vi.waitFor(() => expect(host.shadowRoot.querySelector('.message.assistant .answer-content')).not.toBeNull(), { timeout: 3500 });
  expect(api).toHaveBeenCalledWith('POST', '/api/snapshots/snapshot/chat', {
    requestId: expect.any(String), question: 'How does it work?', history: [], answerLength: 'default',
  }, expect.any(Number));
  const initialRequests = api.mock.calls.filter(([, path]) => path.endsWith('/chat'));
  expect(initialRequests).toHaveLength(2);
  expect(initialRequests[0][2]).toEqual(initialRequests[1][2]);
  const answerLength = host.shadowRoot.querySelector('.answer-length select');
  answerLength.value = 'detailed';
  answerLength.dispatchEvent(new Event('change', { bubbles: true }));
  await tick();
  host.shadowRoot.querySelectorAll('.suggestion')[1].click();
  const followUp = {
    question: 'explain me here',
    answerLength: 'detailed',
    history: [{ role: 'user', content: 'How does it work?' }, { role: 'assistant', content: longAnswer }],
  };
  await vi.waitFor(() => expect(host.shadowRoot.querySelector('.chat-error button')).not.toBeNull());
  host.shadowRoot.querySelector('.chat-error button').click();
  await vi.waitFor(() => expect(host.shadowRoot.querySelectorAll('.message.assistant')).toHaveLength(2));
  expect(host.shadowRoot.querySelectorAll('.message.user')).toHaveLength(2);
  expect(api.mock.calls.filter(([, path]) => path.endsWith('/chat')).slice(-2)).toEqual([
    ['POST', '/api/snapshots/snapshot/chat', { ...followUp, requestId: expect.any(String) }, expect.any(Number)],
    ['POST', '/api/snapshots/snapshot/chat', { ...followUp, requestId: expect.any(String) }, expect.any(Number)],
  ]);
  const retryRequests = api.mock.calls.filter(([, path]) => path.endsWith('/chat')).slice(-2);
  expect(retryRequests[0][2].requestId).toBe(retryRequests[1][2].requestId);

  history.pushState({}, '', '/owner/repo/blob/feature/search/src/app.py');
  dispatchEvent(new Event('turbo:load'));
  await vi.waitFor(() => expect(api).toHaveBeenCalledWith('POST', '/api/repositories', {
    url: 'https://github.com/owner/repo/blob/feature/search/src/app.py',
  }));
  await vi.waitFor(() => expect(host.shadowRoot.querySelectorAll('.message.user')).toHaveLength(2));
  expect(document.querySelector('#gitlens-root')).toBe(host);
  expect(host.shadowRoot.querySelector('select').value).toBe('detailed');

  history.pushState({}, '', '/owner/repo/pull/11');
  dispatchEvent(new Event('turbo:load'));
  await vi.waitFor(() => expect(host.shadowRoot.querySelector('[role="alert"]')?.textContent).toContain('Pull request'));
  expect(host.shadowRoot.querySelector('.conversation')).toBeNull();
  history.pushState({}, '', '/owner/repo/blob/feature/search/src/app.py');
  dispatchEvent(new Event('turbo:load'));
  await vi.waitFor(() => expect(host.shadowRoot.querySelectorAll('.message.user')).toHaveLength(2));

  host.shadowRoot.querySelectorAll('.suggestion')[2].click();
  await vi.waitFor(() => expect(resolveLateAnswer).toBeTypeOf('function'));
  history.pushState({}, '', `/owner/repo/commit/${nextSha}`);
  dispatchEvent(new Event('turbo:load'));
  await vi.waitFor(() => expect(host.shadowRoot.querySelector('.summary')?.textContent).toContain('New commit'));
  resolveLateAnswer({ answer: 'Answer from previous snapshot', citations: [] });
  await tick();
  expect(host.shadowRoot.querySelector('.conversation').textContent).not.toContain('Answer from previous snapshot');
  expect(host.shadowRoot.querySelectorAll('.message.user')).toHaveLength(0);
  expect(host.shadowRoot.querySelector('select').value).toBe('default');

  history.pushState({}, '', '/owner/another');
  dispatchEvent(new Event('turbo:load'));
  await vi.waitFor(() => expect(host.shadowRoot.querySelector('.panel')).toBeNull());
  host.shadowRoot.querySelector('.launcher').click();
  await vi.waitFor(() => expect(host.shadowRoot.querySelector('.repo-name')?.textContent).toBe('owner/another'));
  expect(host.shadowRoot.querySelectorAll('.message.user')).toHaveLength(0);

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
