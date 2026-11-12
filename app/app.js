'use strict';

const S = {
  role: 'store', me: null, kind: 'pusher', pendingUnit: null, scan: null,
  shopId: null, day: null, view: 'entry',
  shops: [], units: [], entry: [], allRows: [], confirmed: false,
  recon: null, setup: null, bdOk: true,
};

const $ = (id) => document.getElementById(id);
const nf = new Intl.NumberFormat('zh-CN');
const nf1 = new Intl.NumberFormat('zh-CN', { maximumFractionDigits: 1 });
const el = UI.el;

const fmt = (v) => (v === null || v === undefined || v === '' ? '-' : nf.format(Math.round(v)));
const fmt1 = (v) => (v === null || v === undefined || v === '' ? '-' : nf1.format(v));
const signed = (v) => {
  if (v === null || v === undefined) return '';
  const n = Math.round(v);
  return (n > 0 ? '+' : n < 0 ? '−' : '') + nf.format(Math.abs(n));
};

const TOKEN_KEY = 'meter_token';
const CWSJ_KEY = 'cwsj_token_v1';

// 和任务平台同域时，直接复用它的登录态，不用二次登录
// 但主动点过「退出」就不再复用，否则退不掉
function getToken() {
  if (localStorage.getItem('meter_logout') === '1') return '';
  return localStorage.getItem(TOKEN_KEY)
    || localStorage.getItem(CWSJ_KEY)
    || '';
}

// 部署在 /meter/ 子路径下也能用
const BASE = (() => {
  const p = location.pathname;
  return p.endsWith('/') ? p : p.replace(/[^/]*$/, '');
})();

async function api(path, body) {
  const headers = {};
  const tok = getToken();
  if (tok) headers.Authorization = 'Bearer ' + tok;
  const opt = { headers };
  if (body !== undefined) {
    opt.method = 'POST';
    headers['Content-Type'] = 'application/json';
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(BASE + path.replace(/^\//, ''), opt);
  let data = null;
  try { data = await r.json(); } catch (_) { data = null; }
  if (!r.ok) throw new Error((data && data.error) || ('HTTP ' + r.status));
  return data;
}

const toast = UI.toast;

/* ------------------------------------------------------------ 视图 */
function syncDayLock() {
  const wrap = $('dayPick');
  if (!wrap) return;
  const store = S.role === 'store';
  // 门店：日期只在核对页出现（填写页固定昨天，不需要选）
  // 总部：填写 / 核对 / 全链都能切日期
  const show = store ? S.view === 'recon'
    : ['entry', 'recon', 'overview'].indexOf(S.view) >= 0;
  wrap.hidden = !show;
  if (store && S.view === 'entry' && S.editDay && S.day !== S.editDay) {
    S.day = S.editDay;
    if (dayPicker) dayPicker.set(S.editDay);
  }
}

function setView(v) {
  S.view = v;
  syncDayLock();
  ['home', 'entry', 'recon', 'overview', 'settings', 'scan'].forEach((k) => {
    $('view-' + k).hidden = k !== v;
  });
  document.querySelectorAll('.nav__item').forEach((b) => {
    if (b.dataset.view === v) b.setAttribute('aria-current', 'page');
    else b.removeAttribute('aria-current');
  });
  if (v === 'home') loadHome();
  if (v === 'entry') loadEntry();
  if (v === 'recon') loadRecon();
  if (v === 'overview') loadOverview();
  if (v === 'settings') loadSettings();
}

function welcomeDialog(b) {
  const units = b.units || [];
  const pusher = units.filter((u) => (u.kind || 'pusher') !== 'pachinko').length;
  const pach = units.filter((u) => (u.kind || '') === 'pachinko').length;

  const scrim = el('div', 'scrim');
  const dlg = el('div', 'dlg');
  dlg.setAttribute('role', 'dialog');
  dlg.setAttribute('aria-modal', 'true');

  dlg.appendChild(el('h2', 'dlg__t', '先确认一下机台'));
  const body = el('p', 'dlg__d');
  body.textContent = '这家店有 ' + pusher + ' 台推币机、' + pach + ' 台弹珠机。'
    + '先去设置里核对清单，不对的话填出来的数对不上账。';
  dlg.appendChild(body);

  const acts = el('div', 'dlg__acts');
  const go = el('button', 'btn btn--primary', '去核对机台');
  go.type = 'button';
  acts.appendChild(go);
  dlg.appendChild(acts);
  scrim.appendChild(dlg);
  document.body.appendChild(scrim);
  go.focus();

  // 一出现就记下：保证这家店只弹这一次，不依赖用户点没点
  api('/api/shop/welcome', { shop_id: b.shop_id }).catch(() => {});

  go.addEventListener('click', () => {
    scrim.remove();
    setView('settings');
  });
}

/* ------------------------------------------------------------ 扫码 */
let scanStream = null;
let scanRaf = 0;

function cameraOk() {
  return !!(window.isSecureContext && navigator.mediaDevices
    && navigator.mediaDevices.getUserMedia);
}

async function openScanner() {
  $('scanner').hidden = false;
  document.body.dataset.scanning = '1';

  if (!cameraOk()) {
    $('scanHint').textContent = '当前是 HTTP，浏览器不给摄像头，改用拍照识别';
    $('scanFile').click();
    return;
  }
  try {
    scanStream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: 'environment' } }, audio: false,
    });
    const v = $('scanVideo');
    v.srcObject = scanStream;
    await v.play();
    $('scanHint').textContent = '对准机台上的二维码';
    setupTorch();
    tickScan();
  } catch (e) {
    $('scanHint').textContent = e.name === 'NotAllowedError'
      ? '摄像头权限被拒绝，去浏览器设置里允许' : ('打不开摄像头：' + e.message);
  }
}

function setupTorch() {
  const btn = $('scanTorch');
  btn.hidden = true;
  if (!scanStream) return;
  const track = scanStream.getVideoTracks()[0];
  const caps = track && track.getCapabilities ? track.getCapabilities() : {};
  if (!caps || !caps.torch) return;
  btn.hidden = false;
  let on = false;
  btn.onclick = async () => {
    on = !on;
    try {
      await track.applyConstraints({ advanced: [{ torch: on }] });
      btn.setAttribute('aria-pressed', on ? 'true' : 'false');
    } catch (e) {
      btn.hidden = true;
    }
  };
}

function stopScanner() {
  cancelAnimationFrame(scanRaf);
  if (scanStream) {
    scanStream.getTracks().forEach((t) => t.stop());
    scanStream = null;
  }
  const v = $('scanVideo');
  if (v) v.srcObject = null;
  $('scanner').hidden = true;
  delete document.body.dataset.scanning;
}

function tickScan() {
  const v = $('scanVideo');
  if (!v || !scanStream) return;
  if (v.readyState === v.HAVE_ENOUGH_DATA && v.videoWidth) {
    const cv = document.createElement('canvas');
    cv.width = v.videoWidth;
    cv.height = v.videoHeight;
    const ctx = cv.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(v, 0, 0);
    const img = ctx.getImageData(0, 0, cv.width, cv.height);
    const res = window.jsQR
      && window.jsQR(img.data, cv.width, cv.height, { inversionAttempts: 'dontInvert' });
    if (res && res.data) { onScanResult(res.data); return; }
  }
  scanRaf = requestAnimationFrame(tickScan);
}

