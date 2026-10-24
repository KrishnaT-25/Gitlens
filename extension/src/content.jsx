import { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { parseRepositoryUrl } from './url.js';
import { api, send } from './bridge.js';
import { AnswerMessage } from './answer.jsx';

const styles = `
:host{all:initial;--paper:#f6f0e4;--paper-deep:#efe5d4;--ink:#272624;--muted:#797267;--line:#e2d4bf;--amber:#d78b42;--amber-dark:#a95d27;--amber-soft:#f3dfc8;--green:#3e9b5a;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--ink)}
*{box-sizing:border-box}button,input{font:inherit}button{cursor:pointer}button:disabled{cursor:wait;opacity:.58}button:focus-visible,input:focus-visible{outline:3px solid #dc9b5f;outline-offset:2px}
.launcher{position:fixed;right:24px;bottom:24px;z-index:2147483647;width:58px;height:58px;border:1px solid #edc89f;border-radius:20px;background:linear-gradient(145deg,#fff8e9,#e7b477);color:#392719;font-weight:800;font-size:17px;letter-spacing:-.06em;box-shadow:0 10px 28px #25170e4d;transition:transform .18s ease,box-shadow .18s ease}.launcher:hover{transform:translateY(-2px);box-shadow:0 14px 32px #25170e5c}
.panel{position:fixed;right:24px;bottom:94px;width:min(440px,calc(100vw - 28px));height:min(700px,calc(100vh - 112px));z-index:2147483647;display:flex;flex-direction:column;overflow:hidden;border:1px solid #e6d8c4;border-radius:25px;background:var(--paper);box-shadow:0 24px 70px #0006,0 2px 8px #6d50301a;font:14px/1.5 Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--ink);animation:panel-in .2s ease-out}.panel.expanded{width:min(620px,calc(100vw - 28px));height:min(850px,calc(100vh - 112px))}@keyframes panel-in{from{opacity:0;transform:translateY(8px) scale(.985)}to{opacity:1;transform:translateY(0) scale(1)}}
.head{min-height:78px;padding:15px 18px;display:flex;justify-content:space-between;align-items:center;gap:12px;background:linear-gradient(120deg,#fff9ed,#f1e4d0);border-bottom:1px solid var(--line)}.repo-title{display:flex;align-items:center;gap:11px;min-width:0}.repo-mark{position:relative;display:grid;place-items:center;flex:none;width:34px;height:34px;color:#615b51}.repo-mark svg{width:29px;height:29px}.repo-mark:after{content:"";position:absolute;right:0;bottom:1px;width:10px;height:10px;border:2px solid var(--paper);border-radius:50%;background:var(--green)}.repo-name{max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:15px;font-weight:750;letter-spacing:-.025em}.repo-sha{color:var(--muted);font-size:12px;letter-spacing:.035em}.head .actions{display:flex;align-items:center;gap:5px;flex:none}.icon-button{display:grid;place-items:center;width:34px;height:34px;padding:0;border:0;border-radius:10px;background:transparent;color:#786f63;font-size:22px;line-height:1;transition:background .16s,color .16s}.icon-button:hover{background:#eaddc8;color:#362d24}.head button.signout{padding:7px 10px;border:1px solid #ddc8ab;border-radius:10px;background:#fffaf1;color:#76512f;font-size:12px;font-weight:650}.head button.signout:hover{background:#f8ead5}
.body{flex:1;overflow:auto;padding:18px 18px 22px;background:radial-gradient(ellipse at top right,#fff9ec88,transparent 42%),var(--paper);scrollbar-color:#c9ae8d transparent;scrollbar-width:thin}.body>p{margin:8px 0 14px}.status-card{padding:12px 14px;border:1px solid #e8d8c2;border-radius:13px;background:#fffaf1;color:#5e5549}.status-card:before{content:"";display:inline-block;width:8px;height:8px;margin:0 8px 1px 0;border-radius:50%;background:var(--amber)}.summary h2{margin:2px 0 8px;font-size:22px;line-height:1.2;letter-spacing:-.04em}.summary>p:first-of-type{margin:0 0 18px;color:#4f4b44;font-size:14px}.summary h3{margin:19px 0 8px;color:#61584c;font-size:12px;font-weight:750;letter-spacing:.11em;text-transform:uppercase}.summary ul{padding-left:20px;margin:5px 0 12px;color:#45413b}.summary li{padding:2px 0}.summary .muted,.muted{color:var(--muted)}.summary .uncertainty{padding:10px 12px;border-left:3px solid #d7b58d;background:#f1e6d6;border-radius:0 10px 10px 0;font-size:12px}
.suggestions{display:grid;gap:8px;margin:9px 0 20px}.suggestion{width:100%;padding:10px 13px;border:1px solid #d6aa7d;border-radius:13px;background:#fff9ef;color:#754322;text-align:left;font-size:13px;line-height:1.4;transition:transform .15s,background .15s,border-color .15s}.suggestion:hover:not(:disabled){transform:translateY(-1px);background:#fff2df;border-color:#c8874d}
.message{max-width:94%;margin:14px 0;padding:13px 15px;border:1px solid transparent;border-radius:18px;white-space:pre-wrap;overflow-wrap:anywhere;box-shadow:0 3px 10px #47341d0d}.message>strong{display:inline-flex;align-items:center;gap:7px;margin-bottom:4px;font-size:12px;font-weight:750;letter-spacing:.015em}.message>p{margin:4px 0 0}.message.user{margin-left:auto;border-radius:18px 18px 5px 18px;background:linear-gradient(135deg,#d99754,#c47b39);color:#fffaf4}.message.user>strong{color:#fff2df}.message.assistant{margin-right:auto;border-radius:18px 18px 18px 5px;background:linear-gradient(145deg,#292928,#1c1c1b);color:#f8f4ec;box-shadow:0 8px 18px #17110b24}.message.assistant>strong{color:#edd2b4}.ai-badge{display:inline-flex;align-items:center;gap:6px;padding:4px 9px;border:1px solid #ffffff17;border-radius:999px;background:#ffffff12;color:#ead5bf}.ai-spark{color:#d986ee;font-size:14px}.citations{display:flex;flex-wrap:wrap;gap:6px;padding:0;margin:11px 0 0;list-style:none}.citations a{display:inline-block;padding:4px 8px;border:1px solid #e4c49f;border-radius:8px;background:#fff7ea;color:#8f5429;font-size:11px;text-decoration:none}.citations a:hover{background:#f8e6ce;text-decoration:underline}.assistant .citations a{border-color:#595045;background:#37322c;color:#f0c48e}.error{padding:10px 12px;border:1px solid #eac4b9;border-radius:11px;background:#fff0e9;color:#a33c2b;font-size:13px}
.foot{display:flex;align-items:center;gap:9px;padding:12px 14px 14px;border-top:1px solid var(--line);background:linear-gradient(180deg,#f5ecde,#fff8eb)}.foot input{flex:1;min-width:0;padding:11px 13px;border:1px solid transparent;border-radius:13px;background:#fffaf1;color:var(--ink);box-shadow:inset 0 0 0 1px #e7d6bf;font-size:13px;transition:box-shadow .16s,border-color .16s}.foot input::placeholder{color:#9b8973}.foot input:focus{border-color:#d79b63;box-shadow:0 0 0 3px #d99b6330;outline:0}.foot button,.primary{min-width:48px;min-height:42px;padding:9px 14px;border:0;border-radius:13px;background:linear-gradient(135deg,#ed9c36,#d87b20);color:white;font-size:13px;font-weight:750;box-shadow:0 4px 10px #bc6a2833;transition:filter .16s,transform .16s}.foot button:hover:not(:disabled),.primary:hover:not(:disabled){filter:brightness(1.05);transform:translateY(-1px)}.primary.secondary{border:1px solid #d7b38d;background:#fff8ec;color:#744a29;box-shadow:none}.empty-state{padding:18px;border:1px solid #e6d7c3;border-radius:16px;background:#fff9ef;color:#625a4e}.empty-state h2{margin:0 0 6px;font-size:17px}.empty-state p{margin:0;color:var(--muted)}
@media(max-width:520px){.panel{right:10px;bottom:82px;width:calc(100vw - 20px);height:min(720px,calc(100vh - 96px));border-radius:21px}.panel.expanded{width:calc(100vw - 20px);height:calc(100vh - 96px)}.head{padding:12px 14px}.body{padding:15px}.repo-name{max-width:190px}.launcher{right:16px;bottom:16px}}
.conversation{min-height:1px}.message.assistant{max-width:100%;padding:13px 15px}.answer-content{font-size:13px;line-height:1.6;overflow-wrap:anywhere}.answer-content>:first-child{margin-top:2px}.answer-content>:last-child{margin-bottom:2px}.answer-content p{margin:8px 0;color:inherit}.answer-content h1,.answer-content h2,.answer-content h3,.answer-content h4{margin:15px 0 7px;color:#f4dfc5;line-height:1.3}.answer-content h1{font-size:18px}.answer-content h2{font-size:16px}.answer-content h3{font-size:14px}.answer-content ul,.answer-content ol{padding-left:20px;margin:8px 0 12px}.answer-content li{padding:2px 0}.answer-content li+li{margin-top:3px}.answer-content blockquote{margin:10px 0;padding:3px 0 3px 12px;border-left:3px solid #bc8a57;color:#e4d5c2}.answer-content a:not(.file-chip){color:#f2c891;text-decoration:underline;text-underline-offset:2px}.answer-content code{padding:1px 4px;border:1px solid #ffffff22;border-radius:5px;background:#ffffff10;color:#f0d3ad;font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace}.file-chip{display:inline-flex;align-items:center;gap:5px;max-width:100%;padding:2px 7px;border:1px solid #594b3e;border-radius:7px;background:#39332c;color:#f3c98f!important;font:11px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace;text-decoration:none!important;overflow-wrap:anywhere;vertical-align:baseline}.file-chip:hover{border-color:#c68b4e;background:#4a3a2b;color:#ffe0b2!important}.file-chip span{flex:none;font-size:12px}.reference-inline{white-space:normal}.answer-code{position:relative;margin:12px 0;border:1px solid #51473c;border-radius:11px;background:#171615}.answer-code pre{max-width:100%;margin:0;padding:34px 12px 12px;overflow-x:auto;white-space:pre;color:#f0e8dc;font:12px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace;scrollbar-width:thin;scrollbar-color:#75634f transparent}.answer-code pre code{padding:0;border:0;background:none;color:inherit;font:inherit}.copy-button{padding:5px 8px;border:1px solid #69543d;border-radius:7px;background:#383129;color:#f2d2aa;font-size:11px;line-height:1.3}.answer-code>.copy-button{position:absolute;top:6px;right:7px;z-index:1}.answer-table{max-width:100%;margin:10px 0;overflow-x:auto;border:1px solid #51473c;border-radius:9px}.answer-table table{width:100%;border-collapse:collapse;font-size:12px}.answer-table th,.answer-table td{min-width:80px;padding:7px 9px;border-bottom:1px solid #51473c;text-align:left;vertical-align:top}.answer-table th{background:#37312b;color:#f4d4ac}.answer-table tr:last-child td{border-bottom:0}.answer-sources{margin-top:11px;border-top:1px solid #51473c;padding-top:8px}.answer-sources summary{width:max-content;max-width:100%;color:#e4c39a;font-size:11px;font-weight:700;cursor:pointer}.answer-sources summary:focus-visible,.answer-actions button:focus-visible,.copy-button:focus-visible{outline:2px solid #f1ba79;outline-offset:2px}.answer-sources ul{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 2px;padding:0;list-style:none}.answer-actions{display:flex;flex-wrap:wrap;align-items:center;gap:7px;margin-top:11px;padding-top:9px;border-top:1px solid #51473c}.feedback-label{margin-left:auto;color:#c7b8a6;font-size:10px}.feedback-button{width:28px;height:27px;padding:0;border:1px solid #51473c;border-radius:7px;background:#332f2a;color:#f4e7d7;font-size:12px}.feedback-button:hover,.feedback-button.selected{border-color:#b78048;background:#59412b}.typing{display:flex;align-items:center;gap:8px;color:#ead6bf!important;font-size:12px}.typing-dots{display:inline-flex;gap:3px}.typing-dots i{width:4px;height:4px;border-radius:50%;background:#d99b61;animation:typing-pulse 1s infinite ease-in-out}.typing-dots i:nth-child(2){animation-delay:.15s}.typing-dots i:nth-child(3){animation-delay:.3s}@keyframes typing-pulse{0%,60%,100%{opacity:.35;transform:translateY(0)}30%{opacity:1;transform:translateY(-3px)}}.jump-latest{position:sticky;bottom:4px;display:block;margin:10px auto 0;padding:7px 11px;border:1px solid #d6aa7d;border-radius:999px;background:#fff9ef;color:#754322;font-size:11px;font-weight:700;box-shadow:0 3px 12px #38241124}.foot{position:sticky;bottom:0;flex:none}.foot textarea{flex:1;min-width:0;max-height:132px;min-height:42px;resize:none;overflow-y:auto;padding:11px 13px;border:1px solid transparent;border-radius:13px;background:#fffaf1;color:var(--ink);box-shadow:inset 0 0 0 1px #e7d6bf;font-family:inherit;font-size:13px;line-height:1.4;transition:box-shadow .16s,border-color .16s}.foot textarea::placeholder{color:#8a7863}.foot textarea:focus{border-color:#d79b63;box-shadow:0 0 0 3px #d99b6330;outline:0}.foot button{min-width:58px}
:host-context(html[data-color-mode="dark"]) .panel{background:#211e1a;border-color:#514638;color:#f0e7d9}:host-context(html[data-color-mode="dark"]) .head,:host-context(html[data-color-mode="dark"]) .foot{background:linear-gradient(120deg,#29241e,#211e1a);border-color:#514638}:host-context(html[data-color-mode="dark"]) .body{background:radial-gradient(ellipse at top right,#49372444,transparent 42%),#211e1a;scrollbar-color:#75634f transparent}:host-context(html[data-color-mode="dark"]) .repo-sha,:host-context(html[data-color-mode="dark"]) .muted{color:#bcb0a1}:host-context(html[data-color-mode="dark"]) .icon-button{color:#d2c4b0}:host-context(html[data-color-mode="dark"]) .icon-button:hover{background:#41372b;color:#fff0db}:host-context(html[data-color-mode="dark"]) .status-card,:host-context(html[data-color-mode="dark"]) .empty-state,:host-context(html[data-color-mode="dark"]) .suggestion,:host-context(html[data-color-mode="dark"]) .jump-latest{border-color:#65513a;background:#30291f;color:#f1d5b1}:host-context(html[data-color-mode="dark"]) .summary>p:first-of-type,:host-context(html[data-color-mode="dark"]) .summary ul{color:#e3d8c8}:host-context(html[data-color-mode="dark"]) .summary h3{color:#cdb99e}:host-context(html[data-color-mode="dark"]) .summary .uncertainty{border-color:#b07a43;background:#342a1f;color:#e7d4bb}:host-context(html[data-color-mode="dark"]) .foot textarea{background:#2d2924;color:#f4ecdf;box-shadow:inset 0 0 0 1px #62513d}:host-context(html[data-color-mode="dark"]) .foot textarea::placeholder{color:#b7a58d}:host-context(html[data-color-mode="dark"]) .file-chip{border-color:#67533a;background:#392f23;color:#f5d19d!important}:host-context(html[data-color-mode="dark"]) .answer-content h1,:host-context(html[data-color-mode="dark"]) .answer-content h2,:host-context(html[data-color-mode="dark"]) .answer-content h3,:host-context(html[data-color-mode="dark"]) .answer-content h4{color:#f4dfc5}:host-context(html[data-color-mode="dark"]) .answer-sources summary{color:#f0c58d}:host-context(html[data-color-mode="dark"]) .answer-actions{border-color:#51473c}
@media(prefers-color-scheme:dark){:host-context(html:not([data-color-mode="light"])) .panel{background:#211e1a;border-color:#514638;color:#f0e7d9}:host-context(html:not([data-color-mode="light"])) .head,:host-context(html:not([data-color-mode="light"])) .foot{background:linear-gradient(120deg,#29241e,#211e1a);border-color:#514638}:host-context(html:not([data-color-mode="light"])) .body{background:radial-gradient(ellipse at top right,#49372444,transparent 42%),#211e1a}:host-context(html:not([data-color-mode="light"])) .foot textarea{background:#2d2924;color:#f4ecdf;box-shadow:inset 0 0 0 1px #62513d}:host-context(html:not([data-color-mode="light"])) .foot textarea::placeholder{color:#b7a58d}:host-context(html:not([data-color-mode="light"])) .summary>p:first-of-type,:host-context(html:not([data-color-mode="light"])) .summary ul{color:#e3d8c8}:host-context(html:not([data-color-mode="light"])) .summary h3{color:#cdb99e}:host-context(html:not([data-color-mode="light"])) .suggestion,:host-context(html:not([data-color-mode="light"])) .empty-state{border-color:#65513a;background:#30291f;color:#f1d5b1}:host-context(html:not([data-color-mode="light"])) .file-chip{border-color:#67533a;background:#392f23;color:#f5d19d!important}}
`;

function Widget({ repo }) {
  const [open, setOpen] = useState(false), [expanded, setExpanded] = useState(false), [signedIn, setSignedIn] = useState(null), [snapshot, setSnapshot] = useState(null);
  const [summary, setSummary] = useState(null), [messages, setMessages] = useState([]), [question, setQuestion] = useState('');
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [serverStatus, setServerStatus] = useState('');
  const closeRef = useRef(null), bodyRef = useRef(null), tailRef = useRef(null), textareaRef = useRef(null), atBottomRef = useRef(true);
  const [showJump, setShowJump] = useState(false);
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
  useEffect(() => {
    if (!open || !atBottomRef.current) { if (open) setShowJump(true); return; }
    scrollBodyToLatest();
    setShowJump(false);
  }, [messages, busy, error, open]);
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
  function resizeQuestion(value) {
    setQuestion(value);
    requestAnimationFrame(() => {
      const field = textareaRef.current;
      if (field) { field.style.height = 'auto'; field.style.height = `${Math.min(field.scrollHeight, 132)}px`; }
    });
  }
  function onBodyScroll() {
    const element = bodyRef.current;
    if (!element) return;
    atBottomRef.current = element.scrollHeight - element.scrollTop - element.clientHeight < 72;
    if (atBottomRef.current) setShowJump(false);
  }
  function jumpToLatest() {
    atBottomRef.current = true;
    scrollBodyToLatest();
    setShowJump(false);
  }
  function scrollBodyToLatest() {
    const body = bodyRef.current;
    if (!body) return;
    if (typeof body.scrollTo === 'function') body.scrollTo({ top: body.scrollHeight, behavior: 'smooth' });
    else body.scrollTop = body.scrollHeight;
  }
  async function signOut() {
    setBusy(true); setError('');
    try {
      await send({ type: 'LOGOUT' });
    } catch (e) { setError(e.message); }
    finally { setSignedIn(false); setSnapshot(null); setSummary(null); setMessages([]); setBusy(false); }
  }
  return <>
    <button className="launcher" aria-label={open ? 'Close GitLens' : 'Open GitLens'} aria-expanded={open} onClick={() => setOpen(!open)}><span aria-hidden="true">GL</span></button>
    {open && <section className={`panel${expanded ? ' expanded' : ''}`} role="dialog" aria-label="GitLens repository assistant" onKeyDown={e => { if (e.key === 'Escape') setOpen(false); }}>
      <header className="head"><div className="repo-title"><span className="repo-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="6" cy="5" r="2"/><circle cx="18" cy="19" r="2"/><circle cx="18" cy="5" r="2"/><path d="M6 7v10a2 2 0 0 0 2 2h8M8 5h8"/></svg></span><div><div className="repo-name" title={`${repo.owner}/${repo.name}`}>{repo.owner}/{repo.name}</div><div className="repo-sha">{snapshot?.sha ? snapshot.sha.slice(0, 12) : 'Repository assistant'}</div></div></div><div className="actions">{signedIn && <button className="signout" disabled={busy} onClick={signOut}>Sign out</button>}<button className="icon-button" aria-label={expanded ? 'Restore panel size' : 'Expand panel'} title={expanded ? 'Restore panel size' : 'Expand panel'} onClick={() => setExpanded(!expanded)}>{expanded ? '↙' : '↗'}</button><button className="icon-button" ref={closeRef} aria-label="Close panel" title="Close panel" onClick={() => setOpen(false)}>×</button></div></header>
      <div className="body" ref={bodyRef} onScroll={onBodyScroll}>
        {signedIn === false && <div className="empty-state"><h2>Welcome to GitLens</h2><p>Sign in to explore this public repository and ask questions about its code.</p><button className="primary" onClick={() => send({ type: 'OPEN_AUTH' }).catch(e => setError(e.message))}>Continue with GitHub</button></div>}
        {signedIn === null && !error && <div className="empty-state"><h2>Connecting to GitLens</h2><p>Checking your sign-in and preparing this repository…</p></div>}
        {serverStatus && <p className="muted" role="status">{serverStatus}</p>}
        {snapshot && !['ready','failed','unsupported'].includes(snapshot.status) && <p className="status-card" role="status">Preparing repository snapshot: {snapshot.status}.</p>}
        {snapshot?.status === 'failed' && <div className="empty-state"><h2>Analysis needs another try</h2><p>The repository analysis did not finish successfully.</p><button className="primary" onClick={async () => { try { const result = await api('POST', `/api/snapshots/${snapshot.snapshotId}/retry`); setSnapshot(result); setError(''); } catch (e) { setError(e.message); } }}>Retry analysis</button></div>}
        {snapshot?.status === 'unsupported' && <div className="empty-state"><h2>Repository is too large to analyze</h2><p>This repository cannot be analyzed within the current limits.</p></div>}
        {summary && <div className="summary"><h2>Repository overview</h2><p>{summary.purpose}</p>{summary.components?.length > 0 && <><h3>Components</h3><ul>{summary.components.map((x,i) => <li key={i}>{x}</li>)}</ul></>}{summary.entrypoints?.length > 0 && <><h3>Entry points</h3><ul>{summary.entrypoints.map((x,i) => <li key={i}>{x}</li>)}</ul></>}{summary.technologies?.length > 0 && <><h3>Technologies</h3><ul>{summary.technologies.map((x,i) => <li key={i}>{x}</li>)}</ul></>}{summary.uncertainty && <p className="uncertainty">{summary.uncertainty}</p>}{summary.suggestedQuestions?.length > 0 && <><h3>Suggested questions</h3><div className="suggestions">{summary.suggestedQuestions.map((x,i) => <button className="suggestion" key={i} disabled={busy} onClick={() => submitQuestion(x)}>{x}</button>)}</div></>}</div>}
        <div className="conversation" role="log" aria-live="polite" aria-label="Conversation">
          {messages.map((m,i) => <div className={`message ${m.role}`} key={i}><strong>{m.role === 'user' ? 'You' : <span className="ai-badge"><span className="ai-spark" aria-hidden="true">✦</span>GitLens AI</span>}</strong>{m.role === 'assistant' ? <AnswerMessage content={m.content} citations={m.citations} repo={repo} sha={snapshot?.sha}/> : <p>{m.content}</p>}</div>)}
          {busy && <div className="message assistant typing" role="status"><span>GitLens is checking repository context</span><span className="typing-dots" aria-hidden="true"><i/><i/><i/></span></div>}
          {error && <p className="error" role="alert">{error}</p>}
          <div ref={tailRef} aria-hidden="true"/>
        </div>
        {showJump && <button className="jump-latest" type="button" onClick={jumpToLatest}>↓ Jump to latest</button>}
      </div>
      {snapshot?.status === 'ready' && <form className="foot" onPointerDown={e => e.stopPropagation()} onClick={e => e.stopPropagation()} onSubmit={ask}><textarea ref={textareaRef} rows={1} aria-label="Ask about this repository" value={question} onChange={e => resizeQuestion(e.target.value)} onPointerDown={e => { e.stopPropagation(); e.currentTarget.focus(); }} onClick={e => { e.stopPropagation(); e.currentTarget.focus(); }} onKeyDown={e => { e.stopPropagation(); if (e.key === 'Escape') setOpen(false); else if (e.key === 'Enter' && !e.shiftKey) ask(e); }} placeholder="Ask me about this repository…"/><button aria-label={busy ? 'Sending question' : 'Ask question'} disabled={busy || !question.trim()}>{busy ? '…' : 'Send'}</button></form>}
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
