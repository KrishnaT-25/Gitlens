import { build } from 'esbuild';
import { readFile, mkdir, rm, writeFile, copyFile } from 'node:fs/promises';
import { resolve } from 'node:path';

const root = resolve(import.meta.dirname, '..');
const dist = resolve(root, 'dist');
const envText = await readFile(resolve(root, '.env'), 'utf8').catch(() => '');
const localApi = envText.split(/\r?\n/).find(x => x.startsWith('API_BASE_URL='))?.slice(13).trim();
const api = (process.env.API_BASE_URL || localApi || 'http://127.0.0.1:8001').replace(/\/$/, '');
const origin = new URL(api).origin;
// Public key fixes the unpacked extension ID across machines; it is not a credential.
const extensionKey = 'MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAops+XDRyqS7K/dXeHa1S7H5t7bTr7JFo43RjJ9CTk7q4hKhNrmEfIJXHkMCYeb9I88QYDhD0v8UkONaK5sRlfqVbotkiF+EGJfOov0vX4pBM6QLk8CJMnVRjHBoNsL+xosQQIuNAzE2CAhJil5c2fnFZz5fupd6nVL3Ux8xyYXc9JJHlErQb0352ncRESDoin3SplGpAPKlrORD0/KhZTYHS8jfpgMmbSfaqznfXKP4yl8pL7ELzuxq5T7c2Y2BU87mr2SXjxbIDB+NJjWNocj/lnOcbhLolFNT5pPm1kTAFEeZXEj7qCWwC2kvJlWuw/p0dbAdI7KenUcrLb6EEDQIDAQAB';
await rm(dist, { recursive: true, force: true });
await mkdir(dist, { recursive: true });
for (const [entry, outfile] of [['content.jsx', 'content.js'], ['background.js', 'background.js'], ['auth.jsx', 'auth.js']]) {
  await build({ entryPoints: [resolve(root, 'src', entry)], outfile: resolve(dist, outfile), bundle: true, format: 'iife', platform: 'browser', target: 'chrome120', jsx: 'automatic', define: { __API_BASE_URL__: JSON.stringify(api) }, minify: true });
}
const manifest = {
  manifest_version: 3,
  name: 'GitLens',
  version: '0.1.0',
  key: extensionKey,
  description: 'Repository-aware summaries and answers on public GitHub repositories.',
  permissions: ['storage', 'identity'],
  host_permissions: [`${origin}/*`],
  content_scripts: [{ matches: ['https://github.com/*'], js: ['content.js'], run_at: 'document_idle' }],
  background: { service_worker: 'background.js' },
  action: { default_title: 'GitLens' },
};
await writeFile(resolve(dist, 'manifest.json'), JSON.stringify(manifest, null, 2));
await copyFile(resolve(root, 'auth.html'), resolve(dist, 'auth.html'));
console.log(`Built GitLens for ${api}`);
