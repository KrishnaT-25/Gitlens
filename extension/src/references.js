const REPOSITORY_REFERENCE = /(?<![\w@./:-])((?:[A-Za-z0-9_.-]+\/)+[A-Za-z0-9_.-]+\/?|[A-Za-z0-9_.-]+\.[A-Za-z0-9_-]+)(?::L?(\d+)(?:-L?(\d+))?)?(?![\w/.-])/g;

export function parseFileReferences(text) {
  const references = [];
  REPOSITORY_REFERENCE.lastIndex = 0;
  for (const match of text.matchAll(REPOSITORY_REFERENCE)) {
    const path = match[1];
    if (path.split('/').some(segment => segment === '.' || segment === '..')) continue;
    references.push({
      start: match.index,
      end: match.index + match[0].length,
      text: match[0],
      path,
      lineStart: match[2] ? Number(match[2]) : null,
      lineEnd: match[3] ? Number(match[3]) : match[2] ? Number(match[2]) : null,
    });
  }
  return references;
}

export function deduplicateSources(sources = []) {
  const unique = new Map();
  for (const source of sources) {
    if (!source || typeof source.path !== 'string') continue;
    const lineStart = Number.isInteger(source.lineStart) ? source.lineStart : null;
    const lineEnd = Number.isInteger(source.lineEnd) ? source.lineEnd : null;
    const key = `${source.path}:${lineStart ?? ''}-${lineEnd ?? ''}`;
    if (!unique.has(key)) unique.set(key, { ...source, lineStart, lineEnd });
  }
  return [...unique.values()].sort((a, b) => a.path.localeCompare(b.path) ||
    (a.lineStart ?? 0) - (b.lineStart ?? 0) || (a.lineEnd ?? 0) - (b.lineEnd ?? 0));
}

export function githubFileUrl(reference, repo, sha) {
  if (!repo?.owner || !repo?.name || !sha || !reference?.path) return '';
  const segments = reference.path.replace(/\\/g, '/').split('/').filter(Boolean);
  if (!segments.length || segments.some(part => part === '.' || part === '..')) return '';
  const kind = reference.path.endsWith('/') ? 'tree' : 'blob';
  const path = segments.map(encodeURIComponent).join('/');
  const range = reference.lineStart ? `#L${reference.lineStart}${reference.lineEnd && reference.lineEnd !== reference.lineStart ? `-L${reference.lineEnd}` : ''}` : '';
  return `https://github.com/${encodeURIComponent(repo.owner)}/${encodeURIComponent(repo.name)}/${kind}/${encodeURIComponent(sha)}/${path}${range}`;
}
