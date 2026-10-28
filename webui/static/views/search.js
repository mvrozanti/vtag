import { getJSON, thumbUrl } from '../api.js';

const PAGE_SIZE = 120;
const PREFETCH_PX = 1200;
const DEBOUNCE_MS = 180;
let timer = null;
let observer = null;
let current = null;

function el(tag, opts = {}, ...children) {
  const e = document.createElement(tag);
  if (opts.cls) e.className = opts.cls;
  if (opts.text != null) e.textContent = String(opts.text);
  if (opts.attrs) for (const [k, v] of Object.entries(opts.attrs)) e.setAttribute(k, v);
  if (opts.id) e.id = opts.id;
  if (opts.on) for (const [k, v] of Object.entries(opts.on)) e.addEventListener(k, v);
  for (const c of children) {
    if (c == null || c === false) continue;
    if (typeof c === 'string') e.appendChild(document.createTextNode(c));
    else e.appendChild(c);
  }
  return e;
}

const VALID_TYPES = ['', 'meme', 'photo', 'screenshot_text', 'screenshot_app', 'graph_chart', 'diagram', 'art', 'other'];
const MEDIA_KINDS = ['', 'image', 'video'];

function cardEl(item, ctx) {
  const card = el('div', { cls: item.fuzzy ? 'vt-card is-fuzzy' : 'vt-card', attrs: { 'data-sha': item.sha, tabindex: '0', role: 'button', 'aria-label': item.basename } });
  const thumb = el('div', { cls: 'vt-thumb is-loading' });
  if (item.sha) {
    const url = thumbUrl(item.sha);
    const probe = new Image();
    probe.onload = () => {
      thumb.style.backgroundImage = `url("${url}")`;
      thumb.classList.remove('is-loading');
    };
    probe.onerror = () => {
      thumb.classList.remove('is-loading');
      thumb.classList.add('is-missing');
      thumb.textContent = (item.format || 'no preview').toLowerCase();
    };
    probe.src = url;
  }
  if (item.content_type && item.content_type !== 'other') {
    thumb.appendChild(el('span', { cls: 'vt-badge', text: item.content_type.replace('_', ' ') }));
  }
  if (item.media === 'video') {
    thumb.appendChild(el('span', { cls: 'vt-badge is-video', text: '▶ video' }));
  }
  if (item.fuzzy) {
    thumb.appendChild(el('span', { cls: 'vt-badge is-fuzzy', text: '~ name', attrs: { title: 'fuzzy filename match' } }));
  }
  card.appendChild(thumb);
  const meta = el('div', { cls: 'vt-meta' });
  meta.appendChild(el('div', { cls: 'vt-name', text: item.basename, attrs: { title: item.path } }));
  const tags = item.tags_top && item.tags_top.length
    ? item.tags_top.slice(0, 5).join(' · ')
    : (item.tagged === false ? 'untagged' : 'no tags');
  meta.appendChild(el('div', { cls: 'vt-tags', text: tags }));
  card.appendChild(meta);
  card.addEventListener('click', () => ctx.openPanel(item.sha));
  card.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); ctx.openPanel(item.sha); }
  });
  return card;
}

async function fetchPage(state) {
  const params = new URLSearchParams(state.params);
  params.set('offset', String(state.loaded));
  params.set('limit', String(PAGE_SIZE));
  const r = await getJSON('/api/search?' + params.toString());
  return r.body || {};
}

function appendPage(state, items) {
  for (const item of items) state.grid.appendChild(cardEl(item, state.ctx));
  state.loaded += items.length;
  if (!items.length) state.total = state.loaded;
}

function nearBottom(sentinel) {
  return sentinel.getBoundingClientRect().top < window.innerHeight + PREFETCH_PX;
}

async function loadMore(sentinel) {
  const state = current;
  if (!state || state.loading || state.loaded >= state.total) return;
  state.loading = true;
  const body = await fetchPage(state);
  if (state !== current) return;
  appendPage(state, body.items || []);
  state.loading = false;
  if (nearBottom(sentinel)) loadMore(sentinel);
}

