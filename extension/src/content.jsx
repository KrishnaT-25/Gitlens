import { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { parseRepositoryUrl } from './url.js';
import { api, send } from './bridge.js';
import { AnswerMessage } from './answer.jsx';

const styles = `
.message.assistant .answer-content{white-space:normal}
.message.assistant .answer-content pre{white-space:pre}
.message.assistant .answer-content :not(pre)>code{white-space:pre-wrap}
.chat-error button{margin-top:8px}
.foot.answer-composer{flex-wrap:wrap}
.answer-length{flex-basis:100%;display:flex;align-items:center;gap:8px;font-size:12px;color:inherit}
.answer-length select{font:inherit;color:inherit;background:transparent;border:1px solid #a58d70;border-radius:8px;padding:4px 7px;color-scheme:light dark}
.answer-length select option{color:CanvasText;background:Canvas}
:host{all:initial;--paper:#f6f0e4;--paper-deep:#efe5d4;--ink:#272624;--muted:#797267;--line:#e2d4bf;--amber:#d78b42;--amber-dark:#a95d27;--amber-soft:#f3dfc8;--green:#3e9b5a;font-family:ui-sans-serif,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--ink)}
*{box-sizing:border-box}button,input{font:inherit}button{cursor:pointer}button:disabled{cursor:wait;opacity:.58}button:focus-visible,input:focus-visible{outline:3px solid #dc9b5f;outline-offset:2px}
.launcher{position:fixed;right:24px;bottom:24px;z-index:2147483647;width:58px;height:58px;border:1px solid #edc89f;border-radius:20px;background:linear-gradient(145deg,#fff8e9,#e7b477);color:#392719;font-weight:800;font-size:17px;letter-spacing:-.06em;box-shadow:0 10px 28px #25170e4d;transition:transform .18s ease,box-shadow .18s ease}.launcher:hover{transform:translateY(-2px);box-shadow:0 14px 32px #25170e5c}
.panel{position:fixed;right:24px;bottom:94px;width:min(440px,calc(100vw - 28px));height:min(700px,calc(100vh - 112px));z-index:2147483647;display:flex;flex-direction:column;overflow:hidden;border:1px solid #e6d8c4;border-radius:25px;background:var(--paper);box-shadow:0 24px 70px #0006,0 2px 8px #6d50301a;font:14px/1.5 ui-sans-serif,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--ink);animation:panel-in .2s ease-out}.panel.expanded{width:min(620px,calc(100vw - 28px));height:min(850px,calc(100vh - 112px))}@keyframes panel-in{from{opacity:0;transform:translateY(8px) scale(.985)}to{opacity:1;transform:translateY(0) scale(1)}}
.head{min-height:78px;padding:15px 18px;display:flex;justify-content:space-between;align-items:center;gap:12px;background:linear-gradient(120deg,#fff9ed,#f1e4d0);border-bottom:1px solid var(--line)}.repo-title{display:flex;align-items:center;gap:11px;min-width:0}.repo-mark{position:relative;display:grid;place-items:center;flex:none;width:34px;height:34px;color:#615b51}.repo-mark svg{width:29px;height:29px}.repo-mark:after{content:"";position:absolute;right:0;bottom:1px;width:10px;height:10px;border:2px solid var(--paper);border-radius:50%;background:var(--green)}.repo-name{max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:15px;font-weight:750;letter-spacing:-.025em}.repo-sha{color:var(--muted);font-size:12px;letter-spacing:.035em}.head .actions{display:flex;align-items:center;gap:5px;flex:none}.icon-button{display:grid;place-items:center;width:34px;height:34px;padding:0;border:0;border-radius:10px;background:transparent;color:#786f63;font-size:22px;line-height:1;transition:background .16s,color .16s}.icon-button:hover{background:#eaddc8;color:#362d24}.head button.signout{padding:7px 10px;border:1px solid #ddc8ab;border-radius:10px;background:#fffaf1;color:#76512f;font-size:12px;font-weight:650}.head button.signout:hover{background:#f8ead5}
.body{flex:1;overflow:auto;padding:18px 18px 22px;background:radial-gradient(ellipse at top right,#fff9ec88,transparent 42%),var(--paper);scrollbar-color:#c9ae8d transparent;scrollbar-width:thin}.body>p{margin:8px 0 14px}.status-card{padding:12px 14px;border:1px solid #e8d8c2;border-radius:13px;background:#fffaf1;color:#5e5549}.status-card:before{content:"";display:inline-block;width:8px;height:8px;margin:0 8px 1px 0;border-radius:50%;background:var(--amber)}.summary h2{margin:2px 0 8px;font-size:22px;line-height:1.2;letter-spacing:-.04em}.summary>p:first-of-type{margin:0 0 18px;color:#4f4b44;font-size:14px}.summary h3{margin:19px 0 8px;color:#61584c;font-size:12px;font-weight:750;letter-spacing:.11em;text-transform:uppercase}.summary ul{padding-left:20px;margin:5px 0 12px;color:#45413b}.summary li{padding:2px 0}.summary .muted,.muted{color:var(--muted)}.summary .uncertainty{padding:10px 12px;border-left:1px solid #d7b58d;background:#f1e6d6;border-radius:0 10px 10px 0;font-size:12px}
.suggestions{display:grid;gap:8px;margin:9px 0 20px}.suggestion{width:100%;padding:10px 13px;border:1px solid #d6aa7d;border-radius:13px;background:#fff9ef;color:#754322;text-align:left;font-size:13px;line-height:1.4;transition:transform .15s,background .15s,border-color .15s}.suggestion:hover:not(:disabled){transform:translateY(-1px);background:#fff2df;border-color:#c8874d}
.message{max-width:94%;margin:14px 0;padding:13px 15px;border:1px solid transparent;border-radius:18px;white-space:pre-wrap;overflow-wrap:anywhere;box-shadow:0 3px 10px #47341d0d}.message>strong{display:inline-flex;align-items:center;gap:7px;margin-bottom:4px;font-size:12px;font-weight:750;letter-spacing:.015em}.message>p{margin:4px 0 0}.message.user{margin-left:auto;border-radius:18px 18px 5px 18px;background:linear-gradient(135deg,#d99754,#c47b39);color:#fffaf4}.message.user>strong{color:#fff2df}.message.assistant{margin-right:auto;border-radius:18px 18px 18px 5px;background:linear-gradient(145deg,#292928,#1c1c1b);color:#f8f4ec;box-shadow:0 8px 18px #17110b24}.message.assistant>strong{color:#edd2b4}.ai-badge{display:inline-flex;align-items:center;gap:6px;padding:4px 9px;border:1px solid #ffffff17;border-radius:999px;background:#ffffff12;color:#ead5bf}.ai-spark{color:#d986ee;font-size:14px}.citations{display:flex;flex-wrap:wrap;gap:6px;padding:0;margin:11px 0 0;list-style:none}.citations a{display:inline-block;padding:4px 8px;border:1px solid #e4c49f;border-radius:8px;background:#fff7ea;color:#8f5429;font-size:11px;text-decoration:none}.citations a:hover{background:#f8e6ce;text-decoration:underline}.assistant .citations a{border-color:#595045;background:#37322c;color:#f0c48e}.error{padding:10px 12px;border:1px solid #eac4b9;border-radius:11px;background:#fff0e9;color:#a33c2b;font-size:13px}
.foot{display:flex;align-items:center;gap:9px;padding:12px 14px 14px;border-top:1px solid var(--line);background:linear-gradient(180deg,#f5ecde,#fff8eb)}.foot input{flex:1;min-width:0;padding:11px 13px;border:1px solid transparent;border-radius:13px;background:#fffaf1;color:var(--ink);box-shadow:inset 0 0 0 1px #e7d6bf;font-size:13px;transition:box-shadow .16s,border-color .16s}.foot input::placeholder{color:#9b8973}.foot input:focus{border-color:#d79b63;box-shadow:0 0 0 3px #d99b6330;outline:0}.foot button,.primary{min-width:48px;min-height:42px;padding:9px 14px;border:0;border-radius:13px;background:linear-gradient(135deg,#ed9c36,#d87b20);color:white;font-size:13px;font-weight:750;box-shadow:0 4px 10px #bc6a2833;transition:filter .16s,transform .16s}.foot button:hover:not(:disabled),.primary:hover:not(:disabled){filter:brightness(1.05);transform:translateY(-1px)}.primary.secondary{border:1px solid #d7b38d;background:#fff8ec;color:#744a29;box-shadow:none}.empty-state{padding:18px;border:1px solid #e6d7c3;border-radius:16px;background:#fff9ef;color:#625a4e}.empty-state h2{margin:0 0 6px;font-size:17px}.empty-state p{margin:0;color:var(--muted)}
@media(max-width:520px){.panel{right:10px;bottom:82px;width:calc(100vw - 20px);height:min(720px,calc(100vh - 96px));border-radius:21px}.panel.expanded{width:calc(100vw - 20px);height:calc(100vh - 96px)}.head{padding:12px 14px}.body{padding:15px}.repo-name{max-width:190px}.launcher{right:16px;bottom:16px}}
.repo-title>div{min-width:0}.repo-sha{max-width:250px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.conversation{min-height:1px}.message.assistant{max-width:100%;padding:13px 15px}.answer-content{font-size:13px;line-height:1.6;overflow-wrap:anywhere}.answer-content>:first-child{margin-top:2px}.answer-content>:last-child{margin-bottom:2px}.answer-content p{margin:8px 0;color:inherit}.answer-content h1,.answer-content h2,.answer-content h3,.answer-content h4{margin:15px 0 7px;color:#f4dfc5;line-height:1.3}.answer-content h1{font-size:18px}.answer-content h2{font-size:16px}.answer-content h3{font-size:14px}.answer-content ul,.answer-content ol{padding-left:20px;margin:8px 0 12px}.answer-content li{padding:2px 0}.answer-content li+li{margin-top:3px}.answer-content blockquote{margin:10px 0;padding:3px 0 3px 12px;border-left:3px solid #bc8a57;color:#e4d5c2}.answer-content a:not(.file-chip){color:#f2c891;text-decoration:underline;text-underline-offset:2px}.answer-content code{padding:1px 4px;border:1px solid #ffffff22;border-radius:5px;background:#ffffff10;color:#f0d3ad;font:12px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace}.file-chip{display:inline-flex;align-items:center;gap:5px;max-width:100%;padding:2px 7px;border:1px solid #594b3e;border-radius:7px;background:#39332c;color:#f3c98f!important;font:11px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace;text-decoration:none!important;overflow-wrap:anywhere;vertical-align:baseline}.file-chip:hover{border-color:#c68b4e;background:#4a3a2b;color:#ffe0b2!important}.file-chip span{flex:none;font-size:12px}.reference-inline{white-space:normal}.answer-code{position:relative;margin:12px 0;border:1px solid #51473c;border-radius:11px;background:#171615}.answer-code pre{max-width:100%;margin:0;padding:34px 12px 12px;overflow-x:auto;white-space:pre;color:#f0e8dc;font:12px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace;scrollbar-width:thin;scrollbar-color:#75634f transparent}.answer-code pre code{padding:0;border:0;background:none;color:inherit;font:inherit}.copy-button{padding:5px 8px;border:1px solid #69543d;border-radius:7px;background:#383129;color:#f2d2aa;font-size:11px;line-height:1.3}.answer-code>.copy-button{position:absolute;top:6px;right:7px;z-index:1}.answer-table{max-width:100%;margin:10px 0;overflow-x:auto;border:1px solid #51473c;border-radius:9px}.answer-table table{width:100%;border-collapse:collapse;font-size:12px}.answer-table th,.answer-table td{min-width:80px;padding:7px 9px;border-bottom:1px solid #51473c;text-align:left;vertical-align:top}.answer-table th{background:#37312b;color:#f4d4ac}.answer-table tr:last-child td{border-bottom:0}.answer-sources{margin-top:11px;border-top:1px solid #51473c;padding-top:8px}.answer-sources summary{width:max-content;max-width:100%;color:#e4c39a;font-size:11px;font-weight:700;cursor:pointer}.answer-sources summary:focus-visible,.answer-actions button:focus-visible,.copy-button:focus-visible{outline:2px solid #f1ba79;outline-offset:2px}.answer-sources ul{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0 2px;padding:0;list-style:none}.answer-actions{display:flex;flex-wrap:wrap;align-items:center;gap:7px;margin-top:11px;padding-top:9px;border-top:1px solid #51473c}.feedback-label{margin-left:auto;color:#c7b8a6;font-size:10px}.feedback-button{width:28px;height:27px;padding:0;border:1px solid #51473c;border-radius:7px;background:#332f2a;color:#f4e7d7;font-size:12px}.feedback-button:hover,.feedback-button.selected{border-color:#b78048;background:#59412b}.typing{display:flex;align-items:center;gap:8px;color:#ead6bf!important;font-size:12px}.typing-dots{display:inline-flex;gap:3px}.typing-dots i{width:4px;height:4px;border-radius:50%;background:#d99b61;animation:typing-pulse 1s infinite ease-in-out}.typing-dots i:nth-child(2){animation-delay:.15s}.typing-dots i:nth-child(3){animation-delay:.3s}@keyframes typing-pulse{0%,60%,100%{opacity:.35;transform:translateY(0)}30%{opacity:1;transform:translateY(-3px)}}.jump-latest{position:sticky;bottom:4px;display:block;margin:10px auto 0;padding:7px 11px;border:1px solid #d6aa7d;border-radius:999px;background:#fff9ef;color:#754322;font-size:11px;font-weight:700;box-shadow:0 3px 12px #38241124}.foot{position:sticky;bottom:0;flex:none}.foot textarea{flex:1;min-width:0;max-height:132px;min-height:42px;resize:none;overflow-y:auto;padding:11px 13px;border:1px solid transparent;border-radius:13px;background:#fffaf1;color:var(--ink);box-shadow:inset 0 0 0 1px #e7d6bf;font-family:inherit;font-size:13px;line-height:1.4;transition:box-shadow .16s,border-color .16s}.foot textarea::placeholder{color:#8a7863}.foot textarea:focus{border-color:#d79b63;box-shadow:0 0 0 3px #d99b6330;outline:0}.foot button{min-width:58px}
:host-context(html[data-color-mode="dark"]) .panel{background:#211e1a;border-color:#514638;color:#f0e7d9}:host-context(html[data-color-mode="dark"]) .head,:host-context(html[data-color-mode="dark"]) .foot{background:linear-gradient(120deg,#29241e,#211e1a);border-color:#514638}:host-context(html[data-color-mode="dark"]) .body{background:radial-gradient(ellipse at top right,#49372444,transparent 42%),#211e1a;scrollbar-color:#75634f transparent}:host-context(html[data-color-mode="dark"]) .repo-sha,:host-context(html[data-color-mode="dark"]) .muted{color:#bcb0a1}:host-context(html[data-color-mode="dark"]) .icon-button{color:#d2c4b0}:host-context(html[data-color-mode="dark"]) .icon-button:hover{background:#41372b;color:#fff0db}:host-context(html[data-color-mode="dark"]) .status-card,:host-context(html[data-color-mode="dark"]) .empty-state,:host-context(html[data-color-mode="dark"]) .suggestion,:host-context(html[data-color-mode="dark"]) .jump-latest{border-color:#65513a;background:#30291f;color:#f1d5b1}:host-context(html[data-color-mode="dark"]) .summary>p:first-of-type,:host-context(html[data-color-mode="dark"]) .summary ul{color:#e3d8c8}:host-context(html[data-color-mode="dark"]) .summary h3{color:#cdb99e}:host-context(html[data-color-mode="dark"]) .summary .uncertainty{border-color:#b07a43;background:#342a1f;color:#e7d4bb}:host-context(html[data-color-mode="dark"]) .foot textarea{background:#2d2924;color:#f4ecdf;box-shadow:inset 0 0 0 1px #62513d}:host-context(html[data-color-mode="dark"]) .foot textarea::placeholder{color:#b7a58d}:host-context(html[data-color-mode="dark"]) .file-chip{border-color:#67533a;background:#392f23;color:#f5d19d!important}:host-context(html[data-color-mode="dark"]) .answer-content h1,:host-context(html[data-color-mode="dark"]) .answer-content h2,:host-context(html[data-color-mode="dark"]) .answer-content h3,:host-context(html[data-color-mode="dark"]) .answer-content h4{color:#f4dfc5}:host-context(html[data-color-mode="dark"]) .answer-sources summary{color:#f0c58d}:host-context(html[data-color-mode="dark"]) .answer-actions{border-color:#51473c}
@media(prefers-color-scheme:dark){:host-context(html:not([data-color-mode="light"])) .panel{background:#211e1a;border-color:#514638;color:#f0e7d9}:host-context(html:not([data-color-mode="light"])) .head,:host-context(html:not([data-color-mode="light"])) .foot{background:linear-gradient(120deg,#29241e,#211e1a);border-color:#514638}:host-context(html:not([data-color-mode="light"])) .body{background:radial-gradient(ellipse at top right,#49372444,transparent 42%),#211e1a}:host-context(html:not([data-color-mode="light"])) .foot textarea{background:#2d2924;color:#f4ecdf;box-shadow:inset 0 0 0 1px #62513d}:host-context(html:not([data-color-mode="light"])) .foot textarea::placeholder{color:#b7a58d}:host-context(html:not([data-color-mode="light"])) .summary>p:first-of-type,:host-context(html:not([data-color-mode="light"])) .summary ul{color:#e3d8c8}:host-context(html:not([data-color-mode="light"])) .summary h3{color:#cdb99e}:host-context(html:not([data-color-mode="light"])) .suggestion,:host-context(html:not([data-color-mode="light"])) .empty-state{border-color:#65513a;background:#30291f;color:#f1d5b1}:host-context(html:not([data-color-mode="light"])) .file-chip{border-color:#67533a;background:#392f23;color:#f5d19d!important}}
/* Repository workbench: one reading surface, one clear action, and restrained source cues. */
:host{--surface:#fff;--canvas:#f5f7f8;--ink:#17212b;--muted:#526170;--line:#dbe2e7;--accent:#176ca7;--accent-hover:#105783;--focus:#2a83c5;font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
button,textarea,select{touch-action:manipulation}
button:focus-visible,textarea:focus-visible,select:focus-visible,.answer-content a:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
.launcher{width:56px;height:56px;border:1px solid #4d6b82;border-radius:16px;background:#172d40;color:#fff;font-size:15px;letter-spacing:-.03em;box-shadow:0 12px 30px #0f273a42}
.launcher:hover{background:#24455e;box-shadow:0 15px 34px #0f273a52}
.panel,.panel.expanded{border:1px solid #cad5dd;border-radius:18px;background:var(--surface);color:var(--ink);box-shadow:0 18px 54px #14263538,0 2px 8px #1426351c;font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}
.head{min-height:76px;padding:14px 16px;background:#172d40;border-color:#172d40;color:#fff}
.repo-mark{width:36px;height:36px;color:#bed7e8}.repo-mark:after{width:8px;height:8px;right:1px;bottom:2px;border-color:#172d40;background:#63c194}
.repo-name{max-width:250px;font-size:15px;font-weight:700;letter-spacing:-.015em}
.repo-sha{max-width:250px;color:#b5c9d8;font-size:11px;letter-spacing:0}
.head .actions{gap:4px}.icon-button{width:40px;height:40px;color:#d7e6ef;border-radius:9px}
.icon-button:hover{background:#33516a;color:#fff}.head button.signout{min-height:38px;padding:7px 9px;border-color:#557085;background:transparent;color:#e9f3f9;font-size:12px}
.head button.signout:hover{background:#33516a;color:#fff}
.body{padding:18px 18px 24px;background:var(--canvas);scrollbar-color:#a9b8c3 transparent}
.summary{padding:0 0 4px}.summary h2{margin:0 0 9px;font-size:21px;line-height:1.25;letter-spacing:-.025em;text-wrap:balance}
.summary>p:first-of-type{margin:0 0 18px;color:#384958;font-size:14px;line-height:1.65}
.summary h3{margin:20px 0 8px;color:#344657;font-size:12px;font-weight:700;letter-spacing:0;text-transform:none}
.summary ul{margin:0 0 12px;padding-left:18px;color:#394957}.summary li{padding:3px 0}
.summary .uncertainty{padding:10px 12px;border:1px solid #e1d5b8;border-radius:9px;background:#fff9e9;color:#5b4c2c;font-size:12px}
.suggestions{gap:7px;margin:9px 0 18px}.suggestion{min-height:44px;padding:10px 12px;border:1px solid #cbdbe6;border-radius:10px;background:#fff;color:#12557e;font-size:13px;font-weight:600;box-shadow:none;transition:background .15s,border-color .15s}
.suggestion:hover:not(:disabled){transform:none;border-color:#77a7c7;background:#edf6fb}
.status-card,.empty-state{border:1px solid var(--line);border-radius:12px;background:#fff;color:var(--ink);box-shadow:none}
.status-card:before{background:#d39a39}.empty-state h2{font-size:18px;letter-spacing:-.02em}.empty-state p{color:#526170}.empty-state .primary{margin-top:14px}
.error{border:1px solid #e7b9b4;border-radius:10px;background:#fff3f1;color:#8c302b}
.message{max-width:100%;margin:16px 0;padding:0;border:0;border-radius:0;box-shadow:none}
.message>strong{margin-bottom:6px;color:#415569;font-size:11px;font-weight:700;letter-spacing:0}
.message.user{max-width:90%;margin-left:auto;padding:10px 13px;border:1px solid #d4e4ed;border-radius:11px;background:#e8f2f8;color:#203746}
.message.user>strong{color:#315874}.message.assistant{max-width:100%;margin:18px 0;padding:15px 16px;border:1px solid #dbe2e7;border-radius:12px;background:#fff;color:#233441;box-shadow:none}
.message.assistant>strong{color:#315874}.ai-badge{padding:0;border:0;border-radius:0;background:none;color:#315874}.ai-spark{color:#176ca7}
.answer-content{font-size:13px;line-height:1.65}.answer-content h1,.answer-content h2,.answer-content h3,.answer-content h4{color:#1d3445}
.answer-content blockquote{border-color:#92b9cf;color:#486171}.answer-content a:not(.file-chip){color:#0d6394}
.answer-content code{border-color:#d8e3e9;background:#f3f7f9;color:#214c65}.file-chip{border-color:#bdd7e7;background:#edf6fb;color:#075c8c!important}.file-chip:hover{border-color:#6fa6c7;background:#dceef8;color:#064c74!important}
.answer-code{border-color:#d3dde3;background:#f7f9fa}.answer-code pre{color:#243d4f}.answer-code pre code{background:none;border:0;color:inherit}.answer-code>.copy-button,.copy-button{border-color:#c6d8e3;background:#fff;color:#195f88}
.answer-table,.answer-table th,.answer-table td,.answer-sources,.answer-actions{border-color:#dbe2e7}.answer-table th{background:#eef3f6;color:#28485e}.answer-sources summary{color:#135b86}.feedback-label{color:#526170}.feedback-button{width:36px;height:36px;border-color:#cedce5;background:#fff;color:#1c4f6b}.feedback-button:hover,.feedback-button.selected{border-color:#5b99bf;background:#e6f3fa}
.typing{padding:12px 14px!important;border:1px solid #dbe2e7!important;color:#38566b!important}.typing-dots i{background:#2675a6}
.jump-latest{min-height:40px;border-color:#b7d0e1;background:#fff;color:#155c87}
.foot{padding:12px 14px 14px;border-color:var(--line);background:#fff}.composer-top{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-basis:100%;min-width:0}.composer-label{color:#263c4d;font-size:12px;font-weight:700}.answer-length{flex-basis:auto;gap:6px;color:#526170;font-size:11px}.answer-length select{min-height:32px;padding:4px 7px;border:1px solid #bacbd7;border-radius:7px;background:#fff;color:#263c4d;color-scheme:light}
.foot textarea{min-height:44px;padding:11px 12px;border:1px solid #b9cad6;border-radius:9px;background:#fff;color:var(--ink);box-shadow:none;font-size:13px;line-height:1.5}
.foot textarea::placeholder{color:#657584}.foot textarea:focus{border-color:#287bb0;box-shadow:0 0 0 3px #287bb026;outline:0}
.foot button,.primary{min-height:44px;border-radius:9px;background:#176ca7;color:#fff;box-shadow:none;font-size:13px;transition:background .15s}.foot button:hover:not(:disabled),.primary:hover:not(:disabled){transform:none;filter:none;background:#105783}.primary.secondary{border-color:#abc7d9;background:#fff;color:#135b86}
.foot button:disabled{opacity:1;background:#dce5eb;color:#526170;box-shadow:none;cursor:default}
.overview-loading{display:grid;gap:10px;padding:5px 0 18px}.overview-loading span{display:block;height:12px;border-radius:4px;background:#e3ebf0}.overview-loading span:first-child{width:52%;height:22px}.overview-loading span:last-child{width:73%}
@media(max-width:520px){.panel,.panel.expanded{right:8px;bottom:78px;width:calc(100vw - 16px);height:calc(100dvh - 92px);border-radius:14px}.head{padding:10px 11px}.body{padding:15px}.repo-name{max-width:calc(100vw - 205px)}.repo-sha{max-width:calc(100vw - 205px)}.launcher{right:16px;bottom:14px}.foot{padding:10px 11px calc(10px + env(safe-area-inset-bottom))}.foot button{min-width:54px;padding-inline:10px}}
@media(max-width:360px){.head button.signout{padding-inline:6px;font-size:11px}.head .actions{gap:0}.repo-title{gap:6px}.repo-mark{width:27px}.repo-mark svg{width:24px}.repo-name,.repo-sha{max-width:calc(100vw - 190px)}.answer-length{font-size:10px}}
@media(prefers-reduced-motion:reduce){.panel{animation:none}.launcher,.suggestion,.foot button,.primary,.typing-dots i{animation:none;transition:none} .body{scroll-behavior:auto}}
:host-context(html[data-color-mode="dark"]){--surface:#17212b;--canvas:#1b2833;--ink:#e7eff4;--muted:#b6c6d0;--line:#3a4c59;--focus:#80c4ef}
:host-context(html[data-color-mode="dark"]) .panel,:host-context(html[data-color-mode="dark"]) .body,:host-context(html[data-color-mode="dark"]) .head,:host-context(html[data-color-mode="dark"]) .foot{color:var(--ink)}
:host-context(html[data-color-mode="dark"]) .panel{background:var(--surface);border-color:#415463}:host-context(html[data-color-mode="dark"]) .head{background:#102232;border-color:#415463}:host-context(html[data-color-mode="dark"]) .body{background:var(--canvas)}:host-context(html[data-color-mode="dark"]) .foot{background:var(--surface);border-color:var(--line)}
:host-context(html[data-color-mode="dark"]) .summary>p:first-of-type,:host-context(html[data-color-mode="dark"]) .summary ul,:host-context(html[data-color-mode="dark"]) .summary h3,:host-context(html[data-color-mode="dark"]) .composer-label{color:#d1dfe8}
:host-context(html[data-color-mode="dark"]) .suggestion,:host-context(html[data-color-mode="dark"]) .empty-state,:host-context(html[data-color-mode="dark"]) .status-card,:host-context(html[data-color-mode="dark"]) .message.assistant{background:#223442;border-color:#466175;color:#e7eff4}
:host-context(html[data-color-mode="dark"]) .message.user{background:#25445a;border-color:#49718a;color:#e7eff4}:host-context(html[data-color-mode="dark"]) .message.user>strong,:host-context(html[data-color-mode="dark"]) .message.assistant>strong,:host-context(html[data-color-mode="dark"]) .ai-badge,:host-context(html[data-color-mode="dark"]) .answer-content h1,:host-context(html[data-color-mode="dark"]) .answer-content h2,:host-context(html[data-color-mode="dark"]) .answer-content h3,:host-context(html[data-color-mode="dark"]) .answer-content h4{color:#bfe2f7}
:host-context(html[data-color-mode="dark"]) .foot textarea,:host-context(html[data-color-mode="dark"]) .answer-length select{background:#223442;border-color:#617a8c;color:#f0f5f8;box-shadow:none;color-scheme:dark}:host-context(html[data-color-mode="dark"]) .foot textarea::placeholder{color:#b0c1cd}:host-context(html[data-color-mode="dark"]) .answer-length{color:#c4d3dd}
:host-context(html[data-color-mode="dark"]) .answer-content a:not(.file-chip),:host-context(html[data-color-mode="dark"]) .answer-sources summary{color:#9ed7fa}:host-context(html[data-color-mode="dark"]) .file-chip,:host-context(html[data-color-mode="dark"]) .answer-content code{background:#2d4555;border-color:#5a8197;color:#d0edff!important}:host-context(html[data-color-mode="dark"]) .answer-code{background:#182630;border-color:#50697a}:host-context(html[data-color-mode="dark"]) .answer-code pre{color:#e7eff4}:host-context(html[data-color-mode="dark"]) .answer-table th{background:#2c4150;color:#d7eaf5}:host-context(html[data-color-mode="dark"]) .feedback-label{color:#bed0da}
:host-context(html[data-color-mode="dark"]) .summary .uncertainty{border:1px solid #776649;background:#342f25;color:#eadbbf}:host-context(html[data-color-mode="dark"]) .foot button:disabled{background:#344c5b;color:#d2e4ee}:host-context(html[data-color-mode="dark"]) .overview-loading span{background:#344b5a}
@media(prefers-color-scheme:dark){:host-context(html:not([data-color-mode="light"])){--surface:#17212b;--canvas:#1b2833;--ink:#e7eff4;--muted:#b6c6d0;--line:#3a4c59;--focus:#80c4ef}:host-context(html:not([data-color-mode="light"])) .panel{background:var(--surface);border-color:#415463;color:var(--ink)}:host-context(html:not([data-color-mode="light"])) .head{background:#102232;border-color:#415463}:host-context(html:not([data-color-mode="light"])) .body{background:var(--canvas)}:host-context(html:not([data-color-mode="light"])) .foot{background:var(--surface);border-color:var(--line)}:host-context(html:not([data-color-mode="light"])) .summary>p:first-of-type,:host-context(html:not([data-color-mode="light"])) .summary ul,:host-context(html:not([data-color-mode="light"])) .summary h3,:host-context(html:not([data-color-mode="light"])) .composer-label{color:#d1dfe8}:host-context(html:not([data-color-mode="light"])) .suggestion,:host-context(html:not([data-color-mode="light"])) .empty-state,:host-context(html:not([data-color-mode="light"])) .status-card,:host-context(html:not([data-color-mode="light"])) .message.assistant{background:#223442;border-color:#466175;color:#e7eff4}:host-context(html:not([data-color-mode="light"])) .message.user{background:#25445a;border-color:#49718a;color:#e7eff4}:host-context(html:not([data-color-mode="light"])) .message.user>strong,:host-context(html:not([data-color-mode="light"])) .message.assistant>strong,:host-context(html:not([data-color-mode="light"])) .ai-badge,:host-context(html:not([data-color-mode="light"])) .answer-content h1,:host-context(html:not([data-color-mode="light"])) .answer-content h2,:host-context(html:not([data-color-mode="light"])) .answer-content h3,:host-context(html:not([data-color-mode="light"])) .answer-content h4{color:#bfe2f7}:host-context(html:not([data-color-mode="light"])) .foot textarea,:host-context(html:not([data-color-mode="light"])) .answer-length select{background:#223442;border-color:#617a8c;color:#f0f5f8;box-shadow:none;color-scheme:dark}:host-context(html:not([data-color-mode="light"])) .foot textarea::placeholder{color:#b0c1cd}:host-context(html:not([data-color-mode="light"])) .answer-length{color:#c4d3dd}:host-context(html:not([data-color-mode="light"])) .answer-content a:not(.file-chip),:host-context(html:not([data-color-mode="light"])) .answer-sources summary{color:#9ed7fa}:host-context(html:not([data-color-mode="light"])) .file-chip,:host-context(html:not([data-color-mode="light"])) .answer-content code{background:#2d4555;border-color:#5a8197;color:#d0edff!important}:host-context(html:not([data-color-mode="light"])) .answer-code{background:#182630;border-color:#50697a}:host-context(html:not([data-color-mode="light"])) .answer-code pre{color:#e7eff4}:host-context(html:not([data-color-mode="light"])) .answer-table th{background:#2c4150;color:#d7eaf5}:host-context(html:not([data-color-mode="light"])) .feedback-label{color:#bed0da}}
@media(prefers-color-scheme:dark){:host-context(html:not([data-color-mode="light"])) .summary .uncertainty{border:1px solid #776649;background:#342f25;color:#eadbbf}:host-context(html:not([data-color-mode="light"])) .foot button:disabled{background:#344c5b;color:#d2e4ee}:host-context(html:not([data-color-mode="light"])) .overview-loading span{background:#344b5a}}
`;

function Widget({ repo }) {
  const [answerLength, setAnswerLength] = useState('default');
  const [failedChat, setFailedChat] = useState(null);
  const [open, setOpen] = useState(false), [expanded, setExpanded] = useState(false), [signedIn, setSignedIn] = useState(null), [storedSnapshot, setSnapshot] = useState(null);
  const [resolvedUrl, setResolvedUrl] = useState(null);
  const [resolutionError, setResolutionError] = useState('');
  const conversation = useRef(null);
  const authEpoch = useRef(0);
  const snapshot = !repo.unsupportedReason && resolvedUrl === repo.url ? storedSnapshot : null;
  const [summary, setSummary] = useState(undefined), [messages, setMessages] = useState([]), [question, setQuestion] = useState('');
  const [error, setError] = useState(''), [busy, setBusy] = useState(false), [serverStatus, setServerStatus] = useState('');
  const closeRef = useRef(null), launcherRef = useRef(null), bodyRef = useRef(null), tailRef = useRef(null), textareaRef = useRef(null), atBottomRef = useRef(true);
  const [resolutionAttempt, setResolutionAttempt] = useState(0);
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
  useEffect(() => () => { conversation.current = null; }, []);
  function resetConversation(next = null) {
    conversation.current = next;
    setSummary(undefined);
    setMessages([]);
    setQuestion('');
    setAnswerLength('default');
    setFailedChat(null);
    setError('');
    setBusy(false);
    atBottomRef.current = true;
  }
  useEffect(() => {
    if (!open || repo.unsupportedReason) return;
    let cancelled = false;
    const epoch = authEpoch.current;
    const stale = () => cancelled || epoch !== authEpoch.current;
    setResolutionError('');
    (async () => {
      try {
        const auth = await send({ type: 'GET_AUTH' });
        if (stale()) return;
        setSignedIn(auth.signedIn);
        if (auth.signedIn) {
          const result = await api('POST', '/api/repositories', { url: repo.url });
          if (stale()) return;
          const key = JSON.stringify([result.snapshotId, result.sha]);
          if (conversation.current?.key !== key) resetConversation({ key });
          setSnapshot(result);
          setResolvedUrl(repo.url);
        } else {
          resetConversation();
          setSnapshot(null);
          setResolvedUrl(null);
        }
      } catch (e) { if (!stale()) setResolutionError(e.message); }
    })();
    return () => { cancelled = true; };
  }, [open, repo.url, resolutionAttempt]);
  useEffect(() => {
    if (!open || !snapshot || ['ready', 'failed', 'unsupported'].includes(snapshot.status)) return;
    let active = true;
    const owner = conversation.current;
    const current = () => active && owner === conversation.current;
    const poll = async () => {
      try { const status = await api('GET', `/api/snapshots/${snapshot.snapshotId}/status`); if (current()) setSnapshot(previous => ({ ...previous, ...status })); }
      catch (e) { if (current()) setError(e.message); }
    };
    const timer = setInterval(poll, 3000); poll();
    return () => { active = false; clearInterval(timer); };
  }, [open, snapshot?.snapshotId, snapshot?.status]);
  useEffect(() => {
    if (snapshot?.status !== 'ready' || summary !== undefined) return;
    let active = true;
    const owner = conversation.current;
    const current = () => active && owner === conversation.current;
    api('GET', `/api/snapshots/${snapshot.snapshotId}/summary`).then(x => {
      if (!current()) return;
      const value = x?.summary;
      setSummary(value ? { ...value, uncertainty: typeof value.uncertainty === 'string' ? value.uncertainty.trim() : '' } : null);
    }).catch(e => { if (current()) { setSummary(null); setError(e.message); } });
    return () => { active = false; };
  }, [snapshot?.status, snapshot?.snapshotId, summary]);
  useEffect(() => {
    if (!open || !atBottomRef.current) { if (open) setShowJump(true); return; }
    scrollBodyToLatest();
    setShowJump(false);
  }, [messages, busy, error, open]);
  async function submitQuestion(value) {
    if (!value.trim() || busy || !snapshot) return;
    const q = value.trim(); setQuestion('');
    const lastQuestion = messages.findLastIndex(message => message.role === 'user');
    const previous = lastQuestion < 0 ? [] : messages.slice(lastQuestion, lastQuestion + 2)
      .map(({ role, content }) => ({ role, content }));
    setMessages(old => [...old, { role: 'user', content: q }]);
    await sendQuestion({ requestId: crypto.randomUUID(), question: q, history: previous, answerLength });
  }
  async function sendQuestion(request) {
    if (busy || !snapshot) return;
    const owner = conversation.current;
    setBusy(true); setError(''); setFailedChat(null);
    try {
      const deadline = performance.now() + 75_000;
      const path = `/api/snapshots/${snapshot.snapshotId}/chat`;
      while (owner === conversation.current && performance.now() < deadline) {
        const result = await api('POST', path, request, deadline - performance.now()).catch(error => {
          if (error.code !== 'chat_transport') throw error;
          return { status: 'running' };
        });
        if (owner !== conversation.current) return;
        if (result.status !== 'running') {
          if (typeof result.answer !== 'string') throw new Error('GitLens returned an incomplete response. Try again.');
          setMessages(old => [...old, { role: 'assistant', content: result.answer, citations: result.citations }]);
          return;
        }
        await new Promise(resolve => setTimeout(resolve, Math.max(0, Math.min(1500, deadline - performance.now()))));
      }
      throw new Error('The chat did not finish in time. Retry answer checks the same request.');
    } catch (e) {
      if (owner !== conversation.current) return;
      setError(e.message);
      setFailedChat(e.terminal || e.code === 'chat_timeout' ? { ...request, requestId: crypto.randomUUID() } : request);
    } finally { if (owner === conversation.current) setBusy(false); }
  }
  function ask(event) { event.preventDefault(); submitQuestion(question); }
  function closePanel() {
    setOpen(false);
    launcherRef.current?.focus();
  }
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
    if (typeof body.scrollTo === 'function') body.scrollTo({ top: body.scrollHeight, behavior: globalThis.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' });
    else body.scrollTop = body.scrollHeight;
  }
  async function signOut() {
    authEpoch.current += 1;
    setBusy(true); setError('');
    try {
      await send({ type: 'LOGOUT' });
    } catch (e) { setError(e.message); }
    finally { resetConversation(); setSignedIn(false); setSnapshot(null); setResolvedUrl(null); setResolutionError(''); }
  }
  async function retryAnalysis() {
    const owner = conversation.current;
    try {
      const result = await api('POST', `/api/snapshots/${snapshot.snapshotId}/retry`);
      if (owner !== conversation.current) return;
      setSnapshot(previous => ({ ...previous, ...result }));
      setError('');
    } catch (e) {
      if (owner === conversation.current) setError(e.message);
    }
  }
  return <>
    <button className="launcher" ref={launcherRef} aria-label={open ? 'Close GitLens' : 'Open GitLens'} aria-expanded={open} onClick={() => open ? closePanel() : setOpen(true)}><span aria-hidden="true">GL</span></button>
    {open && <section className={`panel${expanded ? ' expanded' : ''}`} role="dialog" aria-label="GitLens repository assistant" onKeyDown={e => { if (e.key === 'Escape') closePanel(); }}>
      <header className="head"><div className="repo-title"><span className="repo-mark" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="6" cy="5" r="2"/><circle cx="18" cy="19" r="2"/><circle cx="18" cy="5" r="2"/><path d="M6 7v10a2 2 0 0 0 2 2h8M8 5h8"/></svg></span><div><div className="repo-name" title={`${repo.owner}/${repo.name}`}>{repo.owner}/{repo.name}</div><div className="repo-sha" title={snapshot?.sha ? `${snapshot.ref || "Commit"} · ${snapshot.sha}` : undefined}>{snapshot?.sha ? `${snapshot.ref && snapshot.ref !== snapshot.sha ? snapshot.ref + " · " : "Commit · "}${snapshot.sha.slice(0, 12)}` : 'Repository assistant'}</div></div></div><div className="actions">{signedIn && <button className="signout" disabled={busy} onClick={signOut}>Sign out</button>}<button className="icon-button" aria-label={expanded ? 'Restore panel size' : 'Expand panel'} title={expanded ? 'Restore panel size' : 'Expand panel'} onClick={() => setExpanded(!expanded)}>{expanded ? '↙' : '↗'}</button><button className="icon-button" ref={closeRef} aria-label="Close panel" title="Close panel" onClick={closePanel}>×</button></div></header>
      <div className="body" ref={bodyRef} onScroll={onBodyScroll}>
        {signedIn === false && <div className="empty-state"><h2>Welcome to GitLens</h2><p>Sign in to explore this public repository and ask questions about its code.</p><button className="primary" onClick={() => send({ type: 'OPEN_AUTH' }).catch(e => setError(e.message))}>Continue with GitHub</button></div>}
        {repo.unsupportedReason && <div className="empty-state" role="alert"><h2>Unsupported GitHub page</h2><p>{repo.unsupportedReason}</p></div>}
        {signedIn === null && !resolutionError && !repo.unsupportedReason && <div className="empty-state" role="status"><h2>Connecting to GitLens…</h2><p>Checking your sign-in and preparing this repository.</p></div>}
        {signedIn && !snapshot && !resolutionError && !repo.unsupportedReason && <p className="status-card" role="status">Resolving the repository snapshot…</p>}
        {resolutionError && !repo.unsupportedReason && <div className="error" role="alert"><p>Could not load this repository: {resolutionError}</p><button className="primary secondary" type="button" onClick={() => { setResolutionError(''); setResolutionAttempt(x => x + 1); }}>Try again</button></div>}
        {serverStatus && <p className="muted" role="status">{serverStatus}</p>}
        {snapshot && !['ready','failed','unsupported'].includes(snapshot.status) && <p className="status-card" role="status">Preparing repository snapshot: {snapshot.status}.</p>}
        {snapshot?.status === 'failed' && <div className="empty-state"><h2>Analysis needs another try</h2><p>The repository analysis did not finish successfully.</p><button className="primary" onClick={retryAnalysis}>Retry analysis</button></div>}
        {snapshot?.status === 'unsupported' && <div className="empty-state"><h2>Repository is too large to analyze</h2><p>This repository cannot be analyzed within the current limits.</p></div>}
        {snapshot?.status === 'ready' && summary === undefined && <div className="overview-loading" role="status" aria-label="Loading repository overview…"><span/><span/><span/></div>}
        {snapshot?.status === 'ready' && summary === null && <div className="empty-state"><h2>Overview unavailable</h2><p>You can still ask questions about this snapshot, or try loading the overview again.</p><button className="primary secondary" type="button" onClick={() => { setError(''); setSummary(undefined); }}>Retry overview</button></div>}
        {snapshot && summary && <div className="summary"><h2>Repository overview</h2><p>{summary.purpose}</p>{summary.components?.length > 0 && <><h3>Components</h3><ul>{summary.components.map((x,i) => <li key={i}>{x}</li>)}</ul></>}{summary.entrypoints?.length > 0 && <><h3>Entry points</h3><ul>{summary.entrypoints.map((x,i) => <li key={i}>{x}</li>)}</ul></>}{summary.technologies?.length > 0 && <><h3>Technologies</h3><ul>{summary.technologies.map((x,i) => <li key={i}>{x}</li>)}</ul></>}{summary.uncertainty && <p className="uncertainty">{summary.uncertainty}</p>}{summary.suggestedQuestions?.length > 0 && <><h3>Ask about this codebase</h3><div className="suggestions">{summary.suggestedQuestions.map((x,i) => <button className="suggestion" key={i} disabled={busy} onClick={() => submitQuestion(x)}>{x}</button>)}</div></>}</div>}
        {(snapshot || !signedIn) && <div className="conversation" role="log" aria-live="polite" aria-label="Conversation">
          {messages.map((m,i) => <div className={`message ${m.role}`} key={i}><strong>{m.role === 'user' ? 'You' : <span className="ai-badge"><span className="ai-spark" aria-hidden="true">✦</span>GitLens AI</span>}</strong>{m.role === 'assistant' ? <AnswerMessage content={m.content} citations={m.citations} repo={repo} sha={snapshot?.sha}/> : <p>{m.content}</p>}</div>)}
          {busy && <div className="message assistant typing" role="status"><span>GitLens is checking repository context</span><span className="typing-dots" aria-hidden="true"><i/><i/><i/></span></div>}
          {error && <div className="error chat-error"><div role="alert">{error}</div>{failedChat && snapshot?.status === 'ready' && <button className="primary secondary" type="button" disabled={busy} onClick={() => sendQuestion(failedChat)}>Retry answer</button>}</div>}
          <div ref={tailRef} aria-hidden="true"/>
        </div>}
        {showJump && <button className="jump-latest" type="button" onClick={jumpToLatest}>↓ Jump to latest</button>}
      </div>
      {snapshot?.status === 'ready' && <form className="foot answer-composer" onPointerDown={e => e.stopPropagation()} onClick={e => e.stopPropagation()} onSubmit={ask}>
        <div className="composer-top"><label className="composer-label" htmlFor="gitlens-question">Ask the codebase</label><label className="answer-length">
          Answer length
          <select value={answerLength} onChange={e => setAnswerLength(e.target.value)}>
            <option value="default">Default</option>
            <option value="short">Short</option>
            <option value="detailed">Detailed</option>
          </select>
        </label></div>
        <textarea id="gitlens-question" name="question" autoComplete="off" ref={textareaRef} rows={1} value={question} onChange={e => resizeQuestion(e.target.value)} onPointerDown={e => { e.stopPropagation(); e.currentTarget.focus(); }} onClick={e => { e.stopPropagation(); e.currentTarget.focus(); }} onKeyDown={e => { e.stopPropagation(); if (e.key === 'Escape') closePanel(); else if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); if (!e.nativeEvent.isComposing && e.nativeEvent.keyCode !== 229 && !e.repeat) submitQuestion(question); } }} placeholder="How does this repository work?…"/>
        <button aria-label={busy ? 'Sending question' : 'Ask question'} disabled={busy || !question.trim()}>{busy ? '…' : 'Send'}</button>
      </form>}
    </section>}
  </>;
}

let host, root, current = '';
function sync() {
  const url = location.href;
  if (url === current) return;
  current = url;
  const repo = parseRepositoryUrl(url);
  if (!repo) {
    if (root) { root.unmount(); root = null; host.remove(); host = null; }
    return;
  }
  if (!root) {
    host = document.createElement('div'); host.id = 'gitlens-root';
    const shadow = host.attachShadow({ mode: 'closed' });
    const style = document.createElement('style'); style.textContent = styles; shadow.appendChild(style);
    const mount = document.createElement('div'); shadow.appendChild(mount); document.documentElement.appendChild(host);
    root = createRoot(mount);
  }
  const page = new URL(repo.url);
  repo.url = page.origin + page.pathname;
  const key = JSON.stringify([repo.owner.toLowerCase(), repo.name.toLowerCase()]);
  root.render(<Widget key={key} repo={repo}/>);
}
sync();
addEventListener('popstate', sync);
addEventListener('turbo:load', sync);
addEventListener('pjax:end', sync);
new MutationObserver(() => { if (location.href !== current) sync(); }).observe(document.documentElement, { childList: true, subtree: true });