function codeFromScan(text) {
  try {
    const u = new URL(text, location.origin);
    const c = u.searchParams.get('c');
    const id = u.searchParams.get('u');
    if (c) return { code: c };
    if (id) return { id };
  } catch (e) { /* 不是 URL，往下当编号 */ }
  const t = String(text || '').trim();
  return /^[A-Za-z0-9_-]{2,20}$/.test(t) ? { code: t } : null;
}

function onScanResult(text) {
  const hit = codeFromScan(text);
  stopScanner();
  if (!hit) { toast('这个码不是机台码', 'bad'); return; }
  location.href = hit.code
    ? BASE + '?c=' + encodeURIComponent(hit.code)
    : BASE + '?u=' + encodeURIComponent(hit.id);
}

function scanPhoto(file) {
  if (!file) { stopScanner(); return; }
  const img = new Image();
  img.onload = () => {
    const cv = document.createElement('canvas');
    cv.width = img.width;
    cv.height = img.height;
    const ctx = cv.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(img, 0, 0);
    const data = ctx.getImageData(0, 0, cv.width, cv.height);
    const res = window.jsQR && window.jsQR(data.data, cv.width, cv.height);
    URL.revokeObjectURL(img.src);
    if (res && res.data) onScanResult(res.data);
    else { stopScanner(); toast('没认出二维码，靠近点重拍一张', 'bad'); }
  };
  img.onerror = () => { stopScanner(); toast('这张图读不了', 'bad'); };
  img.src = URL.createObjectURL(file);
}

/* ------------------------------------------------------------ 扫码填写 */
async function loadScan(code, id) {
  const qs = code ? ('c=' + encodeURIComponent(code)) : ('id=' + encodeURIComponent(id));
  const d = await api('/api/unit?' + qs);
  S.scan = d;
  S.kind = d.kind;
  setView('scan');

  const wrap = $('scanBody');
  wrap.innerHTML = '';

  const head = el('div', 'scan__head');
  head.appendChild(el('p', 'scan__kind', d.kind === 'pachinko' ? '弹珠机' : '推币机'));
  head.appendChild(el('h1', 'scan__name', d.machine.name));
  head.appendChild(el('p', 'scan__meta',
    d.rows.length + ' 块码表 · ' + d.shop.name));
  wrap.appendChild(head);

  d.rows.forEach((r) => {
    const card = el('div', 'scan__row');

    const h = el('div', 'scan__row-h');
    if (d.rows.length > 1) h.appendChild(el('span', 'scan__pos', 'P' + (r.pos || 1)));
    h.appendChild(el('span', 'scan__comm', r.comms.join(' / ')));
    if (r.prev_in !== null && r.prev_in !== undefined) {
      h.appendChild(el('span', 'scan__prev', fmt(r.prev_in) + ' → ' + fmt(r.prev_out)));
    }
    card.appendChild(h);

    const inputs = el('div', 'scan__inputs');
    const noop = () => {};
    if (d.kind === 'pachinko') {
      inputs.appendChild(makeCell(r, 'in_r', '投币', 'prev_in', noop));
      inputs.appendChild(makeCell(r, 'p_tokens', '投珠', 'prev_tokens', noop));
      inputs.appendChild(makeCell(r, 'p_out', '出珠', 'prev_out_total', noop));
      inputs.appendChild(makeCell(r, 'p_cards', '出卡', 'prev_cards', noop));
    } else {
      inputs.appendChild(makeCell(r, 'in_r', '入', 'prev_in', noop));
      inputs.appendChild(makeCell(r, 'out_r', '出', 'prev_out', noop));
    }
    card.appendChild(inputs);
    wrap.appendChild(card);
  });

  const foot = el('div', 'scan__foot');
  const save = el('button', 'btn btn--primary', '保存这台');
  save.type = 'button';
  save.addEventListener('click', () => saveScan(save));
  foot.appendChild(save);

  const next = el('button', 'btn', d.next ? '下一台 · ' + d.next.name : '没有下一台');
  next.type = 'button';
  next.disabled = true;
  if (d.next) {
    next.addEventListener('click', () => {
      location.href = BASE + '?c=' + encodeURIComponent(d.next.code);
    });
  }
  foot.appendChild(next);
  wrap.appendChild(foot);

  S.scanNext = next;
}

