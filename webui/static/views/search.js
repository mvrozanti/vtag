import { getJSON } from '../api.js';

const POOL_LIMIT = 800;
let state = null;

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

function matches(item, query, contentType) {
  if (contentType && item.content_type !== contentType) return false;
  if (!query) return true;
  const needle = query.toLowerCase();
  if (item.basename && item.basename.toLowerCase().includes(needle)) return true;
  if (item.tags_top) {
    for (const t of item.tags_top) if (String(t).toLowerCase().includes(needle)) return true;
  }
  return false;
}

function renderResults(grid, status, items, query, contentType, ctx) {
  grid.innerHTML = '';
  const filtered = items.filter(it => matches(it, query, contentType));
  status.textContent = `${filtered.length.toLocaleString()} / ${items.length.toLocaleString()} match`;
  for (const item of filtered.slice(0, 300)) {
    grid.appendChild(cardEl(item, ctx));
  }
  if (filtered.length > 300) {
    const more = el('div', { cls: 'empty' });
    more.appendChild(el('p', { text: `+${filtered.length - 300} more — narrow the query to see them.` }));
    grid.appendChild(more);
  }
}

export async function render(container, { ctx }) {
  container.innerHTML = '';
  state = { items: [] };

  const toolbar = el('div', { cls: 'view-toolbar' });
  const initial = sessionStorage.getItem('vtag.search.q') || '';
  sessionStorage.removeItem('vtag.search.q');
  const fromTag = sessionStorage.getItem('vtag.search.from') === 'tag';
  sessionStorage.removeItem('vtag.search.from');

  const input = el('input', { attrs: { type: 'search', placeholder: 'tag, filename, content_type substring', id: 'search-q', value: initial } });
  input.style.minWidth = '20rem';
  toolbar.appendChild(input);

  toolbar.appendChild(el('span', { cls: 'label', text: 'type' }));
  const select = el('select');
  VALID_TYPES.forEach(t => select.appendChild(el('option', { attrs: { value: t }, text: t || 'all' })));
  toolbar.appendChild(select);

  toolbar.appendChild(el('span', { cls: 'grow' }));
  const status = el('span', { cls: 'label', id: 'search-status', text: 'loading…' });
  toolbar.appendChild(status);
  container.appendChild(toolbar);

  const grid = el('div', { cls: 'card-grid' });
  container.appendChild(grid);

  const r = await getJSON(`/api/recent?limit=${POOL_LIMIT}`);
  state.items = (r.body && r.body.items) || [];
  if (!r.body || !r.body.cache_available) {
    const e = el('div', { cls: 'empty' });
    e.appendChild(el('h2', { text: 'no cache' }));
    e.appendChild(el('p', { text: 'Reindex first — feed view has the button.' }));
    container.appendChild(e);
    status.textContent = '0 / 0';
    return;
  }

  function refresh() {
    renderResults(grid, status, state.items, input.value.trim(), select.value, ctx);
  }
  input.addEventListener('input', refresh);
  select.addEventListener('change', refresh);
  refresh();

  if (fromTag && !document.activeElement?.tagName?.match(/INPUT|SELECT/)) {
    // tag click came from panel: leave focus where it was so user keeps reading
  } else {
    input.focus();
    input.select();
  }
}

export function teardown() { state = null; }
