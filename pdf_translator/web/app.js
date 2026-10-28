const $ = (id) => document.getElementById(id);

const state = {
  meta: null,
  page: 0,                                    // 内部 0-based，只在 API 边界 +1
  mode: localStorage.getItem('mode') === 'zh' ? 'zh' : 'en',
  data: {},                                   // 0-based 页 -> {paras, en, zh, has_zh, backend, ...}
  fontSize: parseInt(localStorage.getItem('fs') || '17', 10),
  busy: false,
  batchRunning: false,
};

const apiPage = (n) => n + 1;

async function jfetch(url, opts) {
  const r = await fetch(url, opts);
  let body = null;
  try { body = await r.json(); } catch (e) { /* 非 JSON 响应 */ }
  if (!r.ok) {
    const msg = (body && (body.error || body.message)) || ('HTTP ' + r.status);
    const err = new Error(body && body.hint ? msg + '（' + body.hint + '）' : msg);
    err.code = body && body.code;
    throw err;
  }
  return body;
}

const post = (url, body) =>
  jfetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

// ---------- 初始化 ----------
async function init() {
  state.meta = await jfetch('/api/meta');
  const m = state.meta;
  document.title = m.title;
  $('bookTitle').textContent = m.title;
  $('modelTag').textContent = m.backend_label || m.model || '';
  $('pageTotal').textContent = '/ ' + m.pages;
  $('pageInput').max = m.pages;
  document.documentElement.style.setProperty('--ratio', (m.page_size[0] / m.page_size[1]).toFixed(4));
  applyFont();

  if (m.backend !== 'api') {
    $('batchBtn').disabled = true;
    $('batchBtn').textContent = '后台批翻（仅 API 模式）';
    $('batchBtn').title = 'agent 模式下请在 agent 对话里用 next/save 循环；网页按 T 只翻当前页';
    $('keyHint').textContent = '← → 翻页 · T 用一次性 agent 翻译本页 · 译文自动落盘';
  }

  const saved = parseInt(localStorage.getItem('page:' + m.title) || '0', 10);
  const start = Number.isFinite(saved) ? Math.min(Math.max(saved, 0), m.pages - 1) : 0;
  await loadPage(start);
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
    try {
      await ensureZh(state.page, true);
    } catch (err) {
      alert('重译失败：' + err.message);
    }
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
  if (!state.data[n]) state.data[n] = await jfetch('/api/page?page=' + apiPage(n));
  render();
  $('pageImg').src = '/api/img?page=' + apiPage(n);

  // agent 模式 auto_translate=false：切页/切中文绝不自动拉起子进程，只有按 T 才翻
  if (state.mode === 'zh' && !state.data[n].has_zh && state.meta.auto_translate) {
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
      if (t < state.meta.pages && !(state.data[t] && state.data[t].zh)) pages.push(apiPage(t));
    }
    if (pages.length) post('/api/translate', { pages, wait: false }).catch(() => {});
  }
}

function preload(n) {
  if (n < 0 || n >= state.meta.pages) return;
  new Image().src = '/api/img?page=' + apiPage(n);
  if (!state.data[n]) jfetch('/api/page?page=' + apiPage(n)).then((d) => (state.data[n] = d)).catch(() => {});
}

// ---------- 渲染 ----------
function render() {
  const d = state.data[state.page];
  const hasZh = !!(d && d.has_zh);
  const showZh = state.mode === 'zh' && hasZh;
  $('zhLayer').hidden = !showZh;
  $('zhHint').hidden = !(state.mode === 'zh' && !hasZh);
  $('pageImg').style.visibility = showZh ? 'hidden' : 'visible';

  if (hasZh) {
    const label = d.backend === 'agent'
      ? '一次性 agent'
      : (state.meta.backend_label || state.meta.model || '');
    $('zhCaption').textContent = `第 ${state.page + 1} 页 · 中文（${label} 翻译，已缓存）`;
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
  const agent = state.meta.backend === 'agent';
  const text = force
    ? `正在重译第 ${n + 1} 页…`
    : agent
      ? `正在拉起一次性 agent 翻译第 ${n + 1} 页…（不常驻，完成即退出）`
      : `正在翻译第 ${n + 1} 页…（首次约 10–30 秒）`;
  showLoading(text);
  try {
    const r = await post('/api/translate', { page: apiPage(n), wait: true, force });
    const res = r.results[String(apiPage(n))];
    if (!res || !res.ok) {
      const msg = (res && res.error) || '翻译失败';
      throw new Error(res && res.hint ? msg + '（' + res.hint + '）' : msg);
    }
    state.data[n] = Object.assign({}, state.data[n], {
      zh: res.text, has_zh: true, backend: res.backend,
    });
    if (res.warning) console.warn(res.warning);
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
  if (!state.meta || state.meta.backend !== 'api') return;
  if (state.batchRunning) {
    await post('/api/batch', { stop: true });
    state.batchRunning = false;
    return;
  }
  const r = await post('/api/batch', { from: 1 });
  if (r.started) state.batchRunning = true;
  else if (r.reason === 'nothing_to_do') $('statusText').textContent = '没有需要翻译的页';
}

async function refreshStatus() {
  let s;
  try {
    s = await jfetch('/api/status');
  } catch (e) {
    return;
  }
  state.batchRunning = s.batch.running;
  if (s.backend === 'api') {
    $('batchBtn').textContent = s.batch.running
      ? `停止批翻 (${s.batch.done}/${s.batch.total})`
      : '后台翻译全书 (B)';
  }

  const tips = [`已译 ${s.translated}/${s.pages}`];
  if (s.skipped) tips.push(`跳过 ${s.skipped}`);
  if (s.empty_pages) tips.push(`空页 ${s.empty_pages}`);
  if (s.in_flight.length) tips.push(`进行中 ${s.in_flight.length}`);
  if (s.stats && s.stats.requests) tips.push(`${s.stats.requests} 次请求 / ${s.stats.tokens} tokens`);
  $('statusText').textContent = tips.join(' · ');
  $('progressBar').style.width = s.pages ? (s.translated / s.pages) * 100 + '%' : '0';

  // 当前页可能在外部 agent 或批翻里被翻好了
  if (s.translated_pages.includes(apiPage(state.page))) {
    const d = state.data[state.page];
    if (!d || !d.has_zh) {
      try {
        state.data[state.page] = await jfetch('/api/page?page=' + apiPage(state.page));
        if (state.mode === 'zh') render();
      } catch (e) { /* ignore */ }
    }
  }
}

init().catch((e) => {
  document.body.insertAdjacentHTML('beforeend', `<pre style="padding:20px;color:#c00">初始化失败: ${e.message}</pre>`);
});
