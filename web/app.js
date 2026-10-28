const $ = (id) => document.getElementById(id);

const state = {
  meta: null,
  page: 0,
  mode: localStorage.getItem('mode') === 'zh' ? 'zh' : 'en',
  data: {},                                   // 页号 -> {paras, zh, has_zh}
  fontSize: parseInt(localStorage.getItem('fs') || '17', 10),
  busy: false,
  batchRunning: false,
};

async function jfetch(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error((await r.text()).slice(0, 300));
  return r.json();
}

const post = (url, body) =>
  jfetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

// ---------- 初始化 ----------
async function init() {
  state.meta = await jfetch('/api/meta');
  const m = state.meta;
  document.title = m.title;
  $('bookTitle').textContent = m.title;
  $('modelTag').textContent = m.model;
  $('pageTotal').textContent = '/ ' + m.pages;
  $('pageInput').max = m.pages;
  document.documentElement.style.setProperty('--ratio', (m.page_size[0] / m.page_size[1]).toFixed(4));
  applyFont();

  const saved = parseInt(localStorage.getItem('page:' + m.title) || '0', 10);
  await loadPage(Math.min(Math.max(saved, 0), m.pages - 1));
  hideLoading();

  setInterval(refreshStatus, 2000);
  refreshStatus();
  bindUI();
}

function bindUI() {
  $('langBtn').onclick = toggleLang;
  $('batchBtn').onclick = toggleBatch;
  $('prevBtn').onclick = $('prevBtn2').onclick = () => go(state.page - 1);
  $('nextBtn').onclick = $('nextBtn2').onclick = () => go(state.page + 1);
  $('firstBtn').onclick = () => go(0);
  $('lastBtn').onclick = () => go(state.meta.pages - 1);
  $('pageInput').onchange = (e) => go(parseInt(e.target.value, 10) - 1);
  $('fontUp').onclick = () => { state.fontSize = Math.min(30, state.fontSize + 1); applyFont(); };
  $('fontDown').onclick = () => { state.fontSize = Math.max(12, state.fontSize - 1); applyFont(); };
  $('retranslateBtn').onclick = async () => {
    await ensureZh(state.page, true);
    render();
  };

  document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT') return;
    const k = e.key.toLowerCase();
    if (e.key === 'ArrowLeft') go(state.page - 1);
    else if (e.key === 'ArrowRight') go(state.page + 1);
    else if (e.key === 'PageUp') go(state.page - 10);
    else if (e.key === 'PageDown') go(state.page + 10);
    else if (e.key === 'Home') go(0);
    else if (e.key === 'End') go(state.meta.pages - 1);
    else if (k === 't') toggleLang();
    else if (k === 'b') toggleBatch();
  });
}

function applyFont() {
  document.documentElement.style.setProperty('--fs', state.fontSize + 'px');
  localStorage.setItem('fs', state.fontSize);
}

// ---------- 翻页 ----------
async function go(n) {
  if (state.meta && (n < 0 || n >= state.meta.pages)) return;
  await loadPage(n);
}

async function loadPage(n) {
  state.page = n;
  localStorage.setItem('page:' + state.meta.title, n);
  $('pageInput').value = n + 1;
  if (!state.data[n]) state.data[n] = await jfetch('/api/page?n=' + n);
  render();
  $('pageImg').src = '/api/img?n=' + n;
  if (state.mode === 'zh' && !state.data[n].has_zh) {
    try {
      await ensureZh(n);
      render();
    } catch (e) { console.warn(e); }
  }
  preload(n + 1);
  preload(n - 1);
  if (state.mode === 'zh' && state.meta.prefetch > 0) {
    const pages = [];
    for (let i = 1; i <= state.meta.prefetch; i++) {
      const t = n + i;
      if (t < state.meta.pages && !(state.data[t] && state.data[t].zh)) pages.push(t);
    }
    if (pages.length) post('/api/translate', { pages, wait: false }).catch(() => {});
  }
}

function preload(n) {
  if (n < 0 || n >= state.meta.pages) return;
  new Image().src = '/api/img?n=' + n;
  if (!state.data[n]) jfetch('/api/page?n=' + n).then((d) => (state.data[n] = d)).catch(() => {});
}

