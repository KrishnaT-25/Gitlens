import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { send } from './bridge.js';

function AuthPage() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [done, setDone] = useState(false);
  async function signIn() {
    setBusy(true); setError('');
    try { await send({ type: 'AUTH_GITHUB' }); setDone(true); }
    catch (e) { setError(e.message); } finally { setBusy(false); }
  }
  return <main style={{ fontFamily: 'system-ui', maxWidth: 400, margin: '10vh auto', padding: 24 }}>
    <h1>Sign in to GitLens</h1>
    <p>GitLens analyzes public repository content. Questions and source evidence sent through Gemini's free API may be used by Google to improve its products.</p>
    {busy && <p role="status">Connecting to GitLens. A sleeping free server may take about a minute to start.</p>}
    {done ? <p>Signed in. Return to your GitHub tab and reopen GitLens.</p> : <button onClick={signIn} disabled={busy}>Continue with GitHub</button>}
    {error && <p role="alert" style={{ color: '#b42318' }}>{error}</p>}
  </main>;
}
createRoot(document.getElementById('root')).render(<AuthPage />);
