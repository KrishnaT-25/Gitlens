const reserved = new Set(['about','account','apps','codespaces','collections','contact','dashboard','enterprise','events','explore','features','issues','login','marketplace','new','notifications','orgs','pricing','pulls','search','settings','signup','site','sponsors','topics','trending','users']);
const routes = new Set(['tree','blob','issues','pull','pulls','commit','commits']);

export function parseRepositoryUrl(value) {
  let url;
  try { url = new URL(value); } catch { return null; }
  if (url.protocol !== 'https:' || url.hostname !== 'github.com' || url.port || url.username || url.password) return null;
  const parts = url.pathname.split('/').filter(Boolean);
  if (parts.length < 2 || reserved.has(parts[0].toLowerCase())) return null;
  if (!/^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$/.test(parts[0])) return null;
  if (!/^[A-Za-z0-9_.-]{1,100}$/.test(parts[1]) || parts[1] === '.' || parts[1] === '..') return null;
  if (parts.length > 2 && !routes.has(parts[2])) return null;
  return { owner: parts[0], name: parts[1], url: url.href };
}
