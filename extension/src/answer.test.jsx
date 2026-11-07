// @vitest-environment jsdom
import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, expect, test } from 'vitest';
import { AnswerMessage } from './answer.jsx';

globalThis.IS_REACT_ACT_ENVIRONMENT = true;
let root;
let container;

async function renderAnswer(props) {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => root.render(<AnswerMessage {...props}/>));
  return container;
}

afterEach(async () => {
  if (root) await act(async () => root.unmount());
  container?.remove();
  root = null;
  container = null;
});

test('renders lists and links only references with verified citations', async () => {
  const view = await renderAnswer({
    content: 'Node.js and `Socket.IO` use code in `server/app/` and `server/migrations/`.\n\n- **`server/app/models.py`**: SQLAlchemy models\n- `server/app/db.py:L1-L10`: engine and session setup',
    citations: [
      { path: 'server/app/models.py', lineStart: 1, lineEnd: 20, url: 'https://github.com/o/r/blob/abc/server/app/models.py#L1-L20' },
      { path: 'server/app/db.py', lineStart: 1, lineEnd: 10, url: 'https://github.com/o/r/blob/abc/server/app/db.py#L1-L10' },
    ],
    repo: { owner: 'o', name: 'r' }, sha: 'abc',
  });
  expect(view.querySelectorAll('.answer-content > ul li')).toHaveLength(2);
  expect(view.querySelectorAll('.answer-content > p .file-chip, .answer-content > ul .file-chip')).toHaveLength(3);
  expect(view.querySelector('.answer-content > p code').textContent).toBe('Socket.IO');
  expect(view.querySelector('.file-chip[href*="models.py"]')?.getAttribute('href')).toContain('https://github.com/o/r/blob/abc/server/app/models.py');
  expect(view.querySelector('strong')).not.toBeNull();
  expect(view.querySelector('.answer-content').textContent).not.toContain('**');
});

test('keeps slash-separated terms as text and links verified extensionless files and directories', async () => {
  const view = await renderAnswer({
    content: 'BM25/FTS and `HTTP/HTTPS` are terms. See `README:L1`, `src/bin`, and `missing/path`.',
    citations: [
      { path: 'README', lineStart: 1, lineEnd: 2, url: 'https://github.com/o/r/blob/abc/README#L1-L2' },
      { path: 'src/bin/run', lineStart: 1, lineEnd: 2, url: 'https://github.com/o/r/blob/abc/src/bin/run#L1-L2' },
    ],
    repo: { owner: 'o', name: 'r' }, sha: 'abc',
  });
  expect(view.querySelector('.answer-content > p').textContent).toContain('BM25/FTS and HTTP/HTTPS');
  expect([...view.querySelectorAll('.answer-content > p a')].map(link => link.href)).toEqual([
    'https://github.com/o/r/blob/abc/README#L1-L2',
    'https://github.com/o/r/tree/abc/src/bin',
  ]);
});

test('uses the matching excerpt when a file has multiple citations', async () => {
  const path = 'src/meeting.js';
  const view = await renderAnswer({
    content: 'See `src/meeting.js` for both branches and `src/meeting.js:L30-L32` for the incoming offer.',
    citations: [[10, 20], [30, 40]].map(([lineStart, lineEnd]) => ({
      path, lineStart, lineEnd, url: `https://github.com/o/r/blob/abc/${path}#L${lineStart}-L${lineEnd}`,
    })),
  });
  const links = view.querySelectorAll('.answer-content > p .file-chip');
  expect(links).toHaveLength(1);
  expect(links[0].getAttribute('href')).toContain('#L30-L40');
});

test('keeps raw HTML inert and rejects javascript links', async () => {
  const view = await renderAnswer({ content: '<script>alert(1)</script> [run](javascript:alert%281%29)', repo: { owner: 'o', name: 'r' }, sha: 'abc' });
  expect(view.querySelector('script')).toBeNull();
  expect(view.querySelector('a[href^="javascript:"]')).toBeNull();
  expect(view.querySelector('.answer-content').textContent).toContain('<script>');
});

test('renders GFM tables and fenced code with copy controls', async () => {
  const view = await renderAnswer({
    content: '| File | Role |\n| --- | --- |\n| `src/api.py` | API |\n\n```python\nprint("ready")\n```',
    repo: { owner: 'o', name: 'r' }, sha: 'abc',
  });
  expect(view.querySelector('.answer-table table')).not.toBeNull();
  expect(view.querySelector('.answer-table tbody td').textContent).toContain('src/api.py');
  expect(view.querySelector('.answer-code pre code').textContent).toContain('print("ready")');
  expect(view.querySelector('.answer-code button[aria-label="Copy code"]')).not.toBeNull();
  expect(view.querySelector('.answer-actions button[aria-label="Copy answer"]')).not.toBeNull();
});

test('shows de-duplicated path-sorted sources collapsed by default', async () => {
  const view = await renderAnswer({
    content: 'The code is configured in these files.',
    citations: [
      { path: 'z.py', lineStart: 1, lineEnd: 2, url: 'https://github.com/o/r/blob/abc/z.py#L1-L2' },
      { path: 'a.py', lineStart: 5, lineEnd: 5, url: 'https://github.com/o/r/blob/abc/a.py#L5-L5' },
      { path: 'a.py', lineStart: 5, lineEnd: 5, url: 'https://github.com/o/r/blob/abc/a.py#L5-L5' },
    ],
    repo: { owner: 'o', name: 'r' }, sha: 'abc',
  });
  const details = view.querySelector('details');
  expect(details).not.toBeNull();
  expect(details.open).toBe(false);
  expect(details.querySelectorAll('li')).toHaveLength(2);
  expect(details.querySelector('li a').textContent).toContain('a.py');
});