async function saveScan(btn) {
  const items = S.scan.rows
    .filter((r) => r.in_r !== null || r.out_r !== null || r.p_tokens !== null
      || r.p_out !== null || r.p_cards !== null)
    .map((r) => ({
      unit_id: r.unit_id, in_r: r.in_r, out_r: r.out_r,
      p_tokens: r.p_tokens, p_out: r.p_out, p_cards: r.p_cards,
    }));
  if (!items.length) { toast('还没填数', 'bad'); return; }
  btn.disabled = true;
  try {
    await api('/api/entry', { shop_id: S.scan.shop.id, day: S.scan.day, items });
    await api('/api/fetch', { shop_id: S.scan.shop.id, day: S.scan.day }).catch(() => {});
    toast('已保存');
    if (S.scanNext) S.scanNext.disabled = false;
  } catch (e) {
    toast(String(e.message || e), 'bad');
  } finally {
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------ 导航 */
async function loadHome() {
  const d = await api(`/api/kinds?shop_id=${encodeURIComponent(S.shopId)}&day=${S.day}`);
  const wrap = $('kindList');
  wrap.innerHTML = '';

  d.kinds.forEach((k) => {
    const btn = el('button', 'kind');
    btn.type = 'button';
    btn.disabled = k.units === 0;

    btn.appendChild(el('span', 'kind__name', k.name));

    const meta = el('span', 'kind__meta');
    meta.textContent = k.units
      ? k.units + ' 台 · 今天已填 ' + k.filled
      : '这家店没有' + k.name;
    btn.appendChild(meta);

    if (k.units) {
      const st = el('span', 'kind__st');
      if (k.ok > 0) {
        st.textContent = k.ok + ' 台可核对 · 误差 ' + fmt1(k.diff_ok);
      } else if (k.filled > 0) {
        st.textContent = '已填 ' + k.filled + ' 台，等宝点数据';
      } else {
        st.textContent = '还没开始';
      }
      btn.appendChild(st);
    }

    const go = el('span', 'kind__go', k.units ? '进入' : '');
    btn.appendChild(go);

    btn.addEventListener('click', () => {
      S.kind = k.kind;
      setView('entry');
    });
    wrap.appendChild(btn);
  });
}

/* ------------------------------------------------------------ 录入 */
async function loadEntry() {
  if (S.role === 'store' && S.view === 'entry' && S.editDay) S.day = S.editDay;
  const d = await api(`/api/entry?shop_id=${encodeURIComponent(S.shopId)}&day=${S.day}&kind=${S.kind}`);
  S.allRows = d.rows.map((r) => Object.assign({}, r, { comm_count: r.comms.length }));
  S.confirmed = !!d.confirmed;
  S.entry = S.allRows.filter((r) => r.active !== false);

  $('confirmPanel').hidden = true;
  $('entryList').hidden = false;

  if (!S.allRows.length) {
    renderEntry();
    $('entryLede').textContent = '这家店没有参与核对的机台，去设置里加';
    return;
  }
  $('entryLede').textContent = (S.kind === 'pachinko' ? '弹珠机' : '推币机')
    + ' · ' + S.entry.length + ' 台 · 每台填两个累计读数，回车跳下一个'
    + (S.role === 'store' ? ' · 填的是 ' + S.day + ' 关门时的数' : '');
  if (!S.confirmed) {
    $('confirmPanel').hidden = false;
    $('entryList').hidden = true;
    renderConfirm();
  } else {
    renderEntry();
  }
}

function renderConfirm() {
  const ul = $('confirmList');
  ul.innerHTML = '';
  S.allRows.forEach((r) => {
    const li = el('li', 'sheet__row');
    li.dataset.off = r.active === false ? 'true' : 'false';

    const cb = el('label', 'cbx');
    const inp = el('input');
    inp.type = 'checkbox';
    inp.checked = r.active !== false;
    inp.dataset.unit = r.unit_id;
    inp.addEventListener('change', () => { li.dataset.off = inp.checked ? 'false' : 'true'; });
    const box = el('span', 'cbx__box');
    box.setAttribute('aria-hidden', 'true');
    box.innerHTML = '<svg viewBox="0 0 16 16"><path d="M3.5 8.5l3 3 6-7" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    cb.appendChild(inp);
    cb.appendChild(box);
    cb.appendChild(el('span', 'sheet__name', r.name));
    li.appendChild(cb);

    const meta = [];
    if (r.comm_count) meta.push(r.comm_count + ' 个通讯ID');
    if (r.shared) meta.push('共用码表');
    if (!r.has_card) meta.push('宝点无数据');
    li.appendChild(el('span', 'sheet__meta', meta.join(' · ')));
    ul.appendChild(li);
  });
  $('confirmHint').textContent = '共 ' + S.allRows.length + ' 台，勾选的要填';
}

async function confirmUnits() {
  const ids = [...document.querySelectorAll('#confirmList input[type=checkbox]')]
    .filter((cb) => cb.checked)
    .map((cb) => Number(cb.dataset.unit));
  if (!ids.length) { toast('至少留一台', 'bad'); return; }
  const btn = $('btnConfirm');
  btn.disabled = true;
  try {
    await api('/api/units/confirm', { shop_id: S.shopId, unit_ids: ids });
    toast('清单已确认');
    await loadEntry();
  } catch (e) {
    toast(String(e.message || e), 'bad');
  } finally {
    btn.disabled = false;
  }
}

function renderEntry() {
  const ul = $('entryList');
  ul.innerHTML = '';

  S.entry.forEach((r) => {
    const li = el('li', 'lrow');
    li.dataset.unit = String(r.unit_id);

    const id = el('div', 'lrow__id');
    if (r.pos) id.appendChild(el('span', 'lrow__pos', 'P' + r.pos));
    id.appendChild(el('span', 'lrow__name', r.name));
    if (r.shared || r.comm_count > 1) {
      id.appendChild(el('span', 'lrow__meta', r.shared ? '共用码表' : r.comm_count + ' 个卡头'));
    }
    if (r.prev_in !== null && r.prev_in !== undefined) {
      const pv = (r.kind === 'pachinko')
        ? '昨日 投币 ' + fmt(r.prev_in)
        : '昨日 入 ' + fmt(r.prev_in) + ' · 出 ' + fmt(r.prev_out);
      id.appendChild(el('span', 'lrow__prev', pv));
    }
    li.appendChild(id);

    const inputs = el('div', 'lrow__inputs');
    if (r.kind === 'pachinko') {
      inputs.dataset.n = '4';
      inputs.appendChild(makeCell(r, 'in_r', '投币', 'prev_in'));
      inputs.appendChild(makeCell(r, 'p_tokens', '投珠', 'prev_tokens'));
      inputs.appendChild(makeCell(r, 'p_out', '出珠', 'prev_out_total'));
      inputs.appendChild(makeCell(r, 'p_cards', '出卡', 'prev_cards'));
    } else {
      inputs.appendChild(makeCell(r, 'in_r', '入', 'prev_in'));
      inputs.appendChild(makeCell(r, 'out_r', '出', 'prev_out'));
    }
    li.appendChild(inputs);

    ul.appendChild(li);
  });

  updateProgress();
  refreshNote();
}

function focusUnit(uid) {
  const li = document.querySelector('.lrow[data-unit="' + uid + '"]');
  if (!li) return;
  li.scrollIntoView({ block: 'center', behavior: 'smooth' });
  li.dataset.hit = 'true';
  const inp = li.querySelector('.input--num');
  if (inp) setTimeout(() => inp.focus(), 260);
  setTimeout(() => { delete li.dataset.hit; }, 2600);
}

function updateProgress() {
  const total = S.entry.length;
  const filled = S.entry.filter(
    (r) => r.in_r !== null || r.out_r !== null || r.p_tokens !== null
      || r.p_out !== null || r.p_cards !== null).length;
  const box = $('entryProgress');
  if (!total) { box.hidden = true; return; }
  box.hidden = false;
  $('progText').textContent = filled + ' / ' + total;
  $('progHint').textContent = filled === total ? '填完了，可以保存' : '';
  const ticks = $('progTicks');
  if (ticks.childElementCount !== total) {
    ticks.innerHTML = '';
    for (let i = 0; i < total; i++) {
      const t = document.createElement('span');
      t.className = 'tick';
      ticks.appendChild(t);
    }
  }
  [...ticks.children].forEach((t, i) => {
    t.dataset.on = i < filled ? 'true' : 'false';
  });
  $('btnSave').disabled = filled === 0;
  $('saveHint').textContent = filled === 0
    ? '先填至少一台'
    : (filled === total ? '都填完了' : '还剩 ' + (total - filled) + ' 台');
  $('btnToRecon').hidden = filled < total || total === 0;
}

function makeCell(row, field, label, prevField, onChange) {
  const cell = el('label', 'cell');
  cell.appendChild(el('span', 'cell__k', label));

  const input = el('input', 'input input--num');
  input.type = 'text';
  input.inputMode = 'numeric';
  input.autocomplete = 'off';
  input.setAttribute('aria-label', row.name + ' 今日' + (label === '入' ? '入币' : '出币'));
  input.placeholder = '';
  if (row[field] !== null && row[field] !== undefined) input.value = String(row[field]);
  cell.appendChild(input);

  const d = el('span', 'cell__d');
  cell.appendChild(d);

  const paint = () => {
    const prev = row[prevField || (field === 'in_r' ? 'prev_in' : 'prev_out')];
    const v = row[field];
    if (prev === null || prev === undefined || v === null || v === undefined) {
      d.textContent = '';
      return;
    }
    const diff = v - prev;
    d.textContent = signed(diff);
    d.dataset.neg = diff < 0 ? 'true' : 'false';
  };
  input.addEventListener('input', () => {
    const raw = String(input.value || '').replace(/[,\s]/g, '');
    const v = raw === '' ? null : Number(raw);
    input.setAttribute('aria-invalid', raw !== '' && !isFinite(v) ? 'true' : 'false');
    row[field] = raw !== '' && isFinite(v) ? v : null;
    paint();
    if (onChange) onChange();
    else updateProgress();
  });
  input.addEventListener('focus', () => input.select());
  input.addEventListener('keydown', (e) => {
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const all = [...document.querySelectorAll('#entryList .input--num')];
    const i = all.indexOf(input);
    if (i >= 0 && i < all.length - 1) all[i + 1].focus();
    else $('btnSave').focus();
  });
  paint();
  return cell;
}

function refreshNote() {
  const bad = S.entry.filter((r) => (r.in_r !== null && r.prev_in !== null && r.in_r < r.prev_in)
    || (r.out_r !== null && r.prev_out !== null && r.out_r < r.prev_out));
  const note = $('entryNote');
  if (!bad.length) { note.hidden = true; return; }
  note.hidden = false;
  $('noteT').textContent = '有 ' + bad.length + ' 个读数比昨天小';
  $('noteD').textContent = bad.map((r) => r.name).join('、') + '。保存后在核对里标成待确认。';
}

async function saveAndFetch() {
  const btn = $('btnSave');
  const items = S.entry
    .filter((r) => r.in_r !== null || r.out_r !== null || r.p_tokens !== null
      || r.p_out !== null || r.p_cards !== null)
    .map((r) => ({
      unit_id: r.unit_id, in_r: r.in_r, out_r: r.out_r,
      p_tokens: r.p_tokens, p_out: r.p_out, p_cards: r.p_cards,
    }));
  if (!items.length) { toast('还没填读数', 'bad'); return; }

  btn.dataset.loading = 'true';
  btn.disabled = true;
  $('saveHint').textContent = '保存中';
  try {
    const res = await api('/api/entry', { shop_id: S.shopId, day: S.day, items });
    $('saveHint').textContent = '已存 ' + res.saved + ' 台，正在拉宝点';
    await api('/api/fetch', { shop_id: S.shopId, day: S.day });
    $('saveHint').textContent = '已保存并拉取';
    toast('已保存');
    await loadEntry();
  } catch (e) {
    $('saveHint').textContent = '拉取失败，读数已保存';
    toast(String(e.message || e), 'bad');
  } finally {
    btn.dataset.loading = 'false';
    btn.disabled = false;
  }
}

/* ------------------------------------------------------------ 核对 */
const TONE = {
  '正常': 'ok',
  '第一次填': 'warn',
  '读数比昨天小': 'bad',
  '今天还没填': 'warn',
  '宝点今天没有数据': 'warn',
};

function statusTag(text) {
  const s = el('span', 'st');
  s.dataset.tone = TONE[text] || '';
  s.appendChild(el('span', 'st__dot'));
  s.appendChild(el('span', null, text));
  return s;
}

async function loadRecon() {
  try {
    const d = await api(`/api/recon?shop_id=${encodeURIComponent(S.shopId)}&day=${S.day}&kind=${S.kind}`);
    S.recon = d;
    renderRecon();
  } catch (e) {
    toast(String(e.message || e), 'bad');
  }
}

function renderRecon() {
  const d = S.recon;
  if (!d) return;
  const s = d.summary || {};
  $('reconLede').textContent = d.day + ' · '
    + (S.kind === 'pachinko' ? '宝点投币 对比 码表投币' : '卡头收币 减 码表收币');
  $('fDiff').textContent = fmt1(s.diff_ok);
  $('fDiff').dataset.tone = Math.abs(s.diff_ok) >= 1000 ? 'bad' : '';
  $('fMeta').textContent = `${s.ok} 台可核对 · 含异常合计 ${fmt1(s.diff_all)} · 误差超千 ${s.big} 台`;

  const pach = S.kind === 'pachinko';

  const head = $('reconHead');
  head.innerHTML = '';
  const hr = el('tr');
  const cols = pach
    ? [['机台', ''], ['宝点投币', 'num'], ['码表投币', 'num'], ['差', 'num'],
       ['返珠率', 'num'], ['收益', 'num'], ['状态', ''], ['原因', '']]
    : [['机台', ''], ['上币', 'num'], ['下币', 'num'], ['码表收币', 'num'],
       ['误差', 'num'], ['状态', ''], ['原因', '']];
  cols.forEach(([t, cls]) => {
    const th = el('th', cls, t);
    th.scope = 'col';
    hr.appendChild(th);
  });
  head.appendChild(hr);

  const body = $('reconBody');
  body.innerHTML = '';
  $('reconEmpty').hidden = d.rows.length > 0;

  d.rows.forEach((r) => {
    const tr = el('tr');
    tr.appendChild(el('td', 'name', r.name));

    if (pach) {
      const pm = r.p || {};
      tr.appendChild(el('td', 'num', fmt(r.up)));
      tr.appendChild(el('td', 'num', fmt(r.meter)));
      const td = el('td', 'num lead', fmt(r.diff));
      if (r.diff !== null && Math.abs(r.diff) >= 1) td.dataset.tone = 'bad';
      tr.appendChild(td);
      tr.appendChild(el('td', 'num',
        pm.rate === null || pm.rate === undefined ? '-' : (pm.rate * 100).toFixed(1) + '%'));
      const pr = el('td', 'num lead', pm.profit === null || pm.profit === undefined
        ? '-' : fmt1(pm.profit));
      if (pm.profit !== null && pm.profit !== undefined && pm.profit < 0) pr.dataset.tone = 'bad';
      tr.appendChild(pr);
    } else {
      tr.appendChild(el('td', 'num', fmt(r.up)));
      tr.appendChild(el('td', 'num', fmt(r.down)));
      tr.appendChild(el('td', 'num', fmt(r.meter)));
      const td = el('td', 'num lead', fmt(r.diff));
      if (r.diff !== null && Math.abs(r.diff) >= 1000) td.dataset.tone = 'bad';
      tr.appendChild(td);
    }

    const st = el('td');
    st.appendChild(statusTag(r.status));
    tr.appendChild(st);
    tr.appendChild(reasonCell(r));
    body.appendChild(tr);
  });
}

const REASONS = ['换码表或归零重计', '机器故障没走系统', '未下完分断电',
  '人为抄错已改', '其他'];

function reasonCell(r) {
  const td = el('td', 'reason');
  const hasDiff = r.diff !== null && r.diff !== undefined && Math.abs(r.diff) >= 1;
  if (r.reason) {
    const b = el('button', 'reason__chip', r.reason);
    b.type = 'button';
    if (r.reason_note) b.title = r.reason_note;
    b.addEventListener('click', () => reasonDialog(r));
    td.appendChild(b);
  } else if (hasDiff) {
    const b = el('button', 'reason__add', '填原因');
    b.type = 'button';
    b.addEventListener('click', () => reasonDialog(r));
    td.appendChild(b);
  }
  return td;
}

function reasonDialog(r) {
  const scrim = el('div', 'scrim');
  const dlg = el('div', 'dlg');
  dlg.setAttribute('role', 'dialog');
  dlg.setAttribute('aria-modal', 'true');
  dlg.appendChild(el('h2', 'dlg__t', r.name));
  const sub = el('p', 'dlg__d');
  sub.textContent = '误差 ' + fmt(r.diff) + '，说明一下原因';
  dlg.appendChild(sub);

  const list = el('div', 'reason-list');
  let picked = r.reason || '';
  const btns = [];
  REASONS.forEach((txt) => {
    const b = el('button', 'reason-opt', txt);
    b.type = 'button';
    b.setAttribute('aria-pressed', txt === picked ? 'true' : 'false');
    b.addEventListener('click', () => {
      picked = txt;
      btns.forEach((x) => x.setAttribute('aria-pressed', x === b ? 'true' : 'false'));
      note.focus();
    });
    btns.push(b);
    list.appendChild(b);
  });
  dlg.appendChild(list);

  const lab = el('label', 'reason-label', '补充说明（可选，不填也行）');
  const note = el('input', 'input');
  note.type = 'text';
  note.placeholder = '比如：换了 3 号码表';
  note.value = r.reason_note || '';
  lab.setAttribute('for', 'reasonNote');
  note.id = 'reasonNote';
  dlg.appendChild(lab);
  dlg.appendChild(note);

  const acts = el('div', 'dlg__acts');
  const cancel = el('button', 'btn btn--quiet', '取消');
  cancel.type = 'button';
  cancel.addEventListener('click', () => scrim.remove());
  const save = el('button', 'btn btn--primary', '保存');
  save.type = 'button';
  save.addEventListener('click', async () => {
    if (!picked) { toast('先选一个原因', 'bad'); return; }
    save.disabled = true;
    try {
      await api('/api/recon/note', {
        shop_id: S.shopId, day: S.day, unit_id: r.unit_id,
        reason: picked, note: note.value.trim(),
      });
      r.reason = picked;
      r.reason_note = note.value.trim();
      scrim.remove();
      renderRecon();
      toast('已记录');
    } catch (e) {
      toast(String(e.message || e), 'bad');
      save.disabled = false;
    }
  });
  acts.appendChild(cancel);
  acts.appendChild(save);
  dlg.appendChild(acts);
  scrim.appendChild(dlg);
  document.body.appendChild(scrim);
  save.focus();
}

function exportRecon() {
  const d = S.recon;
  if (!d || !d.rows.length) { toast('没有数据可导', 'bad'); return; }
  const pach = S.kind === 'pachinko';
  const head = pach
    ? ['机台', '宝点投币', '码表投币', '差', '返珠率', '收益', '状态', '原因']
    : ['机台', '上币', '下币', '码表收币', '误差', '状态', '原因'];

  const cell = (v) => {
    if (v === null || v === undefined) return '';
    const s2 = String(v);
    return /[",\n]/.test(s2) ? '"' + s2.replace(/"/g, '""') + '"' : s2;
  };

  const lines = [head.join(',')];
  d.rows.forEach((r) => {
    const reason = [r.reason, r.reason_note].filter(Boolean).join('：');
    const row = pach
      ? [r.name, r.up, r.meter, r.diff,
         r.p && r.p.rate != null ? (r.p.rate * 100).toFixed(1) + '%' : '',
         r.p && r.p.profit != null ? r.p.profit : '',
         r.status, reason]
      : [r.name, r.up, r.down, r.meter, r.diff, r.status, reason];
    lines.push(row.map(cell).join(','));
  });

  const shop = (S.shops.find((x) => x.id === S.shopId) || {}).name || '门店';
  const blob = new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = '核对_' + shop + '_' + S.day + (pach ? '_弹珠机' : '_推币机') + '.csv';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  toast('已导出');
}

/* ------------------------------------------------------------ 全链 */
async function loadOverview() {
  try {
    const d = await api(`/api/overview?day=${S.day}`);
    $('ovLede').textContent = d.day + ' · ' + d.total.shops + ' 家门店';
    $('ovDiff').textContent = fmt1(d.total.diff_ok);
    $('ovDiff').dataset.tone = Math.abs(d.total.diff_ok) >= 1000 ? 'bad' : '';
    $('ovMeta').textContent = `${d.total.units} 个码表 · ${d.total.ok} 台可核对 · 误差超千 ${d.total.big} 台`;

    const body = $('ovBody');
    body.innerHTML = '';
    $('ovEmpty').hidden = d.rows.length > 0;
    d.rows.forEach((r) => {
      const tr = el('tr');
      tr.appendChild(el('td', 'name', r.shop));
      tr.appendChild(el('td', 'num', nf.format(r.units)));
      tr.appendChild(el('td', 'num', nf.format(r.ok)));
      const td = el('td', 'num lead', fmt1(r.diff_ok));
      if (Math.abs(r.diff_ok) >= 1000) td.dataset.tone = 'bad';
      tr.appendChild(td);
      tr.appendChild(el('td', 'num', fmt1(r.diff_all)));
      tr.appendChild(el('td', 'num', nf.format(r.big)));
      body.appendChild(tr);
    });
  } catch (e) {
    toast(String(e.message || e), 'bad');
  }
}

async function fetchAll() {
  const btn = $('btnFetchAll');
  btn.disabled = true;
  try {
    const r = await api('/api/fetch-all', { day: S.day });
    $('jobStatus').textContent = '开始拉 ' + r.task + ' 家门店';
    pollJob(() => { btn.disabled = false; loadOverview(); });
  } catch (e) {
    btn.disabled = false;
    toast(String(e.message || e), 'bad');
  }
}

function pollJob(done) {
  setTimeout(async () => {
    try {
      const j = await api('/api/job');
      $('jobStatus').textContent = `${j.done}/${j.total} ${j.msg || ''}${j.err ? ' · ' + j.err : ''}`;
      if (j.running) pollJob(done);
      else if (done) done();
    } catch (_) { pollJob(done); }
  }, 1500);
}

/* ------------------------------------------------------------ 设置 */
let MSHOP = null;
let MUNIT = null;
let MGROUP = null;

async function loadSettings() {
  // 门店身份：跳过门店列表，直接进自己的机台
  if (S.role === 'store') {
    const me = S.shops.find((x) => x.id === S.shopId);
    if (me) { await openShop(me); return; }
  }

  MSHOP = null;
  $('shopPane').hidden = false;
  $('unitPane').hidden = true;

  const d = await api('/api/manage/shops');
  const body = $('shopBody');
  body.innerHTML = '';
  d.rows.forEach((r) => {
    const tr = el('tr');
    tr.appendChild(el('td', 'name', r.name));
    tr.appendChild(el('td', 'num', nf.format(r.units)));
    const td = el('td');
    const b1 = el('button', 'btn btn--sm', '机台');
    b1.type = 'button';
    b1.addEventListener('click', () => openShop(r));
    td.appendChild(b1);
    const b2 = el('button', 'btn btn--sm btn--quiet', '删除');
    b2.type = 'button';
    b2.addEventListener('click', async () => {
      const ok = await UI.confirm({
        title: '删除门店',
        body: '「' + r.name + '」的机台和读数会一起删掉，不能恢复。',
        okText: '删除',
        danger: true,
      });
      if (!ok) return;
      try {
        await api('/api/manage/shop/remove', { id: r.id });
        toast('已删除');
        loadSettings();
      } catch (e) { toast(String(e.message || e), 'bad'); }
    });
    td.appendChild(b2);
    tr.appendChild(td);
    body.appendChild(tr);
  });
}

async function openShop(shop) {
  MSHOP = shop;
  MUNIT = null;
  MGROUP = null;
  EDIT_KEY = null;
  $('shopPane').hidden = true;
  $('unitPane').hidden = false;
  $('unitTitle').textContent = shop.name;
  $('btnBackShops').hidden = S.role === 'store';
  await renderUnits();
}

let EDIT_KEY = null;

async function renderUnits() {
  const d = await api('/api/manage/machines?shop_id=' + encodeURIComponent(MSHOP.id));
  const wrap = $('unitList');
  wrap.innerHTML = '';

  if (!d.rows.length) {
    const empty = el('div', 'empty');
    empty.appendChild(el('p', 'empty__t', '还没有机台'));
    empty.appendChild(el('p', 'empty__d', '下面新增一台'));
    wrap.appendChild(empty);
    return;
  }

  const groups = [
    { kind: 'pusher', name: '推币机' },
    { kind: 'pachinko', name: '弹珠机' },
  ];

  groups.forEach((grp) => {
    const rows = d.rows.filter((g) => (g.kind || 'pusher') === grp.kind);
    if (!rows.length) return;

    const head = el('h2', 'machine-group');
    head.appendChild(el('span', null, grp.name));
    head.appendChild(el('span', 'machine-group__n', rows.length + ' 台'));
    const addBtn = el('button', 'btn btn--sm btn--quiet machine-group__add', '+ 新增');
    addBtn.type = 'button';
    addBtn.setAttribute('aria-label', '新增一台' + grp.name);
    addBtn.addEventListener('click', () => {
      ADD_KIND = ADD_KIND === grp.kind ? null : grp.kind;
      renderUnits();
    });
    head.appendChild(addBtn);
    wrap.appendChild(head);

    if (ADD_KIND === grp.kind) wrap.appendChild(addMachineForm(grp.kind));
    if (grp.kind === 'pachinko') wrap.appendChild(pachinkoCfg());

    const grid = el('div', 'machines');
    rows.forEach((g) => {
      const card = el('article', 'machine');
      if (EDIT_KEY === g.key) renderMachineEdit(card, g);
      else renderMachineView(card, g);
      grid.appendChild(card);
    });
    wrap.appendChild(grid);
  });

  if (EDIT_KEY) {
    const f = wrap.querySelector('.machine__name-edit');
    if (f) { f.focus(); f.select(); }
  }
}

let ADD_KIND = null;

function addMachineForm(kind) {
  const box = el('div', 'add-form');

  const f1 = el('label', 'field');
  f1.appendChild(el('span', 'field__label', '机台名'));
  const name = el('input', 'input');
  name.type = 'text';
  name.autocomplete = 'off';
  name.placeholder = kind === 'pachinko' ? '塔兰弹珠机-017' : '凤凰城';
  f1.appendChild(name);
  box.appendChild(f1);

  const f2 = el('label', 'field');
  f2.appendChild(el('span', 'field__label', '通讯ID，多个用逗号隔开'));
  const comms = el('input', 'input');
  comms.type = 'text';
  comms.autocomplete = 'off';
  comms.placeholder = '733720,733895';
  f2.appendChild(comms);
  box.appendChild(f2);

  const acts = el('div', 'add-form__acts');
  const save = el('button', 'btn btn--sm btn--primary', '保存');
  save.type = 'button';
  const cancel = el('button', 'btn btn--sm btn--quiet', '取消');
  cancel.type = 'button';
  cancel.addEventListener('click', () => { ADD_KIND = null; renderUnits(); });
  acts.appendChild(save);
  acts.appendChild(cancel);
  box.appendChild(acts);

  save.addEventListener('click', async () => {
    const nm = name.value.trim();
    const cm = comms.value.trim();
    if (!nm) { toast('填机台名', 'bad'); name.focus(); return; }
    if (!cm) { toast('填通讯ID', 'bad'); comms.focus(); return; }
    save.disabled = true;
    try {
      await api('/api/manage/unit/save', {
        shop_id: MSHOP.id, name: nm, comms: cm, kind,
        shared: cm.split(',').filter(Boolean).length > 1,
      });
      toast('已新增');
      ADD_KIND = null;
      await renderUnits();
    } catch (e) {
      toast(String(e.message || e), 'bad');
      save.disabled = false;
    }
  });

  setTimeout(() => name.focus(), 0);
  return box;
}

function pachinkoCfg() {
  const row = el('div', 'machine-cfg');

  const mk = (label, key, placeholder) => {
    const f = el('label', 'machine-cfg__f');
    f.appendChild(el('span', 'machine-cfg__k', label));
    const inp = el('input', 'input machine-cfg__in');
    inp.type = 'text';
    inp.inputMode = 'decimal';
    inp.placeholder = placeholder;
    inp.value = MSHOP[key] === null || MSHOP[key] === undefined ? '' : MSHOP[key];
    f.appendChild(inp);
    row.appendChild(f);
    return inp;
  };

  const bead = mk('珠值', 'bead_value', '0.022');
  const card = mk('卡片价值', 'card_value', '0.01785');

  const btn = el('button', 'btn btn--sm btn--primary', '保存参数');
  btn.type = 'button';
  btn.addEventListener('click', async () => {
    const b = parseFloat(bead.value);
    const c = parseFloat(card.value);
    if (!isFinite(b) || !isFinite(c)) { toast('两个值都要填数字', 'bad'); return; }
    btn.disabled = true;
    try {
      await api('/api/manage/shop/values', {
        shop_id: MSHOP.id, bead_value: b, card_value: c,
      });
      MSHOP.bead_value = b;
      MSHOP.card_value = c;
      toast('参数已保存');
    } catch (e) {
      toast(String(e.message || e), 'bad');
    } finally {
      btn.disabled = false;
    }
  });
  row.appendChild(btn);

  return row;
}

function renderMachineView(card, g) {
  card.setAttribute('aria-labelledby', 'm-' + slug(g.key));

  const head = el('header', 'machine__head');
  const h = el('h3', 'machine__name', g.key);
  h.id = 'm-' + slug(g.key);
  head.appendChild(h);
  if (g.kind === 'pachinko') head.appendChild(el('span', 'machine__tag', '弹珠'));
  head.appendChild(el('span', 'machine__meta',
    g.merged ? g.heads + ' 个卡头 · 共用码表' : g.heads + ' 个卡头'));
  card.appendChild(head);

  const ul = el('ul', 'machine__comms');
  ul.setAttribute('aria-label', g.key + ' 的卡头');
  g.comms.forEach((c) => ul.appendChild(el('li', 'chip', c)));
  card.appendChild(ul);

  const foot = el('footer', 'machine__foot');

  const edit = el('button', 'btn btn--sm', '改');
  edit.type = 'button';
  edit.setAttribute('aria-label', '改机台 ' + g.key);
  edit.addEventListener('click', () => { EDIT_KEY = g.key; renderUnits(); });
  foot.appendChild(edit);

  if (!g.merged && g.heads > 1) {
    const b = el('button', 'btn btn--sm btn--quiet', '合并成一台');
    b.type = 'button';
    b.setAttribute('aria-label', '把 ' + g.key + ' 的 ' + g.heads + ' 个卡头合并成一台');
    b.addEventListener('click', () => mergeMachine(g));
    foot.appendChild(b);
  }

  if (g.merged) {
    const sp = el('button', 'btn btn--sm btn--quiet', '拆分');
    sp.type = 'button';
    sp.setAttribute('aria-label', '把 ' + g.key + ' 拆回每个卡头一个码表');
    sp.addEventListener('click', async () => {
      const ok = await UI.confirm({
        title: '拆分这台机',
        body: '「' + g.key + '」会拆回 ' + g.comms.length + ' 个卡头，每个卡头各自一个码表。',
        okText: '拆分',
      });
      if (!ok) return;
      try {
        await api('/api/manage/unit/split', { id: g.units[0].id });
        toast('已拆分');
        renderUnits();
      } catch (e) { toast(String(e.message || e), 'bad'); }
    });
    foot.appendChild(sp);
  }

  const del = el('button', 'btn btn--sm btn--quiet', '删除');
  del.type = 'button';
  del.setAttribute('aria-label', '删除机台 ' + g.key);
  del.addEventListener('click', async () => {
    const ok = await UI.confirm({
      title: '删除机台',
      body: '「' + g.key + '」和它的 ' + g.comms.length + ' 个卡头会一起移除。',
      okText: '删除',
      danger: true,
    });
    if (!ok) return;
    try {
      for (const u of g.units) await api('/api/manage/unit/remove', { id: u.id });
      toast('已删除');
      renderUnits();
    } catch (e) { toast(String(e.message || e), 'bad'); }
  });
  foot.appendChild(del);

  card.appendChild(foot);
}

function slug(t) {
  return String(t).replace(/[^\w\u4e00-\u9fa5]+/g, '-');
}

function renderMachineEdit(card, g) {
  card.dataset.editing = 'true';
  card.setAttribute('aria-label', '编辑机台 ' + g.key);

  const head = el('header', 'machine__head');
  const nameIn = el('input', 'machine__name-edit');
  nameIn.type = 'text';
  nameIn.autocomplete = 'off';
  nameIn.value = g.key;
  nameIn.setAttribute('aria-label', '机台名');
  head.appendChild(nameIn);
  head.appendChild(el('span', 'machine__meta', g.heads + ' 个卡头'));
  card.appendChild(head);

  const ul = el('ul', 'machine__comms');

  const XSvg = '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 3l6 6M9 3l-6 6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';

  function addChip(val) {
    const li = el('li', 'chip chip--edit');
    const inp = el('input', 'chip__in');
    inp.type = 'text';
    inp.inputMode = 'numeric';
    inp.autocomplete = 'off';
    inp.value = val || '';
    inp.size = Math.max(4, (val || '').length);
    inp.setAttribute('aria-label', '通讯ID');
    inp.addEventListener('input', () => { inp.size = Math.max(4, inp.value.length); });
    inp.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); saveBtn.click(); }
    });
    li.appendChild(inp);

    const x = el('button', 'chip__x');
    x.type = 'button';
    x.setAttribute('aria-label', '删除这个卡头');
    x.innerHTML = XSvg;
    x.addEventListener('click', () => {
      li.remove();
      refreshMeta();
      const first = ul.querySelector('.chip__in');
      if (first) first.focus();
    });
    li.appendChild(x);
    ul.appendChild(li);
    return inp;
  }

  g.comms.forEach(addChip);
  card.appendChild(ul);

  const addLi = el('li', 'chip chip--add');
  const addBtn = el('button', 'chip__add');
  addBtn.type = 'button';
  addBtn.setAttribute('aria-label', '加一个卡头');
  addBtn.innerHTML = '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 2.5v7M2.5 6h7" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';
  addBtn.addEventListener('click', () => {
    const inp = addChip('');
    refreshMeta();
    inp.focus();
  });
  addLi.appendChild(addBtn);
  ul.appendChild(addLi);

  function refreshMeta() {
    const n = ul.querySelectorAll('.chip__in').length;
    head.querySelector('.machine__meta').textContent = n + ' 个卡头';
    $('machineLive').textContent = '现在 ' + n + ' 个卡头';
  }

  const foot = el('footer', 'machine__foot');

  const saveBtn = el('button', 'btn btn--sm btn--primary', '保存');
  saveBtn.type = 'button';
  saveBtn.addEventListener('click', async () => {
    const nm = nameIn.value.trim();
    const cm = [...ul.querySelectorAll('.chip__in')]
      .map((i) => i.value.trim()).filter(Boolean);
    if (!nm) { toast('填机台名', 'bad'); nameIn.focus(); return; }
    if (!cm.length) { toast('至少留一个通讯ID', 'bad'); return; }
    saveBtn.disabled = true;
    saveBtn.dataset.loading = 'true';
    card.dataset.saving = 'true';
    try {
      if (g.units.length > 1) {
        await api('/api/manage/machine/save', {
          shop_id: MSHOP.id, ids: g.units.map((u) => u.id),
          name: nm, comms: cm.join(','), shared: cm.length > 1,
        });
      } else {
        await api('/api/manage/unit/save', {
          id: g.units[0].id, shop_id: MSHOP.id,
          name: nm, comms: cm.join(','), shared: cm.length > 1,
        });
      }
      toast('已保存');
      EDIT_KEY = null;
      await renderUnits();
    } catch (e) {
      toast(String(e.message || e), 'bad');
    } finally {
      saveBtn.disabled = false;
      saveBtn.dataset.loading = 'false';
      card.dataset.saving = 'false';
    }
  });
  foot.appendChild(saveBtn);

  const cancel = el('button', 'btn btn--sm btn--quiet', '取消');
  cancel.type = 'button';
  cancel.addEventListener('click', () => { EDIT_KEY = null; renderUnits(); });
  foot.appendChild(cancel);

  card.appendChild(foot);

  card.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { EDIT_KEY = null; renderUnits(); }
  });
  nameIn.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); saveBtn.click(); }
  });
}