async function runSearch(grid, status, sentinel, query, contentType, media, ctx) {
  status.textContent = 'searching…';
  const params = new URLSearchParams();
  if (query) params.set('q', query);
  if (contentType) params.set('type', contentType);
  if (media) params.set('media', media);
  const state = { grid, ctx, params, loaded: 0, total: 0, loading: true };
  current = state;
  const body = await fetchPage(state);
  if (state !== current) return;
  grid.innerHTML = '';
  window.scrollTo(0, 0);
  if (!body.cache_available) {
    current = null;
    status.textContent = 'no index';
    grid.appendChild(el('div', { cls: 'mv-empty', text: 'no index yet · build one from the run tab' }));
    return;
  }
  const total = body.total || 0;
  const fuzzy = body.fuzzy || 0;
  status.textContent = `${total.toLocaleString()} match${total === 1 ? '' : 'es'}` + (fuzzy ? ` · ${fuzzy.toLocaleString()} fuzzy` : '');
  state.total = total;
  appendPage(state, body.items || []);
  state.loading = false;
  if (total === 0) {
    grid.appendChild(el('div', { cls: 'mv-empty', text: 'no matches' }));
    return;
  }
  if (nearBottom(sentinel)) loadMore(sentinel);
}

function chipRow(label, values, initial, onPick) {
  let value = initial;
  const chips = values.map((v) => {
    const c = el('button', { cls: 'mv-chip', text: v ? v.replace('_', ' ') : 'all', attrs: { type: 'button', 'aria-pressed': String(v === value) } });
    c.addEventListener('click', () => {
      value = v;
      chips.forEach((x, i) => x.setAttribute('aria-pressed', String(values[i] === value)));
      onPick(value);
    });
    return c;
  });
  return { chips, get: () => value };
}

export async function render(container, { ctx }) {
  container.innerHTML = '';

  const initial = sessionStorage.getItem('vtag.search.q') || '';
  sessionStorage.removeItem('vtag.search.q');
  const fromTag = sessionStorage.getItem('vtag.search.from') === 'tag';
  sessionStorage.removeItem('vtag.search.from');

  const toolbar = el('div', { cls: 'vt-toolbar' });
  const input = el('input', { cls: 'mv-search', attrs: {
    type: 'search', id: 'search-q', value: initial, enterkeyhint: 'search', autocomplete: 'off', autocapitalize: 'off', spellcheck: 'false',
    placeholder: 'tags, labels, filenames…', 'aria-label': 'search',
    title: 'fuzzy filenames work (pepefrog); combine with AND, OR, NOT and parentheses',
  } });
  toolbar.appendChild(input);
  container.appendChild(toolbar);

  const filters = el('div', { cls: 'vt-filters' });
  const types = chipRow('content type', VALID_TYPES, '', () => query());
  let mediaValue = '';
  const mediaChips = MEDIA_KINDS.filter(Boolean).map((kind) => {
    const c = el('button', { cls: 'mv-chip', text: kind, attrs: { type: 'button', 'aria-pressed': 'false' } });
    c.addEventListener('click', () => {
      mediaValue = mediaValue === kind ? '' : kind;
      mediaChips.forEach(x => x.setAttribute('aria-pressed', String(x.textContent === mediaValue)));
      query();
    });
    return c;
  });
  const row = el('div', { cls: 'mv-chips', attrs: { role: 'group', 'aria-label': 'filters' } });
  mediaChips.forEach(c => row.appendChild(c));
  row.appendChild(el('span', { cls: 'vt-chip-sep', attrs: { 'aria-hidden': 'true' } }));
  types.chips.forEach(c => row.appendChild(c));
  filters.appendChild(row);
  container.appendChild(filters);

  const statusRow = el('div', { cls: 'vt-status' });
  const status = el('span', { cls: 'mv-label', id: 'search-status', text: 'loading…' });
  statusRow.appendChild(status);
  container.appendChild(statusRow);

  const grid = el('div', { cls: 'vt-grid' });
  container.appendChild(grid);
  const sentinel = el('div', { cls: 'vt-sentinel' });
  container.appendChild(sentinel);
  observer = new IntersectionObserver((entries) => {
    if (entries.some(e => e.isIntersecting)) loadMore(sentinel);
  }, { rootMargin: `0px 0px ${PREFETCH_PX}px 0px` });
  observer.observe(sentinel);

  const query = () => runSearch(grid, status, sentinel, input.value.trim(), types.get(), mediaValue, ctx);
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(query, DEBOUNCE_MS);
  }
  input.addEventListener('input', schedule);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') input.blur(); });
  await query();

  if (!fromTag && matchMedia('(pointer: fine)').matches) {
    input.focus();
    input.select();
  }
}

export function teardown() {
  clearTimeout(timer);
  timer = null;
  if (observer) observer.disconnect();
  observer = null;
  current = null;
}
