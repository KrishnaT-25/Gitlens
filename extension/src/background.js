import { authorizationUrl, callbackCode, randomUrlSafe } from './oauth.js';
const apiBase = __API_BASE_URL__;
let warmAt = 0;
let warming;

async function wakeApi(sender) {
  const notify = status => {
    if (sender?.tab?.id !== undefined) chrome.tabs.sendMessage(sender.tab.id, { type: 'API_STATUS', status }).catch(() => {});
  };
  if (Date.now() - warmAt < 5 * 60_000) { notify('ready'); return; }
  notify('waking');
  if (!warming) warming = (async () => {
    for (let attempt = 0; attempt < 6; attempt++) {
      try {
        const response = await fetch(apiBase + '/readyz', { signal: AbortSignal.timeout(20_000) });
        if (response.ok && response.headers.get('content-type')?.includes('application/json') && (await response.json()).ok === true) {
          warmAt = Date.now(); return;
        }
      } catch { /* The free API may still be starting. */ }
      if (attempt < 5) await new Promise(resolve => setTimeout(resolve, 5000));
    }
    throw new Error('GitLens server is starting or unavailable. Try again shortly.');
  })().finally(() => { warming = undefined; });
  try { await warming; notify('ready'); }
  catch (error) { notify('unavailable'); throw error; }
}

async function api(method, path, body, sender) {
  if (!['GET', 'POST'].includes(method) || !/^\/api\/(auth|repositories|snapshots)(\/|$)/.test(path)) throw new Error('Invalid API request.');
  await wakeApi(sender);
  const { token } = await chrome.storage.local.get('token');
  const response = await fetch(apiBase + path, {
    method,
    headers: { 'Content-Type': 'application/json', ...(token ? { Authorization: `Bearer ${token}` } : {}) },
    signal: AbortSignal.timeout(28000),
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const reader = response.body?.getReader();
  const chunks = []; let bytes = 0;
  if (reader) {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > 1_000_000) { await reader.cancel(); throw new Error('API response is too large.'); }
      chunks.push(value);
    }
  }
  const data = new Uint8Array(bytes); let offset = 0;
  for (const chunk of chunks) { data.set(chunk, offset); offset += chunk.byteLength; }
  let result = {};
  try { result = JSON.parse(new TextDecoder().decode(data)); } catch { /* Empty or malformed response. */ }
  if (!response.ok) { const error = new Error(result.error?.message || `Request failed (${response.status}).`); error.status = response.status; throw error; }
  return result;
}

chrome.runtime.onMessage.addListener((message, _sender, reply) => {
  (async () => {
    if (message.type === 'API') return api(message.method, message.path, message.body, _sender);
    if (message.type === 'AUTH_GITHUB') {
      const config = await api('GET', '/api/auth/github/config', undefined, _sender);
      if (config.redirectUri !== chrome.identity.getRedirectURL('github')) throw new Error('GitHub callback URL does not match this extension.');
      const state = randomUrlSafe(), verifier = randomUrlSafe();
      const url = await authorizationUrl(config, state, verifier);
      const callback = await chrome.identity.launchWebAuthFlow({ url, interactive: true });
      const code = callbackCode(callback, config.redirectUri, state);
      const result = await api('POST', '/api/auth/github', { code, codeVerifier: verifier }, _sender);
      await chrome.storage.local.set({ token: result.token });
      return { signedIn: true };
    }
    if (message.type === 'GET_AUTH') {
      const { token } = await chrome.storage.local.get('token');
      if (!token) return { signedIn: false };
      try { await api('GET', '/api/auth/me'); return { signedIn: true }; }
      catch (error) { if (error.status === 401) { await chrome.storage.local.remove('token'); return { signedIn: false }; } throw error; }
    }
    if (message.type === 'OPEN_AUTH') { await chrome.tabs.create({ url: chrome.runtime.getURL('auth.html') }); return { opened: true }; }
    if (message.type === 'LOGOUT') { try { await api('POST', '/api/auth/logout'); } finally { await chrome.storage.local.remove('token'); } return { signedIn: false }; }
    throw new Error('Unknown message.');
  })().then(data => reply({ ok: true, data })).catch(error => reply({ ok: false, error: { message: error.message } }));
  return true;
});
