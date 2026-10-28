import { getJSON } from './api.js';
import * as runner from './views/runner.js';
import * as search from './views/search.js';
import { setupPanel } from './panel.js';

const ROUTES = {
  '#/search': search,
  '#/runner': runner,
};
const DEFAULT_ROUTE = '#/search';

const viewEl = document.getElementById('view');
const navEl = document.getElementById('nav');
const runPill = document.getElementById('run-pill');
const cachePill = document.getElementById('cache-pill');

let currentView = null;
let pendingAbort = null;
let cacheMeta = { db_exists: false, count: 0, last_mtime: 0, reindex: { running: false } };

function toast(msg, ms = 2200) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, ms);
}

function updateNav(hash) {
  navEl.querySelectorAll('a[data-route]').forEach(a => {
    if (a.dataset.route === hash) a.setAttribute('aria-current', 'page');
    else a.removeAttribute('aria-current');
  });
}

async function teardownView() {
  if (currentView && typeof currentView.teardown === 'function') {
    try { await currentView.teardown(); } catch (e) { console.error('teardown failed', e); }
  }
  currentView = null;
}

async function route() {
  const hash = ROUTES[location.hash] ? location.hash : (location.hash ? DEFAULT_ROUTE : DEFAULT_ROUTE);
  if (!location.hash) {
    location.hash = DEFAULT_ROUTE;
    return;
  }
  updateNav(hash);
  await teardownView();
  viewEl.innerHTML = '<div class="mv-empty">loading…</div>';
  const mod = ROUTES[hash];
  try {
    await mod.render(viewEl, { ctx, toast });
  } catch (err) {
    console.error(`view ${hash} render failed`, err);
    viewEl.innerHTML = '';
    const e = document.createElement('div');
    e.className = 'mv-empty';
    e.textContent = `view failed to render: ${String(err && err.message || err)}`;
    viewEl.appendChild(e);
  }
  currentView = mod;
}

const ctx = {
  go(hash) {
    if (location.hash === hash) route();
    else location.hash = hash;
  },
  openPanel(sha) { window.dispatchEvent(new CustomEvent('vtag:open-panel', { detail: { sha } })); },
  filterByTag(tag) {
    sessionStorage.setItem('vtag.search.q', tag);
    sessionStorage.setItem('vtag.search.from', 'tag');
    this.go('#/search');
  },
  getCacheMeta() { return cacheMeta; },
  toast,
};

async function refreshHeader() {
  const [status, meta] = await Promise.all([
    getJSON('/api/status').catch(() => ({ body: null })),
    getJSON('/api/cache_meta').catch(() => ({ body: null })),
  ]);
  const dot = runPill.querySelector('.mv-dot');
  const label = runPill.querySelector('.vt-run__label');
  if (status.body) {
    const running = !!status.body.running;
    dot.className = 'mv-dot ' + (running ? 'is-ok' : 'is-unknown');
    label.textContent = running ? 'running' : 'idle';
  } else {
    dot.className = 'mv-dot is-down';
    label.textContent = 'offline';
  }
  if (meta.body) {
    cacheMeta = meta.body;
    const reindexing = !!(meta.body.reindex && meta.body.reindex.running);
    const count = meta.body.count || 0;
    cachePill.textContent = reindexing ? 'reindexing…' : (meta.body.db_exists && count ? `${count.toLocaleString()} indexed` : 'no index');
  }
}

setupPanel({ ctx });
window.addEventListener('hashchange', route);
document.addEventListener('keydown', (e) => {
  if (e.key === '/' && !['INPUT', 'TEXTAREA'].includes(document.activeElement?.tagName)) {
    e.preventDefault(); ctx.go('#/search');
    setTimeout(() => document.getElementById('search-q')?.focus(), 50);
  }
});

route();
refreshHeader();
setInterval(refreshHeader, 5000);
