import { getJSON, postJSON } from '../api.js';

const PAGE = 60;
const VALID_TYPES = ['', 'meme', 'photo', 'screenshot_text', 'screenshot_app', 'graph_chart', 'diagram', 'art', 'other'];

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

function cardEl(item, ctx) {
  const card = el('div', { cls: 'card', attrs: { 'data-sha': item.sha, tabindex: '0' } });
  const thumb = el('div', { cls: 'thumb loading' });
  if (item.sha) {
    thumb.dataset.thumbUrl = `/api/thumb?sha=${encodeURIComponent(item.sha)}`;
    thumb.dataset.thumbFallback = (item.format || 'no thumb').toLowerCase();
    state.thumbObserver?.observe(thumb);
  } else {
    thumb.classList.remove('loading');
    thumb.classList.add('placeholder');
    thumb.textContent = 'no sha';
  }
  if (item.content_type && item.content_type !== 'other') {
    thumb.appendChild(el('span', { cls: 'badge', text: item.content_type.replace('_', ' ') }));
  } else if (item.content_type === 'other') {
    thumb.appendChild(el('span', { cls: 'badge other', text: 'other' }));
  }
  card.appendChild(thumb);

  const meta = el('div', { cls: 'meta' });
  meta.appendChild(el('div', { cls: 'name', text: item.basename, attrs: { title: item.path } }));
  const tagsEl = el('div', { cls: 'tags' });
  if (item.tags_top && item.tags_top.length) {
    tagsEl.appendChild(document.createTextNode(item.tags_top.slice(0, 5).join(' · ')));
    if (item.tags_count > 5) {
      tagsEl.appendChild(el('span', { cls: 'more', text: ` +${item.tags_count - 5}` }));
    }
  } else {
    tagsEl.textContent = '(no tags)';
  }
  meta.appendChild(tagsEl);
  card.appendChild(meta);

  card.addEventListener('click', () => ctx.openPanel(item.sha));
  card.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); ctx.openPanel(item.sha); }
  });
  return card;
}

function emptyState({ container, ctx, missing }) {
  const e = el('div', { cls: 'empty' });
  e.appendChild(el('h2', { text: missing ? 'no cache yet' : 'cache is empty' }));
  e.appendChild(el('p', {
    text: missing
      ? 'The vfind index has not been built. Click reindex to scan tagged images.'
      : 'No tagged images in the cache yet — run vtag tag, then reindex.',
  }));
  const btn = el('button', { cls: 'primary', text: 'reindex now' });
  btn.addEventListener('click', async () => {
    btn.disabled = true; btn.textContent = 'starting…';
    const r = await postJSON('/api/reindex');
    if (r.status === 202 || r.status === 409) {
      ctx.toast('reindex running');
      btn.textContent = 'reindex running';
    } else {
      ctx.toast(r.body?.error || 'reindex failed');
      btn.disabled = false; btn.textContent = 'reindex now';
    }
  });
  e.appendChild(btn);
  container.appendChild(e);
}

async function loadPage(offset, contentType) {
  const params = new URLSearchParams({ limit: PAGE, offset });
  if (contentType) params.set('content_type', contentType);
  const r = await getJSON(`/api/recent?${params.toString()}`);
  return r.body || { cache_available: false, items: [] };
}

export async function render(container, { ctx }) {
  container.innerHTML = '';
  state = { offset: 0, contentType: '', loading: false, done: false, grid: null, observer: null, ctx };

  const toolbar = el('div', { cls: 'view-toolbar' });
  toolbar.appendChild(el('span', { cls: 'label', text: 'filter' }));
  const select = el('select');
  VALID_TYPES.forEach(t => {
    const opt = el('option', { attrs: { value: t }, text: t || 'all types' });
    select.appendChild(opt);
  });
  select.addEventListener('change', () => {
    state.contentType = select.value;
    state.offset = 0; state.done = false;
    state.grid.innerHTML = '';
    pump();
  });
  toolbar.appendChild(select);
  toolbar.appendChild(el('span', { cls: 'grow' }));
  const refreshBtn = el('button', { text: 'refresh' });
  refreshBtn.addEventListener('click', () => {
    state.offset = 0; state.done = false;
    state.grid.innerHTML = '';
    pump();
  });
  toolbar.appendChild(refreshBtn);
  const reindexBtn = el('button', { cls: 'primary', text: 'reindex' });
  reindexBtn.addEventListener('click', async () => {
    const r = await postJSON('/api/reindex');
    if (r.status === 202) ctx.toast('reindex started');
    else if (r.status === 409) ctx.toast('reindex already running');
    else ctx.toast(r.body?.error || 'reindex failed');
  });
  toolbar.appendChild(reindexBtn);
  container.appendChild(toolbar);

  const grid = el('div', { cls: 'card-grid' });
  state.grid = grid;
  container.appendChild(grid);

  const sentinel = el('div', { cls: 'sentinel' });
  container.appendChild(sentinel);

  state.observer = new IntersectionObserver(entries => {
    for (const ent of entries) {
      if (ent.isIntersecting) pump();
    }
  }, { rootMargin: '600px 0px' });
  state.observer.observe(sentinel);

  state.thumbObserver = new IntersectionObserver(entries => {
    for (const ent of entries) {
      if (!ent.isIntersecting) continue;
      const t = ent.target;
      state.thumbObserver.unobserve(t);
      const url = t.dataset.thumbUrl;
      if (!url) continue;
      const probe = new Image();
      probe.onload = () => {
        t.style.backgroundImage = `url("${url}")`;
        t.classList.remove('loading');
      };
      probe.onerror = () => {
        t.classList.remove('loading');
        t.classList.add('placeholder');
        t.textContent = t.dataset.thumbFallback || 'no thumb';
      };
      probe.src = url;
    }
  }, { rootMargin: '300px 0px' });

  async function pump() {
    if (state.loading || state.done) return;
    state.loading = true;
    const page = await loadPage(state.offset, state.contentType);
    state.loading = false;
    if (!page.cache_available) {
      if (state.offset === 0) {
        toolbar.remove();
        grid.remove();
        sentinel.remove();
        emptyState({ container, ctx, missing: !(page.meta && page.meta.db_exists) });
      }
      state.done = true;
      return;
    }
    if (state.offset === 0 && (!page.items || !page.items.length)) {
      emptyState({ container, ctx, missing: !(page.meta && page.meta.db_exists) });
      state.done = true;
      return;
    }
    for (const item of page.items) {
      grid.appendChild(cardEl(item, ctx));
    }
    if (!page.items || page.items.length < PAGE) state.done = true;
    state.offset += (page.items?.length || 0);
  }

  await pump();
}

export function teardown() {
  if (state && state.observer) state.observer.disconnect();
  if (state && state.thumbObserver) state.thumbObserver.disconnect();
  state = null;
}
