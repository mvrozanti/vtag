import { getJSON } from './api.js';

const ARRAY_SECTIONS = [
  ['characters', 'characters'],
  ['cultural_refs', 'cultural refs'],
  ['emotions', 'emotions'],
  ['actions', 'actions'],
  ['visual_elements', 'visual elements'],
  ['composition', 'composition'],
  ['style', 'style'],
  ['setting', 'setting'],
  ['colors', 'colors'],
];

function el(tag, opts = {}, ...children) {
  const e = document.createElement(tag);
  if (opts.cls) e.className = opts.cls;
  if (opts.text != null) e.textContent = String(opts.text);
  if (opts.attrs) for (const [k, v] of Object.entries(opts.attrs)) e.setAttribute(k, v);
  if (opts.on) for (const [k, v] of Object.entries(opts.on)) e.addEventListener(k, v);
  for (const c of children) {
    if (c == null || c === false) continue;
    if (Array.isArray(c)) c.forEach(x => x != null && e.appendChild(x));
    else if (typeof c === 'string') e.appendChild(document.createTextNode(c));
    else e.appendChild(c);
  }
  return e;
}

function section(title, ...children) {
  if (!children.length || children.every(c => c == null)) return null;
  const s = el('div', { cls: 'panel-section' });
  s.appendChild(el('h3', { text: title }));
  children.forEach(c => c && s.appendChild(c));
  return s;
}

function chips(items, { cls = 'chip', clickable = null } = {}) {
  if (!items || !items.length) return null;
  const wrap = el('div', { cls: 'chips' });
  items.forEach(item => {
    const c = el('span', { cls, text: item });
    if (clickable) {
      c.addEventListener('click', () => clickable(item));
    } else {
      c.classList.add('read-only');
    }
    wrap.appendChild(c);
  });
  return wrap;
}

function badgeRow(items) {
  if (!items || !items.length) return null;
  const wrap = el('div', { cls: 'chips' });
  items.forEach(([label, value]) => {
    if (!value) return;
    wrap.appendChild(el('span', { cls: 'chip read-only', text: `${label}: ${value}` }));
  });
  return wrap.childNodes.length ? wrap : null;
}

function fmtDate(iso) {
  if (!iso) return '';
  return iso.replace('T', ' ').replace('Z', '');
}

export function setupPanel({ ctx }) {
  const aside = document.getElementById('panel');
  const inner = document.getElementById('panel-inner');
  let openSha = null;

  function close() {
    aside.classList.remove('open');
    aside.setAttribute('aria-hidden', 'true');
    aside.hidden = true;
    openSha = null;
  }

  function showError(msg) {
    inner.innerHTML = '';
    const head = el('div', { cls: 'panel-head' },
      el('div', { cls: 'title' }, el('div', { cls: 'name', text: 'not found' })),
      el('button', { cls: 'close', text: '×', on: { click: close } }),
    );
    const e = el('div', { cls: 'empty' }, el('p', { text: msg }));
    inner.append(head, e);
  }

  async function open(sha) {
    aside.hidden = false;
    aside.setAttribute('aria-hidden', 'false');
    aside.classList.add('open');
    inner.innerHTML = '<div class="loading">loading…</div>';
    openSha = sha;
    const r = await getJSON(`/api/item?sha=${encodeURIComponent(sha)}`);
    if (openSha !== sha) return;
    if (!r.ok || !r.body || !r.body.payload) {
      showError(r.body?.error || 'no payload');
      return;
    }
    render(r.body.path, r.body.payload);
  }

  function render(path, payload) {
    inner.innerHTML = '';
    const source = payload.source || {};
    const model = payload.model || {};
    const name = path.split('/').pop();

    const head = el('div', { cls: 'panel-head' },
      el('div', { cls: 'title' },
        el('div', { cls: 'name', text: name }),
        el('div', { cls: 'sub', text: path }),
      ),
      el('button', { cls: 'close', text: '×', on: { click: close } }),
    );
    inner.appendChild(head);

    const img = el('img', { cls: 'panel-thumb', attrs: { src: `/api/thumb?sha=${encodeURIComponent(source.sha256 || '')}`, alt: name, loading: 'lazy' } });
    img.addEventListener('error', () => img.remove());
    inner.appendChild(img);

    const typeBadges = badgeRow([
      ['type', payload.content_type],
      ['template', payload.template],
      ['category', payload.category],
    ]);
    if (typeBadges) {
      const s = el('div', { cls: 'panel-section' });
      s.appendChild(typeBadges);
      inner.appendChild(s);
    }

    if (payload.description) inner.appendChild(section('description', el('p', { text: payload.description })));
    if (payload.context) inner.appendChild(section('context', el('p', { cls: 'italic', text: payload.context })));
    if (payload.punchline) inner.appendChild(section('punchline', el('p', { cls: 'italic', text: payload.punchline })));

    if (payload.text_ocr && payload.text_ocr.length) {
      inner.appendChild(section('ocr', el('pre', { text: payload.text_ocr.join('\n') })));
    }

    if (payload.tags && payload.tags.length) {
      const tagChips = chips(payload.tags, { cls: 'chip tag', clickable: (t) => ctx.filterByTag(t) });
      inner.appendChild(section(`tags · ${payload.tags.length}`, tagChips));
    }

    ARRAY_SECTIONS.forEach(([key, label]) => {
      const arr = payload[key];
      if (arr && arr.length) {
        inner.appendChild(section(label, chips(arr, { cls: 'chip' })));
      }
    });

    const kv = el('div', { cls: 'kv' });
    const pairs = [
      ['format', source.format],
      ['size', source.width && source.height ? `${source.width}×${source.height}` : ''],
      ['bytes', source.size_bytes ? source.size_bytes.toLocaleString() : ''],
      ['sha256', source.sha256 ? source.sha256.slice(0, 16) + '…' : ''],
      ['tagged at', fmtDate(payload.tagged_at)],
      ['vlm', model.vlm],
      ['ocr', model.ocr],
      ['prompt v', model.prompt_version],
    ];
    pairs.forEach(([k, v]) => {
      if (!v) return;
      kv.appendChild(el('span', { cls: 'k', text: k }));
      kv.appendChild(el('span', { cls: 'v', text: String(v) }));
    });
    if (kv.children.length) inner.appendChild(section('source', kv));
  }

  window.addEventListener('vtag:open-panel', (e) => open(e.detail.sha));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !aside.hidden) close();
  });
  aside.addEventListener('click', (e) => {
    if (e.target === aside) close();
  });
}
