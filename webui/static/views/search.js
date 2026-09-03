import { getJSON, postJSON } from '../api.js';

const RESULT_LIMIT = 300;
const DEBOUNCE_MS = 180;
let timer = null;

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
  const card = el('div', { cls: 'card', attrs: { 'data-sha': item.sha, tabindex: '0' } });
  const thumb = el('div', { cls: 'thumb loading' });
  if (item.sha) {
    const url = `/api/thumb?sha=${encodeURIComponent(item.sha)}`;
    thumb.style.backgroundImage = `url("${url}")`;
    const probe = new Image();
    probe.onload = () => thumb.classList.remove('loading');
    probe.onerror = () => {
      thumb.classList.remove('loading');
      thumb.classList.add('placeholder');
      thumb.style.backgroundImage = '';
      thumb.textContent = (item.format || 'no thumb').toLowerCase();
    };
    probe.src = url;
  }
  if (item.content_type && item.content_type !== 'other') {
    thumb.appendChild(el('span', { cls: 'badge', text: item.content_type.replace('_', ' ') }));
  }
  if (item.media === 'video') {
    thumb.appendChild(el('span', { cls: 'badge video', text: 'video' }));
  }
  card.appendChild(thumb);
  const meta = el('div', { cls: 'meta' });
  meta.appendChild(el('div', { cls: 'name', text: item.basename, attrs: { title: item.path } }));
  const tagsEl = el('div', { cls: 'tags' });
  if (item.tags_top && item.tags_top.length) {
    tagsEl.textContent = item.tags_top.slice(0, 5).join(' · ');
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

async function runSearch(grid, status, query, contentType, media, ctx) {
  status.textContent = 'searching…';
  const params = new URLSearchParams();
  if (query) params.set('q', query);
  if (contentType) params.set('type', contentType);
  if (media) params.set('media', media);
  params.set('limit', String(RESULT_LIMIT));
  const r = await getJSON('/api/search?' + params.toString());
  const body = r.body || {};
  grid.innerHTML = '';
  if (!body.cache_available) {
    status.textContent = '0 match';
    const e = el('div', { cls: 'empty' });
    e.appendChild(el('h2', { text: 'no cache' }));
    e.appendChild(el('p', { text: 'No index yet — hit reindex in the toolbar above.' }));
    grid.appendChild(e);
    return;
  }
  const items = body.items || [];
  const total = body.total || 0;
  status.textContent = `${total.toLocaleString()} match${total === 1 ? '' : 'es'}`;
  for (const item of items) grid.appendChild(cardEl(item, ctx));
  if (total > items.length) {
    const more = el('div', { cls: 'empty' });
    more.appendChild(el('p', { text: `showing ${items.length} of ${total.toLocaleString()} — narrow the query to see the rest.` }));
    grid.appendChild(more);
  } else if (total === 0) {
    const none = el('div', { cls: 'empty' });
    none.appendChild(el('p', { text: 'no matches' }));
    grid.appendChild(none);
  }
}

export async function render(container, { ctx }) {
  container.innerHTML = '';

  const toolbar = el('div', { cls: 'view-toolbar' });
  const initial = sessionStorage.getItem('vtag.search.q') || '';
  sessionStorage.removeItem('vtag.search.q');
  const fromTag = sessionStorage.getItem('vtag.search.from') === 'tag';
  sessionStorage.removeItem('vtag.search.from');

  const input = el('input', { attrs: { type: 'search', placeholder: 'tag, label, filename — or: pepe AND (smug OR angry) NOT politics', id: 'search-q', value: initial } });
  input.style.minWidth = '20rem';
  toolbar.appendChild(input);

  toolbar.appendChild(el('span', { cls: 'label', text: 'type' }));
  const select = el('select');
  VALID_TYPES.forEach(t => select.appendChild(el('option', { attrs: { value: t }, text: t || 'all' })));
  toolbar.appendChild(select);

  toolbar.appendChild(el('span', { cls: 'label', text: 'media' }));
  const mediaSelect = el('select');
  MEDIA_KINDS.forEach(k => mediaSelect.appendChild(el('option', { attrs: { value: k }, text: k || 'all' })));
  toolbar.appendChild(mediaSelect);

  const reindexBtn = el('button', { text: 'reindex' });
  reindexBtn.addEventListener('click', async () => {
    const r = await postJSON('/api/reindex');
    if (r.status === 202) ctx.toast('reindex started');
    else if (r.status === 409) ctx.toast('reindex already running');
    else ctx.toast(r.body?.error || 'reindex failed');
  });
  toolbar.appendChild(reindexBtn);

  toolbar.appendChild(el('span', { cls: 'grow' }));
  const status = el('span', { cls: 'label', id: 'search-status', text: 'loading…' });
  toolbar.appendChild(status);
  container.appendChild(toolbar);

  const grid = el('div', { cls: 'card-grid' });
  container.appendChild(grid);

  const query = () => runSearch(grid, status, input.value.trim(), select.value, mediaSelect.value, ctx);
  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(query, DEBOUNCE_MS);
  }
  input.addEventListener('input', schedule);
  select.addEventListener('change', query);
  mediaSelect.addEventListener('change', query);
  await query();

  if (fromTag && !document.activeElement?.tagName?.match(/INPUT|SELECT/)) {
    // tag click came from panel: leave focus where it was so user keeps reading
  } else {
    input.focus();
    input.select();
  }
}

export function teardown() { clearTimeout(timer); timer = null; }
