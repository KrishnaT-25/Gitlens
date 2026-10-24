import { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { parseRepositoryUrl } from './url.js';
import { api, send } from './bridge.js';

const styles = `:host{all:initial;font-family:system-ui,sans-serif;color:#24292f}*{box-sizing:border-box}.launcher{position:fixed;bottom:24px;right:24px;z-index:2147483647;border:0;border-radius:50%;width:56px;height:56px;background:#24292f;color:white;font-weight:700;cursor:pointer;box-shadow:0 4px 16px #0004}.panel{position:fixed;right:24px;bottom:88px;width:min(390px,calc(100vw - 32px));height:min(560px,calc(100vh - 112px));z-index:2147483647;background:white;border:1px solid #d0d7de;border-radius:14px;box-shadow:0 12px 36px #0003;display:flex;flex-direction:column;overflow:hidden;font:14px system-ui,sans-serif;color:#24292f}.head{padding:12px 16px;background:#f6f8fa;border-bottom:1px solid #d0d7de;display:flex;justify-content:space-between;align-items:center}.head .actions{display:flex;align-items:center;gap:8px}.body{padding:16px;overflow:auto;flex:1}.foot{padding:12px;border-top:1px solid #d0d7de;display:flex;gap:8px}button{cursor:pointer}button:focus-visible,input:focus-visible{outline:2px solid #0969da;outline-offset:2px}.head button{border:0;background:none;font-size:18px}.head button.signout{font-size:13px;color:#0969da;border:1px solid #d0d7de;border-radius:6px;padding:5px 8px;background:white}.head button.close{color:#24292f;padding:4px}.message{white-space:pre-wrap;margin:12px 0;padding:10px;border-radius:8px;background:#f6f8fa}.message.user{background:#ddf4ff}.citations{padding-left:18px}.muted{color:#57606a}.error{color:#b42318}.foot input{flex:1;min-width:0;padding:8px;border:1px solid #d0d7de;border-radius:6px}.foot button,.primary{border:0;border-radius:6px;background:#0969da;color:white;padding:8px 12px}.summary ul{padding-left:20px}`;

function Widget({ repo }) {
  const [open, setOpen] = useState(false), [signedIn, setSignedIn] = useState(null), [snapshot, setSnapshot] = useState(null);
  const [summary, setSummary] = useState(null), [messages, setMessages] = useState([]), [question, setQuestion] = useState('');
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [serverStatus, setServerStatus] = useState('');
  const closeRef = useRef(null);
  useEffect(() => { if (open) closeRef.current?.focus(); }, [open]);
  useEffect(() => {
    if (!globalThis.chrome?.runtime?.onMessage) return;
    const onStatus = message => {
      if (message?.type === 'API_STATUS') setServerStatus(message.status === 'waking' ? 'Waking up the GitLens server…' : '');
    };
    globalThis.chrome.runtime.onMessage.addListener(onStatus);
    return () => globalThis.chrome.runtime.onMessage.removeListener(onStatus);
  }, []);
  useEffect(() => { setSnapshot(null); setSummary(null); setMessages([]); setError(''); }, [repo.url]);
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    (async () => {
      try {
        const auth = await send({ type: 'GET_AUTH' });
        if (cancelled) return;
        setSignedIn(auth.signedIn);
        if (auth.signedIn) {
          const result = await api('POST', '/api/repositories', { url: repo.url });
          if (!cancelled) setSnapshot(result);
        }
      } catch (e) { if (!cancelled) setError(e.message); }
    })();
    return () => { cancelled = true; };
  }, [open, repo.url]);
  useEffect(() => {
    if (!open || !snapshot || ['ready', 'failed', 'unsupported'].includes(snapshot.status)) return;
    let active = true;
    const poll = async () => {
      try { const status = await api('GET', `/api/snapshots/${snapshot.snapshotId}/status`); if (active) setSnapshot(status); }
      catch (e) { if (active) setError(e.message); }
    };
    const timer = setInterval(poll, 3000); poll();
    return () => { active = false; clearInterval(timer); };
  }, [open, snapshot?.snapshotId, snapshot?.status]);
  useEffect(() => {
    if (snapshot?.status !== 'ready' || summary) return;
    api('GET', `/api/snapshots/${snapshot.snapshotId}/summary`).then(x => setSummary(x.summary)).catch(e => setError(e.message));
  }, [snapshot?.status, snapshot?.snapshotId, summary]);
  async function submitQuestion(value) {
    if (!value.trim() || busy || !snapshot) return;
    const q = value.trim(); setQuestion(''); setBusy(true); setError('');
    const previous = messages.slice(-2).map(x => ({ role: x.role, content: x.content }));
    setMessages(old => [...old, { role: 'user', content: q }]);
    try {
      const result = await api('POST', `/api/snapshots/${snapshot.snapshotId}/chat`, { question: q, history: previous });
      setMessages(old => [...old, { role: 'assistant', content: result.answer, citations: result.citations }]);
    } catch (e) { setError(e.message); } finally { setBusy(false); }
  }
  function ask(event) { event.preventDefault(); submitQuestion(question); }
  async function signOut() {
    setBusy(true); setError('');
    try {
      await send({ type: 'LOGOUT' });
    } catch (e) { setError(e.message); }
    finally { setSignedIn(false); setSnapshot(null); setSummary(null); setMessages([]); setBusy(false); }
  }
  return <>
    <button className="launcher" aria-label={open ? 'Close GitLens' : 'Open GitLens'} aria-expanded={open} onClick={() => setOpen(!open)}>GL</button>
    {open && <section className="panel" role="dialog" aria-label="GitLens repository assistant" onKeyDown={e => { if (e.key === 'Escape') setOpen(false); }}>
      <header className="head"><div><strong>{repo.owner}/{repo.name}</strong><br/><small>{snapshot?.sha ? snapshot.sha.slice(0, 12) : 'Repository assistant'}</small></div><div className="actions">{signedIn && <button className="signout" disabled={busy} onClick={signOut}>Sign out</button>}<button className="close" ref={closeRef} aria-label="Close panel" onClick={() => setOpen(false)}>×</button></div></header>
      <div className="body">
        {signedIn === false && <><p>Sign in to analyze this repository.</p><button className="primary" onClick={() => send({ type: 'OPEN_AUTH' }).catch(e => setError(e.message))}>Sign in</button></>}
        {signedIn === null && !error && <p>Checking sign-in…</p>}
        {serverStatus && <p className="muted" role="status">{serverStatus}</p>}
        {snapshot && !['ready','failed','unsupported'].includes(snapshot.status) && <p>Repository status: {snapshot.status}.</p>}
        {snapshot?.status === 'failed' && <><p>Analysis failed.</p><button className="primary" onClick={async () => { try { const result = await api('POST', `/api/snapshots/${snapshot.snapshotId}/retry`); setSnapshot(result); setError(''); } catch (e) { setError(e.message); } }}>Retry analysis</button></>}
        {snapshot?.status === 'unsupported' && <p>This repository cannot be analyzed within the current limits.</p>}
        {summary && <div className="summary"><h2>Repository summary</h2><p>{summary.purpose}</p><h3>Components</h3><ul>{summary.components?.map((x,i) => <li key={i}>{x}</li>)}</ul><h3>Entry points</h3><ul>{summary.entrypoints?.map((x,i) => <li key={i}>{x}</li>)}</ul><h3>Technologies</h3><ul>{summary.technologies?.map((x,i) => <li key={i}>{x}</li>)}</ul>{summary.uncertainty && <p className="muted">{summary.uncertainty}</p>}{summary.suggestedQuestions?.length > 0 && <><h3>Suggested questions</h3>{summary.suggestedQuestions.map((x,i) => <p key={i}><button disabled={busy} onClick={() => submitQuestion(x)}>{x}</button></p>)}</>}</div>}
        {messages.map((m,i) => <div className={`message ${m.role}`} key={i}><strong>{m.role === 'user' ? 'You' : 'GitLens'}</strong><p>{m.content}</p>{m.citations?.length > 0 && <ul className="citations">{m.citations.map((c,j) => <li key={j}><a href={c.url} target="_blank" rel="noopener noreferrer">{c.path}:L{c.lineStart}-L{c.lineEnd}</a></li>)}</ul>}</div>)}
        {error && <p className="error" role="alert">{error}</p>}
      </div>
      {snapshot?.status === 'ready' && <form className="foot" onPointerDown={e => e.stopPropagation()} onClick={e => e.stopPropagation()} onSubmit={ask}><input aria-label="Ask about this repository" value={question} onChange={e => setQuestion(e.target.value)} onPointerDown={e => { e.stopPropagation(); e.currentTarget.focus(); }} onClick={e => { e.stopPropagation(); e.currentTarget.focus(); }} onKeyDown={e => { e.stopPropagation(); if (e.key === 'Escape') setOpen(false); }} placeholder="Ask about this codebase"/><button disabled={busy || !question.trim()}>Ask</button></form>}
    </section>}
  </>;
}

let host, root, current = '';
function sync() {
  const url = location.href;
  if (url === current) return;
  current = url;
  const repo = parseRepositoryUrl(url);
  if (root) { root.unmount(); root = null; host.remove(); host = null; }
  if (!repo) return;
  host = document.createElement('div'); host.id = 'gitlens-root';
  const shadow = host.attachShadow({ mode: 'closed' });
  const style = document.createElement('style'); style.textContent = styles; shadow.appendChild(style);
  const mount = document.createElement('div'); shadow.appendChild(mount); document.documentElement.appendChild(host);
  root = createRoot(mount); root.render(<Widget repo={repo}/>);
}
sync();
addEventListener('popstate', sync);
addEventListener('turbo:load', sync);
addEventListener('pjax:end', sync);
new MutationObserver(() => { if (location.href !== current) sync(); }).observe(document.documentElement, { childList: true, subtree: true });
