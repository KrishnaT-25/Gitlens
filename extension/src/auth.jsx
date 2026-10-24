import { useState } from 'react';
import { createRoot } from 'react-dom/client';
import { send } from './bridge.js';

const authStyles = `*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 80% 0%,#f1dfc5 0,transparent 40%),#282725;color:#f7f0e4;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}.auth-shell{min-height:100vh;display:grid;place-items:center;padding:28px}.auth-card{width:min(540px,100%);padding:clamp(28px,6vw,52px);border:1px solid #e7d8c1;border-radius:28px;background:linear-gradient(150deg,#fffaf0,#f1e5d2);color:#292723;box-shadow:0 30px 90px #0005}.auth-brand{display:flex;align-items:center;gap:12px;margin-bottom:28px;color:#7b4d2a;font-size:13px;font-weight:750;letter-spacing:.12em;text-transform:uppercase}.auth-mark{display:grid;place-items:center;width:42px;height:42px;border-radius:14px;background:linear-gradient(145deg,#edb66f,#d78237);color:white;font-size:18px;letter-spacing:-.06em}.auth-card h1{margin:0 0 12px;font-size:clamp(28px,6vw,38px);line-height:1.12;letter-spacing:-.05em}.auth-card p{color:#62594e;line-height:1.65}.auth-note{padding:13px 15px;border:1px solid #e4d2b8;border-radius:13px;background:#fff8eb;font-size:13px}.auth-status{padding:13px 15px;border-left:3px solid #d68a43;border-radius:0 12px 12px 0;background:#f3e4cf}.auth-button{width:100%;margin-top:10px;padding:13px 16px;border:0;border-radius:13px;background:linear-gradient(135deg,#e99739,#d47a21);color:#fff;font:inherit;font-size:14px;font-weight:700;cursor:pointer;box-shadow:0 6px 16px #bf722b33}.auth-button:hover:not(:disabled){filter:brightness(1.05);transform:translateY(-1px)}.auth-button:disabled{opacity:.6;cursor:wait}.auth-button:focus-visible{outline:3px solid #ad6128;outline-offset:3px}.auth-error{padding:11px 13px;border:1px solid #e5bdb0;border-radius:11px;background:#fff0e9;color:#9f3829}.auth-success{padding:14px 16px;border:1px solid #b8d8b8;border-radius:13px;background:#edf6e8;color:#315c36}`;

function AuthPage() {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [done, setDone] = useState(false);
  async function signIn() {
    setBusy(true); setError('');
    try { await send({ type: 'AUTH_GITHUB' }); setDone(true); }
    catch (e) { setError(e.message); } finally { setBusy(false); }
  }
  return <><style>{authStyles}</style><main className="auth-shell"><section className="auth-card">
    <div className="auth-brand"><span className="auth-mark" aria-hidden="true">GL</span>GitLens repository assistant</div>
    <h1>Sign in to GitLens</h1>
    <p>Explore public repositories with summaries and source-grounded answers.</p>
    <p className="auth-note">Questions and source evidence sent through Gemini’s free API may be used by Google to improve its products.</p>
    {busy && <p className="auth-status" role="status">Connecting to GitLens. A sleeping free server may take about a minute to start.</p>}
    {done ? <p className="auth-success">Signed in. Return to your GitHub tab and reopen GitLens.</p> : <button className="auth-button" onClick={signIn} disabled={busy}>{busy ? 'Connecting…' : 'Continue with GitHub'}</button>}
    {error && <p className="auth-error" role="alert">{error}</p>}
  </section></main></>;
}
createRoot(document.getElementById('root')).render(<AuthPage />);
