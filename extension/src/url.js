const reserved = new Set(['about','account','apps','codespaces','collections','contact','dashboard','enterprise','events','explore','features','issues','login','marketplace','new','notifications','orgs','pricing','pulls','search','settings','signup','site','sponsors','topics','trending','users']);
const routes = new Set(['tree','blob','issues','pull','pulls','commit','commits']);

export function parseRepositoryUrl(value) {
  let url, parts;
  try {
    if (typeof value !== 'string' || value.length > 2048 || /[\x00-\x20\x7f\\]/.test(value) || !/^https:\/\/github\.com(?:\/|[?#]|$)/i.test(value)) return null;
    // Check the raw path before URL normalizes dot segments away.
    const rawPath = value.replace(/^https:\/\/github\.com/i, '').split(/[?#]/)[0];
    parts = decodeURIComponent(rawPath).replace(/^\//, '').replace(/\/$/, '').split('/');
    if (parts.some(part => !part || part === '.' || part === '..' || /[\x00-\x1f\x7f\\]/.test(part))) return null;
    url = new URL(value);
  } catch { return null; }
  if (url.protocol !== 'https:' || url.hostname !== 'github.com' || url.port || url.username || url.password) return null;
  if (parts.length < 2 || reserved.has(parts[0].toLowerCase())) return null;
  if (!/^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$/.test(parts[0])) return null;
  if (!/^[A-Za-z0-9_.-]{1,100}$/.test(parts[1]) || parts[1] === '.' || parts[1] === '..') return null;
  if (parts.length > 2 && !routes.has(parts[2])) return null;
  const route = parts[2], tail = parts.slice(3);
  if ((['tree', 'blob', 'commit'].includes(route) && !tail.length) ||
      (route === 'blob' && tail.length < 2) ||
      (route === 'commit' && (tail.length !== 1 || !/^[a-f0-9]{7,40}$/i.test(tail[0]))) ||
      (route === 'issues' && tail.length && (tail.length !== 1 || !/^\d+$/.test(tail[0])))) return null;
  if (route === 'pull' || route === 'pulls') return { owner: parts[0], name: parts[1], url: url.href,
    unsupportedReason: 'Pull request pages are not supported. Open a branch, tag, or commit instead.' };
  return { owner: parts[0], name: parts[1], url: url.href };
}
