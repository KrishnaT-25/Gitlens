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
});
