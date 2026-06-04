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
  parent.appendChild(el('span', { cls: 'k', text: key }));
  parent.appendChild(el('span', { cls: 'v', text: value || '–' }));
}

export async function render(container, { ctx }) {
  container.innerHTML = '';

  const controls = el('div', { cls: 'runner-controls' });
  const btnStart = el('button', { cls: 'primary', text: 'start run' });
  const btnStop = el('button', { cls: 'danger', text: 'stop run', attrs: { disabled: '' } });
  const btnRefresh = el('button', { text: 'refresh' });
  controls.append(btnStart, btnStop, btnRefresh);
  container.appendChild(controls);

  const grid = el('div', { cls: 'runner-grid' });

  const progressCard = el('div', { cls: 'runner-card' });
  progressCard.appendChild(el('h3', {}, 'progress', el('span', { id: 'r-state', cls: 'v', text: '…' })));
  const bar = el('div', { cls: 'bar-track' }, el('span', { cls: 'bar-fill', id: 'r-bar', attrs: { style: 'width:0%' } }));
  progressCard.appendChild(bar);
  const progressKV = el('div', { cls: 'kv', id: 'r-progress-kv' });
  progressCard.appendChild(progressKV);

  const runCard = el('div', { cls: 'runner-card' });
  runCard.appendChild(el('h3', { text: 'run' }));
  const runKV = el('div', { cls: 'kv', id: 'r-run-kv' });
  runCard.appendChild(runKV);

  grid.append(progressCard, runCard);
  container.appendChild(grid);

  const logCard = el('div', { cls: 'runner-card' });
  logCard.style.marginTop = '1rem';
  logCard.appendChild(el('h3', {},
    'log tail',
    el('span', { cls: 'v', id: 'r-log-name', text: '' }),
  ));
  const logPre = el('pre', { cls: 'log-pre', id: 'r-log', text: '(loading)' });
  logCard.appendChild(logPre);
  container.appendChild(logCard);

  async function refresh() {
    const [s, txt] = await Promise.all([
      getJSON('/api/status').catch(() => ({ body: null })),
      getText('/api/log').catch(() => ''),
    ]);
    const st = s.body || {};
    const running = !!st.running;
    document.getElementById('r-state').textContent = running ? 'running' : 'idle';
    btnStart.disabled = running;
    btnStop.disabled = !running;

    const p = st.progress || {};
    const tagged = st.tagged_count || 0;
    const total = st.total_count || 0;
    const pct = total > 0 ? Math.round(100 * tagged / total) : 0;
    document.getElementById('r-bar').style.width = pct + '%';

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
      logPre.classList.remove('empty');
      logPre.scrollTop = logPre.scrollHeight;
    } else {
      logPre.textContent = '(no log)';
      logPre.classList.add('empty');
    }
  }

  btnStart.addEventListener('click', async () => {
    btnStart.disabled = true;
    const r = await postJSON('/api/start');
    if (!r.ok) ctx.toast(r.body?.error || 'start failed');
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
