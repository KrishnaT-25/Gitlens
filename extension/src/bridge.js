export function send(message) {
  return chrome.runtime.sendMessage(message).then(result => {
    if (!result?.ok) {
      const error = new Error(result?.error?.message || 'GitLens request failed.');
      error.code = result?.error?.code;
      error.terminal = result?.error?.terminal;
      throw error;
    }
    return result.data;
  });
}
export function api(method, path, body, timeoutMs) { return send({ type: 'API', method, path, body, timeoutMs }); }
