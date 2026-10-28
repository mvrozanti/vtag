import { getJSON, postJSON, getText } from '../api.js';

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

function fmtDuration(s) {
  s = Math.round(s);
  if (s <= 0) return '–';
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m ${sec}s`;
  return `${sec}s`;
}

function kvRow(parent, key, value) {
  parent.appendChild(el('dt', { text: key }));
  parent.appendChild(el('dd', { text: value || '–' }));
}

function box(title, aside, ...children) {
  const b = el('section', { cls: 'vt-box' });
  const h = el('div', { cls: 'vt-box__head' }, el('h3', { cls: 'mv-label', text: title }));
  if (aside) h.appendChild(aside);
  b.appendChild(h);
  children.forEach(c => c && b.appendChild(c));
  return b;
}

export async function render(container, { ctx }) {
  container.innerHTML = '';
  const wrap = el('div', { cls: 'vt-runner' });
  container.appendChild(wrap);

  const targetInput = el('input', {
    cls: 'mv-input',
    attrs: { type: 'text', id: 'r-target', placeholder: 'default target directory', spellcheck: 'false', autocapitalize: 'off', autocomplete: 'off', enterkeyhint: 'go' },
  });
  wrap.appendChild(el('label', { cls: 'mv-field vt-field', attrs: { for: 'r-target' } },
    el('span', { cls: 'mv-label', text: 'target' }),
    targetInput,
    el('span', { cls: 'mv-field__hint', text: 'blank tags the default directory recursively' }),
  ));

  const btnStart = el('button', { cls: 'mv-btn is-primary', text: 'start run', attrs: { type: 'button' } });
  const btnStop = el('button', { cls: 'mv-btn is-danger', text: 'stop run', attrs: { type: 'button', disabled: '' } });
  const btnRefresh = el('button', { cls: 'mv-btn', text: 'refresh', attrs: { type: 'button' } });
  const btnReindex = el('button', { cls: 'mv-btn', text: 'reindex search', attrs: { type: 'button' } });
  wrap.appendChild(el('div', { cls: 'vt-actions' }, btnStart, btnStop, btnRefresh, btnReindex));
  let targetDirty = false;
  targetInput.addEventListener('input', () => { targetDirty = true; });

  const meter = el('div', { cls: 'mv-meter' },
    el('span', { cls: 'mv-meter__label', text: 'done' }),
    el('span', { cls: 'mv-meter__track' }, el('span', { cls: 'mv-meter__fill', id: 'r-bar', attrs: { style: 'width:0%' } })),
    el('span', { cls: 'mv-meter__value', id: 'r-pct', text: '–' }),
  );
  const progressKV = el('dl', { cls: 'vt-kv', id: 'r-progress-kv' });
  wrap.appendChild(box('progress', el('span', { cls: 'mv-pill', id: 'r-state', text: '…' }), meter, progressKV));

  const runKV = el('dl', { cls: 'vt-kv', id: 'r-run-kv' });
  wrap.appendChild(box('run', null, runKV));

  const logPre = el('pre', { cls: 'vt-log', id: 'r-log', text: '(loading)' });
  const logBox = box('log tail', el('span', { cls: 'mv-field__hint', id: 'r-log-name', text: '' }), logPre);
  logBox.classList.add('is-wide');
  wrap.appendChild(logBox);

  btnReindex.addEventListener('click', async () => {
    btnReindex.disabled = true;
    const r = await postJSON('/api/reindex');
    if (r.status === 202) ctx.toast('reindex started');
    else if (r.status === 409) ctx.toast('reindex already running');
    else ctx.toast(r.body?.error || 'reindex failed');
    btnReindex.disabled = false;
  });

  async function refresh() {
    const [s, txt] = await Promise.all([
      getJSON('/api/status').catch(() => ({ body: null })),
      getText('/api/log').catch(() => ''),
    ]);
    const st = s.body || {};
    const running = !!st.running;
    const stateEl = document.getElementById('r-state');
    stateEl.textContent = running ? 'running' : 'idle';
    stateEl.className = 'mv-pill' + (running ? ' is-ok' : '');
    btnStart.disabled = running;
    btnStop.disabled = !running;
    targetInput.disabled = running;
    if (!targetDirty && document.activeElement !== targetInput) {
      targetInput.value = st.target_dir && st.target_dir !== '(unset)' ? st.target_dir : '';
    }

    const p = st.progress || {};
    const tagged = st.tagged_count || 0;
    const total = st.total_count || 0;
    const pct = total > 0 ? Math.round(100 * tagged / total) : 0;
    document.getElementById('r-bar').style.width = pct + '%';
    document.getElementById('r-pct').textContent = total ? `${pct}%` : '–';

    progressKV.innerHTML = '';
    kvRow(progressKV, 'tagged', total ? `${tagged.toLocaleString()} / ${total.toLocaleString()} (${pct}%)${st.scan_in_flight ? ' · scanning' : ''}` : (st.scan_in_flight ? 'scanning…' : '–'));
    kvRow(progressKV, 'untagged', st.untagged_count != null ? st.untagged_count.toLocaleString() : '–');
    kvRow(progressKV, 'ok / fail / skip', `${p.ok || 0} / ${p.fail || 0} / ${p.skip || 0}`);
    kvRow(progressKV, 'avg', p.avg_seconds ? `${p.avg_seconds.toFixed(1)}s / img` : '–');
    kvRow(progressKV, 'rate', p.images_per_min ? `${p.images_per_min.toFixed(1)} img/min` : '–');
    kvRow(progressKV, 'eta', st.eta_seconds ? fmtDuration(st.eta_seconds) : '–');
    if (p.done) {
      kvRow(progressKV, 'done', `tagged ${p.done.tagged} · skipped ${p.done.skipped} · failed ${p.done.failed} · total ${p.done.total}`);
    }

    runKV.innerHTML = '';
    kvRow(runKV, 'target', st.target_dir || '(unset)');
    kvRow(runKV, 'pid', st.pid || '–');
    kvRow(runKV, 'started', st.started_at || '–');
    if (!running && st.stopped_at) kvRow(runKV, 'stopped', st.stopped_at);
    kvRow(runKV, 'log', st.log ? st.log.split('/').pop() : '–');

    document.getElementById('r-log-name').textContent = st.log ? st.log.split('/').pop() : '';
    if (txt) {
      logPre.textContent = txt;

      logPre.scrollTop = logPre.scrollHeight;
    } else {
      logPre.textContent = '(no log)';

    }
  }

  btnStart.addEventListener('click', async () => {
    btnStart.disabled = true;
    const target = targetInput.value.trim();
    const r = await postJSON('/api/start', target ? { target } : {});
    if (!r.ok) ctx.toast(r.body?.error || 'start failed');
    targetDirty = false;
    refresh();
  });
  btnStop.addEventListener('click', async () => {
    btnStop.disabled = true;
    const r = await postJSON('/api/stop');
    if (!r.ok) ctx.toast(r.body?.error || 'stop failed');
    refresh();
  });
  btnRefresh.addEventListener('click', refresh);

  await refresh();
  timer = setInterval(refresh, 5000);
}

export function teardown() {
  if (timer) { clearInterval(timer); timer = null; }
}
