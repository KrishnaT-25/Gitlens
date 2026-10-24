import { Children, cloneElement, isValidElement, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { deduplicateSources, githubFileUrl, parseFileReferences } from './references.js';

function plainText(node) {
  if (typeof node === 'string' || typeof node === 'number') return String(node);
  if (Array.isArray(node)) return node.map(plainText).join('');
  if (isValidElement(node)) return plainText(node.props.children);
  return '';
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const field = document.createElement('textarea');
    field.value = text;
    field.setAttribute('readonly', '');
    field.style.cssText = 'position:fixed;left:-9999px;top:0';
    document.body.appendChild(field);
    field.select();
    const copied = document.execCommand('copy');
    field.remove();
    if (!copied) throw new Error('Clipboard unavailable');
  }
}

function CopyButton({ text, label = 'Copy' }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    try {
      await copyText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch {
      setCopied(false);
    }
  }
  return <button className="copy-button" type="button" aria-label={`${label} ${copied ? 'copied' : ''}`.trim()} onClick={copy}>{copied ? 'Copied' : label}</button>;
}

function safeMarkdownUrl(value) {
  const url = String(value || '').trim();
  if (url.startsWith('#')) return url;
  try {
    const parsed = new URL(url);
    return ['https:', 'mailto:'].includes(parsed.protocol) ? url : '';
  } catch {
    return '';
  }
}

export function AnswerMessage({ content, citations = [], repo, sha }) {
  const [feedback, setFeedback] = useState('');
  const sources = deduplicateSources(citations);

  function fileChip(reference) {
    const cited = sources.find(source => source.path === reference.path &&
      (!reference.lineStart || !source.lineStart || (source.lineStart <= reference.lineStart && source.lineEnd >= reference.lineEnd)));
    const href = cited?.url || githubFileUrl(reference, repo, sha);
    if (!href) return reference.text;
    return <a className="file-chip" href={href} target="_blank" rel="noopener noreferrer" title={`Open ${reference.text}`} aria-label={`Open ${reference.text} on GitHub`}><span aria-hidden="true">▤</span>{reference.text}</a>;
  }

  function renderReferences(text) {
    const references = parseFileReferences(text);
    if (!references.length) return text;
    const output = [];
    let offset = 0;
    for (const [index, reference] of references.entries()) {
      output.push(text.slice(offset, reference.start), <span className="reference-inline" key={`${reference.start}-${index}`}>{fileChip(reference)}</span>);
      offset = reference.end;
    }
    output.push(text.slice(offset));
    return output;
  }

  function transformText(children) {
    return Children.map(children, child => {
      if (typeof child === 'string') return renderReferences(child);
      if (!isValidElement(child) || child.type === Link || child.type === InlineCode) return child;
      return cloneElement(child, undefined, transformText(child.props.children));
    });
  }

  function Link({ href, children, ...props }) {
    const safeHref = safeMarkdownUrl(href);
    if (!safeHref) return <span>{children}</span>;
    return <a href={safeHref} target="_blank" rel="noopener noreferrer" {...props}>{children}</a>;
  }

  function InlineCode({ className, children, ...props }) {
    const text = plainText(children).replace(/\n$/, '');
    const refs = parseFileReferences(text);
    if (!className && refs.length === 1 && refs[0].start === 0 && refs[0].end === text.length) return fileChip(refs[0]);
    return <code className={className} {...props}>{children}</code>;
  }

  function CodeBlock({ children }) {
    const code = plainText(children).replace(/\n$/, '');
    return <div className="answer-code"><CopyButton text={code} label="Copy code"/><pre>{children}</pre></div>;
  }

  const components = {
    p: ({ children, ...props }) => <p {...props}>{transformText(children)}</p>,
    li: ({ children, ...props }) => <li {...props}>{transformText(children)}</li>,
    h1: ({ children, ...props }) => <h1 {...props}>{transformText(children)}</h1>,
    h2: ({ children, ...props }) => <h2 {...props}>{transformText(children)}</h2>,
    h3: ({ children, ...props }) => <h3 {...props}>{transformText(children)}</h3>,
    h4: ({ children, ...props }) => <h4 {...props}>{transformText(children)}</h4>,
    blockquote: ({ children, ...props }) => <blockquote {...props}>{transformText(children)}</blockquote>,
    td: ({ children, ...props }) => <td {...props}>{transformText(children)}</td>,
    th: ({ children, ...props }) => <th {...props}>{transformText(children)}</th>,
    a: Link,
    code: InlineCode,
    pre: CodeBlock,
    table: ({ children, ...props }) => <div className="answer-table"><table {...props}>{children}</table></div>,
  };

  function setRating(rating) {
    setFeedback(rating);
    // TODO: connect to answer feedback storage when a feedback endpoint exists.
    window.dispatchEvent(new CustomEvent('gitlens:answer-feedback', { detail: { rating, sha } }));
  }

  return <article className="answer-content">
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={components} urlTransform={safeMarkdownUrl}>{content}</ReactMarkdown>
    {sources.length > 0 && <details className="answer-sources"><summary>Sources ({sources.length})</summary><ul>{sources.map(source => <li key={`${source.path}:${source.lineStart}-${source.lineEnd}`}>{fileChip({ path: source.path, lineStart: source.lineStart, lineEnd: source.lineEnd, text: `${source.path}${source.lineStart ? `:L${source.lineStart}${source.lineEnd && source.lineEnd !== source.lineStart ? `-L${source.lineEnd}` : ''}` : ''}` })}</li>)}</ul></details>}
    <div className="answer-actions"><CopyButton text={content} label="Copy answer"/><span className="feedback-label">Was this helpful?</span><button className={`feedback-button${feedback === 'up' ? ' selected' : ''}`} type="button" aria-label="Helpful answer" aria-pressed={feedback === 'up'} onClick={() => setRating('up')}>👍</button><button className={`feedback-button${feedback === 'down' ? ' selected' : ''}`} type="button" aria-label="Unhelpful answer" aria-pressed={feedback === 'down'} onClick={() => setRating('down')}>👎</button></div>
  </article>;
}
