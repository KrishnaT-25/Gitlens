import { describe, it, expect } from 'vitest';
import { callbackCode } from './oauth.js';

const redirect = 'https://copoojjfbgbgcnaokahlbopnafkljfkf.chromiumapp.org/github';

describe('GitHub OAuth callback', () => {
  it('accepts only the expected redirect and state', () => {
    expect(callbackCode(`${redirect}?code=abc&state=xyz`, redirect, 'xyz')).toBe('abc');
    expect(() => callbackCode(`${redirect}?code=abc&state=wrong`, redirect, 'xyz')).toThrow();
    expect(() => callbackCode('https://evil.test/github?code=abc&state=xyz', redirect, 'xyz')).toThrow();
    expect(() => callbackCode(`${redirect}?error=access_denied&state=xyz`, redirect, 'xyz')).toThrow();
  });
});