async function mergeMachine(g) {
  const ok = await UI.confirm({
    title: '合并成一台',
    body: '「' + g.key + '」的 ' + g.comms.length + ' 个卡头合成一台，共用一个码表，以后只填一个读数。',
    okText: '合并',
  });
  if (!ok) return;
  try {
    const r = await api('/api/manage/unit/merge', {
      shop_id: MSHOP.id,
      ids: g.units.map((u) => u.id),
    });
    toast('已合并：' + r.name + '（' + r.comms + ' 个卡头）');
    await renderUnits();
  } catch (e) {
    toast(String(e.message || e), 'bad');
  }
}

async function importShops() {
  const btn = $('btnImportShops');
  btn.disabled = true;
  $('importStatus').textContent = '导入门店';
  try {
    const r = await api('/api/shops/sync', {});
    $('importStatus').textContent = `已导入 ${r.shops} 家，正在拉机台（${r.task} 家）`;
    pollJob(async () => {
      $('importStatus').textContent = '导入完成，可以逐店检查机台';
      btn.disabled = false;

  const b = await api('/api/bootstrap');
      S.shops = b.shops;
      S.units = b.units;
      loadSettings();
    });
  } catch (e) {
    btn.disabled = false;
    $('importStatus').textContent = '';
    toast(String(e.message || e), 'bad');
  }
}

