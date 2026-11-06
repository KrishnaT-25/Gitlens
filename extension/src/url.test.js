import { describe, it, expect } from 'vitest';
import { parseRepositoryUrl } from './url.js';

describe('GitHub URL parser', () => {
  it('accepts roots and supported subpages', () => {
    expect(parseRepositoryUrl('https://github.com/openai/openai-python')?.name).toBe('openai-python');
    expect(parseRepositoryUrl('https://github.com/a/b/blob/main/src/file.py?x=1')?.owner).toBe('a');
  });
  it('rejects lookalike hosts and unrelated pages', () => {
    for (const url of ['https://github.com.evil.test/a/b', 'https://github.com/search?q=x', 'https://github.com/a/b/settings', 'http://github.com/a/b']) expect(parseRepositoryUrl(url)).toBeNull();
  });
  it('preserves selected refs, slash branches, tags, and commits for server resolution', () => {
    for (const path of ['/tree/develop', '/blob/feature/search/src/app.py', '/tree/v1.0', '/tree/release%2F2026', '/commit/abcdef1']) {
      const url = `https://github.com/a/b${path}`;
      expect(parseRepositoryUrl(url)?.url).toBe(url);
    }
  });
  it('identifies PR contexts without pretending to analyse the default branch', () => {
    for (const path of ['/pull', '/pulls', '/pull/12/files']) {
      expect(parseRepositoryUrl(`https://github.com/a/b${path}`)?.unsupportedReason).toContain('Pull request');
    }
  });
  it('rejects incomplete routes, invalid ports, malformed escapes, and traversal before URL normalization', () => {
    for (const url of ['https://github.com:443/a/b', 'https://github.com:bad/a/b', 'https://github.com/a//b',
      'https://github.com/a/b/tree', 'https://github.com/a/b/blob/main', 'https://github.com/a/b/commit/main',
      'https://github.com/a/b/tree/main/../secret', 'https://github.com/a/b/tree/main/%2e%2e/secret',
      'https://github.com/a/b/tree/%ZZ', 'https://github.com/a/b/tree/main%00', 'https://github.com/a/b/issues/unknown']) {
      expect(parseRepositoryUrl(url)).toBeNull();
    }
  });
});
