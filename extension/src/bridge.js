export function send(message) {
  return chrome.runtime.sendMessage(message).then(result => {
    if (!result?.ok) throw new Error(result?.error?.message || 'GitLens request failed.');
    return result.data;
  });
}
export function api(method, path, body) { return send({ type: 'API', method, path, body }); }
