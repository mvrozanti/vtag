import { getJSON } from '../api.js';

let currentKind = 'fail';
let state = null;

function el(tag, opts = {}, ...children) {
  const e = document.createElement(tag);
  if (opts.cls) e.className = opts.cls;
  if (opts.text != null) e.textContent = String(opts.text);
  if (opts.attrs) for (const [k, v] of Object.entries(opts.attrs)) e.setAttribute(k, v);
  if (opts.on) for (const [k, v] of Object.entries(opts.on)) e.addEventListener(k, v);
  for (const c of children) {
    if (c == null || c === false) continue;
    if (typeof c === 'string') e.appendChild(document.createTextNode(c));
    else e.appendChild(c);
  }
  return e;
}

async function fetchEvents(kind) {
  const r = await getJSON(`/api/events?kind=${kind}&limit=200`);
  return r.body || { items: [], log: null };
}

async function shaForPath(path) {
  // Best-effort sha lookup by scanning a recent slice of the cache.
  // Avoids a server round-trip per row.
  if (!state || !state.cachePathToSha) return null;
  return state.cachePathToSha.get(path) || null;
}

async function loadCacheIndex() {
  // Pull up to ~500 latest cache entries so events can map path → sha for click-through.
  const r = await getJSON('/api/recent?limit=500');
  const map = new Map();
  if (r.body && r.body.items) {
    for (const item of r.body.items) {
      if (item.path && item.sha) map.set(item.path, item.sha);
    }
  }
  return map;
}

function rowEl(ev, ctx) {
  const row = el('div', { cls: 'event-row ' + ev.kind.toLowerCase() });
  row.appendChild(el('span', { cls: 'count', text: `${ev.n}/${ev.total}` }));
  row.appendChild(el('span', { cls: 'path', text: ev.path, attrs: { title: ev.path } }));
  row.appendChild(el('span', { cls: 'reason', text: ev.reason || '', attrs: { title: ev.reason || '' } }));
  row.addEventListener('click', async () => {
    const sha = await shaForPath(ev.path);
    if (sha) ctx.openPanel(sha);
    else {
      try {
        await navigator.clipboard.writeText(ev.path);
        ctx.toast('path copied');
      } catch (e) {
        ctx.toast('copy failed');
      }
    }
  });
  return row;
}

async function renderList(container, ctx) {
  const listWrap = container.querySelector('#event-body');
  listWrap.innerHTML = '<div class="loading">loading…</div>';
  const data = await fetchEvents(currentKind);
  listWrap.innerHTML = '';

  const meta = container.querySelector('#event-meta');
  meta.textContent = data.log ? data.log.split('/').pop() + ` · ${data.items.length} ${currentKind.toLowerCase()}s` : `no log · 0 ${currentKind.toLowerCase()}s`;

  if (!data.items || !data.items.length) {
    const e = el('div', { cls: 'empty' });
    e.appendChild(el('h2', { text: 'no events' }));
    e.appendChild(el('p', { text: data.log ? `No ${currentKind.toLowerCase()} entries in the latest run log.` : 'No run log found yet — start a tagging run from the runner page.' }));
    listWrap.appendChild(e);
    return;
  }

  const list = el('div', { cls: 'event-list' });
  for (const ev of data.items) list.appendChild(rowEl(ev, ctx));
  listWrap.appendChild(list);
}

export async function render(container, { ctx }) {
  container.innerHTML = '';
  state = { cachePathToSha: new Map() };

  const toolbar = el('div', { cls: 'view-toolbar' });
  toolbar.appendChild(el('span', { cls: 'label', text: 'view' }));
  const tabs = el('div', { cls: 'event-tabs' });
  for (const k of ['fail', 'skip']) {
    const btn = el('button', { text: `${k}s`, attrs: { 'data-kind': k } });
    if (k === currentKind) btn.classList.add('active');
    btn.addEventListener('click', () => {
      currentKind = k;
      tabs.querySelectorAll('button').forEach(b => b.classList.toggle('active', b.dataset.kind === k));
      renderList(container, ctx);
    });
    tabs.appendChild(btn);
  }
  toolbar.appendChild(tabs);
  toolbar.appendChild(el('span', { cls: 'grow' }));
  toolbar.appendChild(el('span', { cls: 'label', id: 'event-meta', text: 'loading…' }));
  const refreshBtn = el('button', { text: 'refresh' });
  refreshBtn.addEventListener('click', () => renderList(container, ctx));
  toolbar.appendChild(refreshBtn);
  container.appendChild(toolbar);

  container.appendChild(el('div', { attrs: { id: 'event-body' } }));

  // fire-and-forget cache index load for path→sha click-through
  loadCacheIndex().then(map => { if (state) state.cachePathToSha = map; });

  await renderList(container, ctx);
}

export function teardown() { state = null; }