/* ------------------------------------------------------------ 启动 */
let shopPicker = null;
let dayPicker = null;

function showGate(msg) {
  $('gate').hidden = false;
  document.querySelector('.app').hidden = true;
  if (msg) {
    $('loginErr').textContent = msg;
    $('loginErr').hidden = false;
  }
}

async function boot() {
  let me = null;
  try {
    me = await api('/api/me');
  } catch (e) {
    localStorage.removeItem(TOKEN_KEY);
    showGate('');
    return;
  }
  S.me = me;
  S.role = me.type === 'store' ? 'store' : 'hq';
  $('gate').hidden = true;
  document.querySelector('.app').hidden = false;

  if (me.type === 'store' && !me.bound) {
    showGate('任务平台里你的门店是「' + (me.cwsj_name || '?') + '」，还没和码表系统对上，请联系总部');
    return;
  }

  const scanCode = new URLSearchParams(location.search).get('c');
  if (scanCode) {
    document.body.dataset.scan = '1';
    await loadScan(scanCode);
    return;
  }
  const scanId = new URLSearchParams(location.search).get('u');
  if (scanId) {
    document.body.dataset.scan = '1';
    await loadScan(null, scanId);
    return;
  }

  const b = await api('/api/bootstrap');
  S.shopId = b.shop_id;
  S.shops = b.shops;
  S.units = b.units;
  S.bdOk = b.baodian_ok;
  S.day = b.edit_day || b.today;
  S.editDay = b.edit_day || b.today;

  const conn = $('conn');
  conn.dataset.state = S.bdOk ? 'ok' : 'bad';
  $('connText').textContent = S.bdOk ? '宝点已连接' : '宝点未连接';

  const shop = S.shops.find((s) => s.id === S.shopId);
  shopPicker = UI.picker({
    btn: $('shopBtn'),
    pop: $('shopPop'),
    valueEl: $('shopVal'),
    getItems: () => {
      if (S.role === 'store') {
        const me = S.shops.find((x) => x.id === S.shopId);
        return me ? [{ label: me.name, value: me.id, selected: true }] : [];
      }
      return S.shops.map((s) => ({
        label: s.name,
        value: s.id,
        group: s.group_name || '未分组',
        selected: s.id === S.shopId,
      }));
    },
    onPick: async (it) => {
      S.shopId = it.value;
      shopPicker.set(it.label);
      await api('/api/shops', { name: it.label }).catch(() => {});
      const bb = await api('/api/bootstrap');
      S.units = bb.units;
      await loadEntry();
      if (S.view === 'recon') loadRecon();
      if (S.view === 'overview') loadOverview();
    },
  });
  shopPicker.set(shop ? shop.name : '未选门店');

  dayPicker = UI.datePicker({
    btn: $('dayBtn'),
    pop: $('dayPop'),
    valueEl: $('dayVal'),
    value: S.day,
    max: S.editDay,
    onChange: async (v) => {
      S.day = v;
      if (S.view === 'entry') await loadEntry();
      else if (S.view === 'recon') await loadRecon();
      else if (S.view === 'overview') await loadOverview();
    },
  });

  applyRole();
  if (!b.units_confirmed) welcomeDialog(b);

  if (S.pendingUnit) {
    setView('entry');
    await loadEntry();
    focusUnit(S.pendingUnit);
    S.pendingUnit = null;
  } else {
    setView('home');
    await loadEntry();
  }
}