// ---------- 渲染 ----------
function render() {
  const d = state.data[state.page];
  const hasZh = !!(d && d.has_zh);
  $('zhLayer').hidden = !(state.mode === 'zh' && hasZh);
  $('pageImg').style.visibility = state.mode === 'zh' && hasZh ? 'hidden' : 'visible';
  if (hasZh) {
    $('zhCaption').textContent = `第 ${state.page + 1} 页 · 中文（${state.meta.model} 翻译，已缓存）`;
    $('zhText').innerHTML = '';
    (d.zh || '（本页无正文）').split(/\n{2,}/).forEach((p) => {
      const el = document.createElement('p');
      el.textContent = p.trim();
      $('zhText').appendChild(el);
    });
    $('zhLayer').scrollTop = 0;
  }
  $('langBtn').textContent = state.mode === 'zh' ? '原文 (T)' : '中文 (T)';
  $('langBtn').classList.toggle('primary', state.mode !== 'zh');
}

async function toggleLang() {
  if (state.busy) return;
  if (state.mode === 'zh') {
    state.mode = 'en';
    localStorage.setItem('mode', 'en');
    render();
    return;
  }
  state.mode = 'zh';
  localStorage.setItem('mode', 'zh');
  render();
  const d = state.data[state.page];
  if (!d || !d.has_zh) {
    try {
      await ensureZh(state.page);
      render();
    } catch (err) {
      state.mode = 'en';
      localStorage.setItem('mode', 'en');
      render();
      alert('翻译失败：' + err.message);
    }
  }
}

async function ensureZh(n, force = false) {
  state.busy = true;
  showLoading(`正在翻译第 ${n + 1} 页…（首次约 10–30 秒）`);
  try {
    const r = await post('/api/translate', { pages: [n], wait: true, force });
    const res = r.results[String(n)];
    if (!res || !res.ok) throw new Error((res && res.error) || '翻译失败');
    state.data[n] = Object.assign({}, state.data[n], { zh: res.text, has_zh: true });
    return res.text;
  } finally {
    state.busy = false;
    hideLoading();
  }
}

function showLoading(text) {
  $('loadingText').textContent = text;
  $('loading').hidden = false;
}
function hideLoading() {
  $('loading').hidden = true;
}

// ---------- 批翻与状态 ----------
async function toggleBatch() {
  if (state.batchRunning) {
    await post('/api/batch', { stop: true });
    $('batchBtn').textContent = '后台翻译全书 (B)';
    return;
  }
  const r = await post('/api/batch', { from: 0 });
  if (r.started) {
    $('batchBtn').textContent = '停止批翻 (B)';
    state.batchRunning = true;
  }
}

async function refreshStatus() {
  let s;
  try {
    s = await jfetch('/api/status');
  } catch (e) {
    return;
  }
  state.batchRunning = s.batch.running;
  $('batchBtn').textContent = s.batch.running ? `停止批翻 (${s.batch.done}/${s.batch.total})` : '后台翻译全书 (B)';

  const tips = [`已译 ${s.translated}/${s.pages}`];
  if (s.in_flight.length) tips.push(`进行中 ${s.in_flight.length}`);
  if (s.stats.requests) tips.push(`${s.stats.requests} 次请求 / ${s.stats.tokens} tokens`);
  if (!s.extract.ready) tips.push(`提取全文 ${s.extract.done}/${s.extract.total}`);
  $('statusText').textContent = tips.join(' · ');
  $('progressBar').style.width = s.pages ? (s.translated / s.pages) * 100 + '%' : '0';

  // 当前页在后台被翻好了
  if (s.translated_pages.includes(state.page)) {
    const d = state.data[state.page];
    if (!d || !d.has_zh) {
      try {
        state.data[state.page] = await jfetch('/api/page?n=' + state.page);
        if (state.mode === 'zh') render();
      } catch (e) { /* ignore */ }
    }
  }
}

init().catch((e) => {
  document.body.insertAdjacentHTML('beforeend', `<pre style="padding:20px;color:#c00">初始化失败: ${e.message}</pre>`);
});
