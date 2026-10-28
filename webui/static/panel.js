import { getJSON, thumbUrl } from './api.js';

const VIDEO_FORMATS = new Set(['MP4', 'MOV', 'MKV', 'WEBM', 'AVI']);
const VIDEO_EXT_RE = /\.(mp4|mov|mkv|webm|avi)$/i;

const ARRAY_SECTIONS = [
  ['user_labels', 'your labels'],
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

const TAGS_SHOWN = 12;

function section(title, ...children) {
  if (!children.length || children.every(c => c == null)) return null;
  const s = el('section', { cls: 'vt-section' });
  if (title) s.appendChild(el('h3', { cls: 'mv-label', text: title }));
  children.forEach(c => c && s.appendChild(c));
  return s;
}

function chips(items, { clickable = null, cls = '' } = {}) {
  if (!items || !items.length) return null;
  const wrap = el('div', { cls: 'mv-chips vt-wrap' });
  items.forEach(item => {
    if (clickable) {
      wrap.appendChild(el('button', { cls: `mv-chip ${cls}`.trim(), text: item, attrs: { type: 'button' }, on: { click: () => clickable(item) } }));
    } else {
      wrap.appendChild(el('span', { cls: `mv-chip is-static ${cls}`.trim(), text: item }));
    }
  });
  return wrap;
}

function tagChips(tags, onPick) {
  const wrap = chips(tags.slice(0, TAGS_SHOWN), { clickable: onPick, cls: 'vt-tag' });
  if (tags.length > TAGS_SHOWN) {
    const more = el('button', { cls: 'mv-chip', text: `+${tags.length - TAGS_SHOWN} more`, attrs: { type: 'button' } });
    more.addEventListener('click', () => {
      more.remove();
      tags.slice(TAGS_SHOWN).forEach(t => wrap.appendChild(el('button', { cls: 'mv-chip vt-tag', text: t, attrs: { type: 'button' }, on: { click: () => onPick(t) } })));
    });
    wrap.appendChild(more);
  }
  return wrap;
}

function fmtDate(iso) {
  if (!iso) return '';
  return iso.replace('T', ' ').replace('Z', '');
}

export function setupPanel({ ctx }) {
  const aside = document.getElementById('panel');
  const inner = document.getElementById('panel-inner');
  let openSha = null;
  let afterClose = null;

  function close() {
    aside.classList.remove('is-open');
    aside.setAttribute('aria-hidden', 'true');
    document.documentElement.classList.remove('vt-locked');
    openSha = null;
    setTimeout(() => { if (!openSha) { aside.hidden = true; inner.innerHTML = ''; } }, 220);
    const then = afterClose;
    afterClose = null;
    if (then) then();
  }

  function requestClose(then) {
    afterClose = then || null;
    if (history.state && history.state.vtPanel) history.back();
    else close();
  }

  function head(name, path, rawUrl) {
    const h = el('div', { cls: 'vt-sheet__head' });
    h.appendChild(el('button', { cls: 'mv-btn vt-icon', text: '✕', attrs: { type: 'button', 'aria-label': 'close' }, on: { click: () => requestClose() } }));
    const title = el('div', { cls: 'vt-sheet__title' });
    title.appendChild(el('b', { text: name }));
    if (path) {
      const dirs = path.split('/').slice(0, -1).filter(Boolean);
      const where = dirs.length > 3 ? `…/${dirs.slice(-3).join('/')}` : `/${dirs.join('/')}`;
      title.appendChild(el('span', { text: where, attrs: { title: path } }));
    }
    h.appendChild(title);
    if (rawUrl) h.appendChild(el('a', { cls: 'mv-btn vt-icon', text: '↗', attrs: { href: rawUrl, target: '_blank', rel: 'noopener', 'aria-label': 'open the original', title: 'open the original' } }));
    return h;
  }

  function showError(msg) {
    inner.innerHTML = '';
    inner.append(head('not found', '', ''), el('div', { cls: 'mv-empty', text: msg }));
  }

  async function open(sha) {
    if (!(history.state && history.state.vtPanel)) history.pushState({ vtPanel: true }, '');
    aside.hidden = false;
    aside.setAttribute('aria-hidden', 'false');
    document.documentElement.classList.add('vt-locked');
    requestAnimationFrame(() => aside.classList.add('is-open'));
    aside.scrollTop = 0;
    inner.innerHTML = '<div class="mv-empty">loading…</div>';
    openSha = sha;
    const r = await getJSON(`/api/item?sha=${encodeURIComponent(sha)}`);
    if (openSha !== sha) return;
    if (!r.ok || !r.body || !r.body.path) {
      showError(r.body?.error || 'not found');
      return;
    }
    render(sha, r.body.path, r.body.payload || null);
  }

  function render(sha, path, tagged) {
    const payload = tagged || {};
    inner.innerHTML = '';
    const source = payload.source || {};
    const model = payload.model || {};
    const name = path.split('/').pop();
    const rawUrl = `/api/raw?sha=${encodeURIComponent(sha)}`;
    inner.appendChild(head(name, path, rawUrl));
    const body = el('div', { cls: 'vt-sheet__body' });
    inner.appendChild(body);

    const fmt = String(source.format || '').toUpperCase();
    const isVideo = fmt ? VIDEO_FORMATS.has(fmt) : VIDEO_EXT_RE.test(path);
    if (sha && isVideo) {
      body.appendChild(el('video', { cls: 'mv-media vt-media', attrs: {
        controls: '', playsinline: '', preload: 'metadata', src: rawUrl, poster: thumbUrl(sha),
      } }));
    } else {
      const img = el('img', { cls: 'mv-media vt-media', attrs: { src: thumbUrl(sha), alt: name, title: 'tap to open full resolution', decoding: 'async' } });
      img.addEventListener('error', () => img.remove());
      img.addEventListener('click', () => window.open(rawUrl, '_blank', 'noopener'));
      const full = new Image();
      full.onload = () => { img.src = rawUrl; };
      full.src = rawUrl;
      body.appendChild(img);
    }

    const pick = (t) => requestClose(() => ctx.filterByTag(t));

    if (!tagged) body.appendChild(section('', el('p', { cls: 'is-quiet', text: 'Found by filename, not tagged yet.' })));

    const typeBadges = [['type', payload.content_type], ['template', payload.template], ['category', payload.category]]
      .filter(([, v]) => v).map(([k, v]) => `${k}: ${v}`);
    if (typeBadges.length) body.appendChild(section('', chips(typeBadges)));

    if (payload.description) body.appendChild(section('description', el('p', { text: payload.description })));
    if (payload.context) body.appendChild(section('context', el('p', { cls: 'is-quiet', text: payload.context })));
    if (payload.punchline) body.appendChild(section('punchline', el('p', { cls: 'is-quiet', text: payload.punchline })));
    if (payload.text_ocr && payload.text_ocr.length) body.appendChild(section('text in the image', el('pre', { text: payload.text_ocr.join('\n') })));
    if (payload.tags && payload.tags.length) body.appendChild(section(`tags · ${payload.tags.length}`, tagChips(payload.tags, pick)));

    const more = el('details', { cls: 'mv-status' });
    more.appendChild(el('summary', { cls: 'mv-status__summary' }, el('span', { cls: 'mv-label', text: 'more about it' })));
    const moreBody = el('div', { cls: 'mv-status__body' });
    ARRAY_SECTIONS.forEach(([key, label]) => {
      const arr = payload[key];
      if (arr && arr.length) moreBody.appendChild(section(label, chips(arr)));
    });
    const kv = el('dl', { cls: 'vt-kv' });
    [
      ['format', source.format],
      ['size', source.width && source.height ? `${source.width}×${source.height}` : ''],
      ['bytes', source.size_bytes ? source.size_bytes.toLocaleString() : ''],
      ['sha256', source.sha256 ? source.sha256.slice(0, 16) + '…' : ''],
      ['tagged at', fmtDate(payload.tagged_at)],
      ['vlm', model.vlm],
      ['ocr', model.ocr],
      ['prompt v', model.prompt_version],
    ].forEach(([k, v]) => {
      if (!v) return;
      kv.appendChild(el('dt', { text: k }));
      kv.appendChild(el('dd', { text: String(v) }));
    });
    if (kv.children.length) moreBody.appendChild(section('source', kv));
    if (moreBody.children.length) {
      more.appendChild(moreBody);
      body.appendChild(more);
    }
  }

  window.addEventListener('vtag:open-panel', (e) => open(e.detail.sha));
  window.addEventListener('popstate', () => { if (!aside.hidden && aside.classList.contains('is-open')) close(); });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !aside.hidden) requestClose();
  });
}