function applyRole() {
  const store = S.role === 'store';
  $('tools').hidden = false;
  const navOv = document.querySelector('.nav__item[data-view="overview"]');
  if (navOv) navOv.hidden = store;
  $('shopBtn').disabled = store;
  $('shopBtn').setAttribute('aria-label', store ? '当前门店' : '选择门店');
  if (store && S.view === 'overview') setView('entry');
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.nav__item').forEach((b) => {
    b.addEventListener('click', () => setView(b.dataset.view));
  });
  $('btnSave').addEventListener('click', saveAndFetch);
  $('btnToRecon').addEventListener('click', () => setView('recon'));
  $('btnExport').addEventListener('click', exportRecon);
  $('btnConfirm').addEventListener('click', confirmUnits);
  $('btnFetchAll').addEventListener('click', fetchAll);
  $('btnImportShops').addEventListener('click', importShops);
  $('btnBackShops').addEventListener('click', loadSettings);
  $('btnScanBack').addEventListener('click', () => { location.href = BASE; });
  $('btnScan').addEventListener('click', openScanner);
  $('scanClose').addEventListener('click', stopScanner);
  $('scanFile').addEventListener('change', (e) => {
    const f = e.target.files && e.target.files[0];
    e.target.value = '';
    scanPhoto(f);
  });
  $('btnQr').addEventListener('click', () => {
    window.open(BASE + 'qr?shop_id=' + encodeURIComponent(MSHOP.id), '_blank');
  });
  $('loginForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const account = $('loginAccount').value.trim();
    const pwd = $('loginPwd').value;
    if (!account || !pwd) {
      $('loginErr').textContent = '账号和密码都要填';
      $('loginErr').hidden = false;
      return;
    }
    const btn = $('btnLogin');
    btn.disabled = true;
    $('loginErr').hidden = true;
    try {
      const r = await api('/api/login', { account, pwd });
      localStorage.removeItem('meter_logout');
      localStorage.setItem(TOKEN_KEY, r.token);
      location.reload();
    } catch (err) {
      $('loginErr').textContent = String(err.message || err);
      $('loginErr').hidden = false;
      btn.disabled = false;
    }
  });

  $('btnLogout').addEventListener('click', () => {
    localStorage.setItem('meter_logout', '1');
    localStorage.removeItem(TOKEN_KEY);
    location.reload();
  });

  boot().catch((e) => toast(String(e.message || e), 'bad'));
});
