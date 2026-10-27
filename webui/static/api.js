const COMMON = { credentials: 'same-origin', cache: 'no-store' };
const THUMB_VERSION = 2;

export function thumbUrl(sha) {
  return `/api/thumb?sha=${encodeURIComponent(sha)}&v=${THUMB_VERSION}`;
}

export async function getJSON(path) {
  const r = await fetch(path, COMMON);
  let body = null;
  try { body = await r.json(); } catch (e) { body = null; }
  return { status: r.status, ok: r.ok, body };
}

export async function postJSON(path, payload) {
  const opts = { ...COMMON, method: 'POST' };
  if (payload !== undefined) {
    opts.headers = { 'Content-Type': 'application/json' };
    opts.body = JSON.stringify(payload);
  }
  const r = await fetch(path, opts);
  let body = null;
  try { body = await r.json(); } catch (e) { body = null; }
  return { status: r.status, ok: r.ok, body };
}

export async function getText(path) {
  const r = await fetch(path, COMMON);
  if (!r.ok) return '';
  return await r.text();
}
