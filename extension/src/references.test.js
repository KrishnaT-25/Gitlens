import { expect, test } from 'vitest';
import { deduplicateSources, githubFileUrl, parseFileReferences } from './references.js';

test('finds paths and line ranges in prose without parsing URLs', () => {
  const references = parseFileReferences('See server/app/db.py:L1-L10, `README.md:2`, and server/app/; https://github.com/owner/repo/blob/main/not/a-reference.py');
  expect(references.map(({ path, lineStart, lineEnd }) => [path, lineStart, lineEnd])).toEqual([
    ['server/app/db.py', 1, 10],
    ['README.md', 2, 2],
    ['server/app/', null, null],
  ]);
});

test('does not accept traversal references and handles single-line syntax', () => {
  const references = parseFileReferences('../secret.py and src/db.py:12');
  expect(references.map(ref => [ref.path, ref.lineStart, ref.lineEnd])).toEqual([['src/db.py', 12, 12]]);
});

test('deduplicates and sorts source entries by path and line', () => {
  const sources = deduplicateSources([
    { path: 'z.py', lineStart: 3, lineEnd: 4, url: 'z' },
    { path: 'a.py', lineStart: 8, lineEnd: 8, url: 'a8' },
    { path: 'z.py', lineStart: 3, lineEnd: 4, url: 'duplicate' },
    { path: 'a.py', lineStart: 2, lineEnd: 4, url: 'a2' },
  ]);
  expect(sources.map(({ path, lineStart }) => [path, lineStart])).toEqual([['a.py', 2], ['a.py', 8], ['z.py', 3]]);
});

test('builds a SHA-pinned GitHub URL with encoded paths and ranges', () => {
  expect(githubFileUrl({ path: 'src/My File.py', lineStart: 2, lineEnd: 5 }, { owner: 'team', name: 'repo' }, 'abc123'))
    .toBe('https://github.com/team/repo/blob/abc123/src/My%20File.py#L2-L5');
  expect(githubFileUrl({ path: '../secret.py' }, { owner: 'team', name: 'repo' }, 'abc123')).toBe('');
});
