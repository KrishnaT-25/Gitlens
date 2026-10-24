export function randomUrlSafe() {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export async function authorizationUrl({ clientId, redirectUri }, state, verifier) {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
  const challenge = btoa(String.fromCharCode(...new Uint8Array(digest))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  const url = new URL('https://github.com/login/oauth/authorize');
  url.searchParams.set('client_id', clientId);
  url.searchParams.set('redirect_uri', redirectUri);
  url.searchParams.set('scope', '');
  url.searchParams.set('state', state);
  url.searchParams.set('code_challenge', challenge);
  url.searchParams.set('code_challenge_method', 'S256');
  return url.href;
}

export function callbackCode(callback, redirectUri, state) {
  if (!callback) throw new Error('GitHub sign-in was cancelled.');
  const actual = new URL(callback);
  const expected = new URL(redirectUri);
  if (actual.origin !== expected.origin || actual.pathname !== expected.pathname || actual.searchParams.get('state') !== state) {
    throw new Error('GitHub sign-in could not be verified.');
  }
  if (actual.searchParams.has('error')) throw new Error('GitHub sign-in was declined or failed.');
  const code = actual.searchParams.get('code');
  if (!code) throw new Error('GitHub did not return a sign-in code.');
  return code;
}
