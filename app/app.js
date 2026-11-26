'use strict';

const S = {
  role: 'store', me: null, kind: 'pusher', pendingUnit: null, scan: null,
  brand: null, brandFlags: null, pachFields: ['in_r', 'p_tokens', 'p_out', 'p_cards'],
  meterUnit: 'coin',
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
  // 点退出时已经把 token 清掉了，这里只认 token 本身
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

/* ------------------------------------------------------------ 历史栈
   视图 = 一条历史；面板只登记，不占历史（返回先关面板再退视图）。 */
const VIEWS = ['home', 'entry', 'recon', 'overview', 'settings', 'scan'];
const HIST = { promptTs: 0, ready: false };
const DLGS = [];                      // 打开着的面板，后进先出

function trackDlg(close) {
  if (DLGS.indexOf(close) < 0) DLGS.push(close);
}

function untrackDlg(close) {
  const i = DLGS.indexOf(close);
  if (i >= 0) DLGS.splice(i, 1);
}

function viewFromHash() {
  const h = (location.hash || '').replace(/^#/, '');
  return VIEWS.indexOf(h) >= 0 ? h : null;
}

function histUrl(v) {
  return location.pathname + '#' + v;
}

function pushView(v, replace) {
  if (!HIST.ready) return;
  const st = history.state || {};
  if (replace || st.view === v) {
    history.replaceState({ view: v, nav: true }, '', histUrl(v));
    return;
  }
  history.pushState({ view: v, nav: true }, '', histUrl(v));
}

// 开局：栈底放一条「页面自身」，上面再压一条挡板，第一次返回才有得退
function initHistory(startView) {
  histRestore(startView);
  history.pushState({ hold: true, view: startView }, '', histUrl(startView));
  HIST.ready = true;
}

// 不带参数的历史操作，避免 pushView 又改地址
function histRestore(v) {
  history.replaceState({ view: v }, '', histUrl(v));
}

window.addEventListener('popstate', (e) => {
  const st = e.state || {};
  // 1) 有面板开着 → 先关面板，位置补回来，用户不用多按一次
  if (DLGS.length) {
    const close = DLGS.pop();
    try { close(); } catch (err) { /* 面板已卸载就算了 */ }
    history.pushState({ view: S.view, nav: true }, '', histUrl(S.view));
    return;
  }
  // 2) 还停在本应用的某一条视图记录上
  if (st.nav && st.view && VIEWS.indexOf(st.view) >= 0) {
    setView(st.view, { silent: true });
    return;
  }
  if (st.hold && st.view) {
    setView(st.view, { silent: true });
    return;
  }
  // 3) 栈底：第一次提示，两秒内再按才真退出
  const now = Date.now();
  if (now - HIST.promptTs < 2000) {
    history.back();
    return;
  }
  HIST.promptTs = now;
  toast('再按一次退出');
  history.pushState({ hold: true, view: S.view }, '', histUrl(S.view));
});

/* ------------------------------------------------------------ 视图 */
function syncDayLock() {
  const wrap = $('dayPick');
  if (!wrap) return;
  const store = S.role === 'store';
  // 门店：日期只在核对页出现（填写页固定昨天，不需要选）
  // 总部：填写 / 核对 / 全链都能切日期
  const show = store ? ['entry', 'recon'].indexOf(S.view) >= 0
    : ['entry', 'recon', 'overview'].indexOf(S.view) >= 0;
  wrap.hidden = !show;
}

/* 手机（竖屏/窄屏）：不提供设置页，页头也不放门店选择器 */
const PHONE_MQ = window.matchMedia('(max-width: 47.99em)');
function isPhone() { return PHONE_MQ.matches; }

/* 取数方式的中文名：客服用什么词，界面就用什么词 */
function provLabel() {
  return ({ baodian: '宝点', shiruan: '世软', jingjian: '鲸舰', manual: '纯手工' })[S.provider || 'baodian']
    || '宝点';
}

function setView(v, opts) {
  opts = opts || {};
  // 手机上不提供设置页：机台/门店维护只在电脑上做
  if (v === 'settings' && isPhone()) v = 'home';
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
  if (!opts.silent) pushView(v, opts.replace);
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
      const flds0 = r.fields || S.pachFields || [];
      const has0 = (k) => flds0.indexOf(k) >= 0;
      if (has0('in_r')) inputs.appendChild(makeCell(r, 'in_r', r.unit === 'bead' ? '投珠数' : '投币数', 'prev_in', noop));
      if (has0('p_tokens')) inputs.appendChild(makeCell(r, 'p_tokens', '投珠数', 'prev_tokens', noop));
      if (has0('p_out')) inputs.appendChild(makeCell(r, 'p_out', '出珠数', 'prev_out_total', noop));
      if (has0('p_cards')) inputs.appendChild(makeCell(r, 'p_cards', '出卡', 'prev_cards', noop));
    } else {
      inputs.appendChild(makeCell(r, 'in_r', '入', 'prev_in', noop));
      inputs.appendChild(makeCell(r, 'out_r', '出', 'prev_out', noop));
    }
    card.appendChild(inputs);
    card.appendChild(photoBox(r, d.shop.id, d.day, d.shop.name));
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
  refreshBdState();
  const d = await api(`/api/entry?shop_id=${encodeURIComponent(S.shopId)}&day=${S.day}&kind=${S.kind}`);
  S.allRows = groupByName(d.rows.map((r) => Object.assign({}, r, { comm_count: r.comms.length })),
    S.kind === 'pachinko');
  S.confirmed = !!d.confirmed;
  S.entry = S.allRows.filter((r) => r.active !== false);

  $('confirmPanel').hidden = true;
  $('entryList').hidden = false;

  if (!S.allRows.length) {
    renderEntry();
    $('entryLede').textContent = isPhone()
      ? '这家店还没有机台，让总部在电脑上把机台加进来'
      : '这家店没有参与核对的机台，去设置里加';
    return;
  }
  const bits = [S.kind === 'pachinko' ? '弹珠机' : '推币机', S.entry.length + ' 台'];
  if (S.kind === 'pachinko') {
    const ns = [...new Set(S.entry.map((r) => (r.fields || []).length))].filter((x) => x > 0);
    if (ns.length === 1) bits.push('每台填 ' + ns[0] + ' 个数');
    else if (ns.length > 1) bits.push('每台填 ' + Math.min(...ns) + '~' + Math.max(...ns) + ' 个数');
  } else {
    bits.push('每台填 2 个累计读数');
  }
  if (S.role === 'store') {
    bits.push(window.matchMedia('(max-width: 47.99em)').matches ? S.day.slice(5) : S.day);
  }
  $('entryLede').textContent = bits.join(' · ');
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
  // 空壳（没卡头也没读数）是同步/拆分留下的旧行，不占确认清单
  const rows = S.allRows.filter((r) => (r.comms || []).length
    || r.in_r !== null || r.out_r !== null);
  rows.forEach((r) => {
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
    cb.appendChild(el('span', 'sheet__name',
      (S.kind === 'pachinko') ? r.name : (baseName(r.name) || r.name)));
    li.appendChild(cb);

    const meta = [];
    const pc = (r.pcomms || []).filter((x) => x && x.comm);
    if (pc.length) meta.push(pc.map((x) => x.p || '未标').join(' / '));
    if (r.shared) meta.push('共用码表');
    if (!r.has_card && (S.provider || 'baodian') !== 'manual') meta.push(provLabel() + '无数据');
    li.appendChild(el('span', 'sheet__meta', meta.join(' · ')));
    ul.appendChild(li);
  });
  $('confirmHint').textContent = '共 ' + rows.length + ' 台，勾选的要填';
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

function baseName(n) {
  // 「冰淇淋嘉年华 (2)」「可乐风暴 733586」都归到同一台机
  return String(n || '').replace(/\s*\(\d+\)\s*$/, '').replace(/\s+\d{5,}$/, '').trim();
}

/* 同名/同品牌的排在一起：推币机按机台名，弹珠机按品牌；组内按 P 位 */
function groupByName(rows, isPach) {
  const key = (r) => (isPach ? (r.brand || '未分品牌') : (baseName(r.name) || r.name));
  const order = new Map();
  rows.forEach((r) => {
    const b = key(r);
    if (!order.has(b)) order.set(b, order.size);
  });
  const firstP = (r) => ((((r.pcomms || [])[0] || {}).p) || (r.poss || [])[0] || '');
  rows.sort((a, b) => (order.get(key(a)) - order.get(key(b)))
    || String(firstP(a)).localeCompare(String(firstP(b)))
    || String(a.name).localeCompare(String(b.name)));
  return rows;
}

function renderEntry() {
  const ul = $('entryList');
  ul.innerHTML = '';

  // 推币机：一台机（几个卡头）归一组，组头写机台名，卡上只写 P 位 + 通讯ID
  // 弹珠机：按品牌分组，卡上写整台机名 + P 位：通讯ID
  const isPach = S.kind === 'pachinko';
  const groupKey = (r) => (isPach ? (r.brand || '未分品牌') : (baseName(r.name) || r.name));
  const counts = {};
  S.entry.forEach((r) => {
    const b = groupKey(r);
    counts[b] = (counts[b] || 0) + 1;
  });
  let lastBase = null;

  S.entry.forEach((r) => {
    const base = groupKey(r);
    if (base !== lastBase) {
      const gh = el('li', 'entry-group');
      gh.appendChild(el('span', 'entry-group__name', base));
      const n = counts[base];
      if (isPach) gh.appendChild(el('span', 'entry-group__n', n + ' 台'));
      else if (n > 1) gh.appendChild(el('span', 'entry-group__n', n + ' 个卡头'));
      ul.appendChild(gh);
      lastBase = base;
    }

    const li = el('li', 'lrow');
    li.dataset.unit = String(r.unit_id);

    const id = el('div', 'lrow__id');
    const pc = (r.pcomms || []).filter((x) => x && x.comm);
    const nm = /^(.*?)\s+(\d{5,})$/.exec(r.name || '');
    // 两种机台都带机台名：凤凰城001 P1：671567 / 塔兰弹珠机-001 P1：817879
    // 拆分出来的「(2)(3)」不显示——同组里靠 P 位区分
    id.appendChild(el('span', 'lrow__name', baseName(nm ? nm[1] : r.name) || r.name));
    if (pc.length) {
      pc.forEach((x) => id.appendChild(el('span', 'lrow__comm', (x.p || '未标') + '：' + x.comm)));
    } else if (nm) {
      id.appendChild(el('span', 'lrow__comm', nm[2]));
    }
    if (r.shared) id.appendChild(el('span', 'lrow__meta', '共用码表'));
    else if (pc.length > 1) id.appendChild(el('span', 'lrow__meta', pc.length + ' 个卡头'));
    if (r.prev_in !== null && r.prev_in !== undefined) {
      const pv = (r.kind === 'pachinko')
        ? '昨日 投币 ' + fmt(r.prev_in)
        : '昨日 入 ' + fmt(r.prev_in) + ' · 出 ' + fmt(r.prev_out);
      id.appendChild(el('span', 'lrow__prev', pv));
    }
    li.appendChild(id);

    const inputs = el('div', 'lrow__inputs');
    if (r.kind === 'pachinko') {
      const flds = r.fields || S.pachFields || [];
      const has = (k) => flds.indexOf(k) >= 0;
      inputs.dataset.n = String(flds.length);
      if (has('in_r')) inputs.appendChild(makeCell(r, 'in_r', r.unit === 'bead' ? '投珠数' : '投币数', 'prev_in'));
      if (has('p_tokens')) inputs.appendChild(makeCell(r, 'p_tokens', '投珠数', 'prev_tokens'));
      if (has('p_out')) inputs.appendChild(makeCell(r, 'p_out', '出珠数', 'prev_out_total'));
      if (has('p_cards')) inputs.appendChild(makeCell(r, 'p_cards', '出卡', 'prev_cards'));
    } else {
      inputs.appendChild(makeCell(r, 'in_r', '入', 'prev_in'));
      inputs.appendChild(makeCell(r, 'out_r', '出', 'prev_out'));
    }
    li.appendChild(inputs);
    li.appendChild(photoBox(r, S.shopId, S.day, shopName()));

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
  const noPic = S.entry.filter((r) => r.active !== false
    && !(r.photos && r.photos.some((p) => !p.expired))).length;
  const allFilled = filled === total && total > 0;
  const canSubmit = allFilled && noPic === 0;
  const sub = $('btnSubmit');
  if (sub) sub.disabled = !canSubmit;
  $('saveHint').textContent = filled === 0
    ? '先填至少一台'
    : (!allFilled
      ? '还剩 ' + (total - filled) + ' 台没填'
      : (noPic
        ? '还差 ' + noPic + ' 台没拍照'
        : '都填完了，可以提交'));
  $('btnToRecon').hidden = !canSubmit;
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

/* ------------------------------------------------------------ 拍照凭证 */
const PHOTO_MAX_SIDE = 1400;
const PHOTO_QUALITY = 0.72;

function hasReading(r) {
  return r.in_r !== null || r.out_r !== null || r.p_tokens !== null
    || r.p_out !== null || r.p_cards !== null;
}

function compressImage(file, stamp) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      const scale = Math.min(1, PHOTO_MAX_SIDE / Math.max(img.width, img.height));
      const cv = document.createElement('canvas');
      cv.width = Math.round(img.width * scale);
      cv.height = Math.round(img.height * scale);
      const g = cv.getContext('2d');
      g.drawImage(img, 0, 0, cv.width, cv.height);
      if (stamp) drawStamp(g, cv.width, cv.height, stamp);
      cv.toBlob((b) => (b
        ? resolve({ blob: b, w: cv.width, h: cv.height })
        : reject(new Error('压缩失败，重拍一张'))), 'image/jpeg', PHOTO_QUALITY);
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error('这张图读不了，重拍一张')); };
    img.src = url;
  });
}

// 左下角水印：上行「门店 · 机台 · P位」，下行日期时间
function drawStamp(g, w, h, stamp) {
  const big = Math.round(w * 0.045);
  const small = Math.round(w * 0.032);
  const pad = Math.round(w * 0.03);
  g.textBaseline = 'alphabetic';
  g.lineWidth = Math.max(2, Math.round(big * 0.12));
  g.strokeStyle = 'rgba(0,0,0,.55)';
  g.fillStyle = '#fff';
  let y = h - pad - small * 1.4;
  g.font = '600 ' + big + 'px -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif';
  g.strokeText(stamp.title, pad, y);
  g.fillText(stamp.title, pad, y);
  y += Math.round(small * 1.6);
  g.font = '400 ' + small + 'px -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif';
  g.strokeText(stamp.time, pad, y);
  g.fillText(stamp.time, pad, y);
}

function stampNow(row, poi) {
  const d = new Date();
  const p2 = (n) => String(n).padStart(2, '0');
  const when = d.getFullYear() + '-' + p2(d.getMonth() + 1) + '-' + p2(d.getDate())
    + ' ' + p2(d.getHours()) + ':' + p2(d.getMinutes());
  const nm = String(row.name || '').replace(/\s+\d{5,}$/, '');
  const bits = [poi, nm];
  if (row.pos) bits.push('P' + row.pos);
  return { title: bits.filter(Boolean).join(' · '), time: when };
}

function blobToBase64(blob) {
  return new Promise((resolve, reject) => {
    const fr = new FileReader();
    fr.onload = () => resolve(String(fr.result).split(',')[1] || '');
    fr.onerror = () => reject(new Error('读不到照片'));
    fr.readAsDataURL(blob);
  });
}

// 照片带登录态取回来（<img src> 带不了 Authorization）
async function loadPhoto(id) {
  S.photoUrls = S.photoUrls || {};
  if (S.photoUrls[id]) return S.photoUrls[id];
  const tok = getToken();
  const r = await fetch(BASE + 'api/photo?id=' + encodeURIComponent(id),
    { headers: tok ? { Authorization: 'Bearer ' + tok } : {} });
  if (!r.ok) return null;
  const blob = await r.blob();
  S.photoBlobs = S.photoBlobs || {};
  S.photoBlobs[id] = blob;
  const url = URL.createObjectURL(blob);
  S.photoUrls[id] = url;
  return url;
}

async function uploadPhoto(shopId, day, row, file, poi) {
  const { blob, w, h } = await compressImage(file, stampNow(row, poi));
  const data = await blobToBase64(blob);
  const d = await api('/api/photo', {
    shop_id: shopId, day, unit_id: row.unit_id, data, w, h,
  });
  row.photos = row.photos || [];
  row.photos.push({ id: d.id, at: d.at });
  S.photoUrls = S.photoUrls || {};
  S.photoUrls[d.id] = URL.createObjectURL(blob);
  return d;
}

// 每台机：缩略图（点开看大图）+ 拍照按钮；一块码表一天可以拍多张
function photoBox(row, shopId, day, poi) {
  const box = el('div', 'pic');
  row.photos = row.photos || [];

  const thumb = el('button', 'pic__thumb');
  thumb.type = 'button';
  const img = el('img', 'pic__img');
  img.alt = row.name + ' 的码表照片';
  img.hidden = true;
  const badge = el('span', 'pic__badge');

  const CAM_SVG = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"'
    + ' stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">'
    + '<path d="M3 8.5h3.2L8 6h8l1.8 2.5H21V19H3z"/><circle cx="12" cy="13.2" r="3.4"/></svg>';
  const btn = el('button', 'btn btn--quiet pic__btn');
  btn.type = 'button';
  const ico = el('span', 'pic__btn-ico');
  ico.innerHTML = CAM_SVG;
  const lbl = el('span', 'pic__btn-t', row.photos.length ? '加一张' : '拍照');
  btn.appendChild(ico);
  btn.appendChild(lbl);
  const setLabel = (t) => { lbl.textContent = t; };

  const file = document.createElement('input');
  file.type = 'file';
  file.accept = 'image/*';
  file.capture = 'environment';     // 手机上只让拍，不从相册挑
  file.hidden = true;

  const paint = () => {
    const live = row.photos.filter((p) => !p.expired);
    const last = live[live.length - 1];
    if (!last) {
      img.hidden = true;
      badge.hidden = true;
      setLabel('拍照');
      return;
    }
    thumb.dataset.has = 'true';
    setLabel('加一张');
    badge.hidden = live.length < 2;
    badge.textContent = String(live.length);
    loadPhoto(last.id).then((u) => { if (u) { img.src = u; img.hidden = false; } }).catch(() => {});
  };
  paint();

  thumb.addEventListener('click', () => {
    const live = row.photos.filter((p) => !p.expired);
    if (!live.length) { file.click(); return; }
    openViewer(live, 0, row.name);   // 从第一张开始看
  });
  thumb.appendChild(img);
  thumb.appendChild(badge);

  btn.addEventListener('click', () => file.click());
  file.addEventListener('change', async () => {
    const f = file.files && file.files[0];
    file.value = '';
    if (!f) return;
    btn.disabled = true;
    const old = lbl.textContent;
    setLabel('上传中');
    try {
      await uploadPhoto(shopId, day, row, f, poi);
      paint();
      if (typeof updateProgress === 'function') updateProgress();
      toast('照片已存');
    } catch (e) {
      toast(String(e.message || e), 'bad');
      setLabel(old);
    } finally {
      btn.disabled = false;
    }
  });

  box.appendChild(thumb);
  box.appendChild(btn);
  box.appendChild(file);
  return box;
}

/* ------------------------------------------------------------ 看大图 */
let _viewer = null;

function openViewer(list, start, title) {
  const items = (list || []).filter((x) => x && !x.expired);
  if (!items.length) { toast('照片已过期'); return; }
  if (_viewer) { _viewer.remove(); _viewer = null; }
  let idx = Math.min(Math.max(0, start | 0), items.length - 1);

  const root = el('div', 'vw');
  root.setAttribute('role', 'dialog');
  root.setAttribute('aria-modal', 'true');
  const box = el('div', 'vw__box');
  const stage = el('div', 'vw__stage');
  const img = el('img', 'vw__img');
  const counter = el('div', 'vw__count');
  const prev = el('button', 'vw__nav vw__nav--prev', '‹');
  const next = el('button', 'vw__nav vw__nav--next', '›');
  const close = el('button', 'vw__close', '×');
  close.setAttribute('aria-label', '关闭');
  const dl = el('button', 'vw__dl', '保存到相册');
  dl.type = 'button';

  prev.type = 'button'; next.type = 'button';

  // iOS Safari 不认 <a download>，会变成「下载到文件」；有分享能力就走系统分享面板
  // （里面才有「存储图像」＝存进相册），没有才退回下载。
  const saveCurrent = async () => {
    const it = items[idx];
    const base = (title || 'meter') + '_' + String(it.at || '').replace(/[^\d]/g, '').slice(0, 12);
    const name = base + '.jpg';
    // 大图已经加载过，blob 就在缓存里：先走同步分享，iOS 才不会把手势判失效
    let blob = S.photoBlobs && S.photoBlobs[it.id];
    const tryShare = () => {
      if (!blob || !navigator.canShare) return false;
      const file = new File([blob], name, { type: 'image/jpeg' });
      if (!navigator.canShare({ files: [file] })) return false;
      navigator.share({ files: [file], title: name })
        .then(() => {
          toast('已保存到相册');
          dl.textContent = '已保存 ✓';
          setTimeout(() => { dl.textContent = '保存到相册'; }, 2000);
        })
        .catch((e) => {
          if (e && e.name === 'AbortError') return;      // 用户自己取消了
          toast('没存上，长按图片也能存', 'bad');
        });
      return true;
    };
    if (tryShare()) return;
    const u = await loadPhoto(it.id).catch(() => null);
    blob = S.photoBlobs && S.photoBlobs[it.id];
    if (tryShare()) return;
    if (!u) { toast('照片没取到'); return; }
    const a = document.createElement('a');
    a.href = u;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    toast('浏览器不支持直接存相册，已下载到文件；长按图片也能存');
  };
  dl.addEventListener('click', (e) => { e.stopPropagation(); saveCurrent(); });

  const show = async (n) => {
    idx = n;
    counter.textContent = (n + 1) + ' / ' + items.length;
    prev.disabled = n <= 0;
    next.disabled = n >= items.length - 1;
    img.removeAttribute('src');
    stage.dataset.loading = 'true';
    const u = await loadPhoto(items[n].id).catch(() => null);
    stage.dataset.loading = 'false';
    if (u) img.src = u;
  };
  prev.addEventListener('click', (e) => { e.stopPropagation(); if (idx > 0) show(idx - 1); });
  next.addEventListener('click', (e) => { e.stopPropagation(); if (idx < items.length - 1) show(idx + 1); });
  close.addEventListener('click', (e) => { e.stopPropagation(); done(); });
  root.addEventListener('click', (e) => { if (e.target === root || e.target === box) done(); });
  const onKey = (e) => {
    if (e.key === 'Escape') done();
    if (e.key === 'ArrowLeft' && idx > 0) show(idx - 1);
    if (e.key === 'ArrowRight' && idx < items.length - 1) show(idx + 1);
  };
  document.addEventListener('keydown', onKey);
  const done = () => {
    document.removeEventListener('keydown', onKey);
    root.remove();
    _viewer = null;
    document.body.style.overflow = '';
  };

  if (items.length > 1) { box.appendChild(prev); box.appendChild(next); }
  box.appendChild(stage);
  box.appendChild(counter);
  box.appendChild(close);
  box.appendChild(dl);
  stage.appendChild(img);
  root.appendChild(box);
  document.body.appendChild(root);
  document.body.style.overflow = 'hidden';
  _viewer = root;
  show(idx);
}

async function openDayPhotos(r) {
  try {
    const d = await api('/api/photos?shop_id=' + encodeURIComponent(S.shopId)
      + '&day=' + encodeURIComponent(S.day) + '&unit_id=' + encodeURIComponent(r.unit_id));
    openViewer(d.photos || [], 0, r.name);
  } catch (e) {
    toast(String(e.message || e), 'bad');
  }
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

function shopName() {
  const s = (S.shops || []).find((x) => x.id === S.shopId);
  return s ? s.name : '';
}

function filledItems(rows) {
  return rows.filter(hasReading).map((r) => ({
    unit_id: r.unit_id, in_r: r.in_r, out_r: r.out_r,
    p_tokens: r.p_tokens, p_out: r.p_out, p_cards: r.p_cards,
  }));
}

/* 保存 = 暂存，不取宝点数据；提交 = 存完再取数（= 以前的「取数」） */
async function saveDraft(btn) {
  const items = filledItems(S.entry);
  if (!items.length) { toast('还没填读数', 'bad'); return false; }
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '保存中';
  try {
    const res = await api('/api/entry', { shop_id: S.shopId, day: S.day, items });
    $('saveHint').textContent = '已暂存 ' + res.saved + ' 台，还没提交';
    toast('已保存');
    return true;
  } catch (e) {
    toast(String(e.message || e), 'bad');
    return false;
  } finally {
    btn.disabled = false;
    btn.textContent = old;
    updateProgress();
  }
}

async function submitDay(btn) {
  const rows = S.entry.filter((r) => r.active !== false);
  const lack = rows.filter((r) => !hasReading(r));
  if (lack.length) {
    toast('还有 ' + lack.length + ' 台没填', 'bad');
    focusUnit(lack[0].unit_id);
    return;
  }
  const noPic = rows.filter((r) => !(r.photos && r.photos.some((p) => !p.expired)));
  if (noPic.length) {
    toast(noPic[0].name + ' 还没拍照', 'bad');
    focusUnit(noPic[0].unit_id);
    return;
  }
  btn.disabled = true;
  $('saveHint').textContent = '提交中';
  try {
    await api('/api/entry', { shop_id: S.shopId, day: S.day, items: filledItems(rows) });
    if (S.provider === 'manual') {
      $('saveHint').textContent = '已提交';
      toast('已提交');
      setView('recon');
      return;
    }
    $('saveHint').textContent = '已存 ' + rows.length + ' 台，正在取数';
    await api('/api/fetch', { shop_id: S.shopId, day: S.day });
    $('saveHint').textContent = '已提交';
    toast('已提交');
    setView('recon');
  } catch (e) {
    $('saveHint').textContent = '取数失败，读数已保存';
    toast(String(e.message || e), 'bad');
  } finally {
    btn.disabled = false;
    updateProgress();
  }
}

/* ------------------------------------------------------------ 核对 */
const TONE = {
  '正常': 'ok',
  '第一次填': 'warn',
  '读数比昨天小': 'bad',
  '今天还没填': 'warn',
  '卡头今天没有数据': 'warn',
  '只抄表（无卡头）': 'mute',
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
    + (S.kind === 'pachinko' ? '卡头投币 对比 码表投币' : '卡头收币 减 码表收币');
  $('fDiff').textContent = fmt1(s.diff_ok);
  $('fDiff').dataset.tone = Math.abs(s.diff_ok) >= 1000 ? 'bad' : '';
  $('fMeta').textContent = `${s.ok} 台可核对 · 含异常合计 ${fmt1(s.diff_all)} · 误差超千 ${s.big} 台`;

  // 锁定状态只作为提示显示，锁定/解锁按钮已下线
  const fz = d.frozen;
  if (fz) {
    $('reconLede').textContent = d.day + ' · 已锁定'
      + (fz.at ? '（' + fz.at.slice(0, 16) + ' ' + (fz.by || '') + '）' : '');
  }

  const pach = S.kind === 'pachinko';
  // 空壳机台（没卡头也没读数）不进核对表——历史拆分留下的残骸
  d.rows = d.rows.filter((r) => (r.comms || []).length || r.in_r !== null || r.out_r !== null
    || r.meter !== null);
  // 同一台机的几行排在一起（弹珠机按品牌），组内按 P 位
  d.rows = groupByName(d.rows.slice(), pach);
  document.querySelectorAll('#reconKind .seg__b').forEach((b) => {
    b.setAttribute('aria-selected', b.dataset.kind === S.kind ? 'true' : 'false');
  });

  const head = $('reconHead');
  head.innerHTML = '';
  const hr = el('tr');
  const cols = reconColumns(d.rows, pach);
  cols.forEach((c) => {
    const th = el('th', c.cls, c.label);
    th.scope = 'col';
    hr.appendChild(th);
  });
  head.appendChild(hr);

  const body = $('reconBody');
  body.innerHTML = '';
  $('reconEmpty').hidden = d.rows.length > 0;

  // 一台机一组：组头写机台名（弹珠机写品牌），组里一行一个卡头；合并的只有一行
  const keyOf = (r) => (pach ? (r.brand || '未分品牌') : (baseName(r.name) || r.name));
  // 同一个卡头在组里只留一行：优先保留有照片 / 有读数 / 有宝点数的那行（拆分合并留下的空壳会被挤掉）
  const weight = (r) => ((r.photos || 0) ? 4 : 0)
    + ((r.in_r !== null && r.in_r !== undefined) || (r.out_r !== null && r.out_r !== undefined) ? 2 : 0)
    + (r.meter !== null && r.meter !== undefined ? 1 : 0);
  {
    const groups = new Map();
    d.rows.forEach((r) => {
      const k = keyOf(r);
      if (!groups.has(k)) groups.set(k, []);
      groups.get(k).push(r);
    });
    const out = [];
    groups.forEach((list) => {
      list.sort((a, b) => weight(b) - weight(a)
        || String(((a.poss || [])[0] || '')).localeCompare(String(((b.poss || [])[0] || ''))));
      const seen = new Set();
      list.forEach((r) => {
        const cs = (r.comms || []).map(String);
        if (cs.length && cs.every((c) => seen.has(c))) return;   // 这个卡头已经有更完整的一行
        cs.forEach((c) => seen.add(c));
        out.push(r);
      });
    });
    d.rows = out;
  }
  const counts = {};
  d.rows.forEach((r) => {
    const k = keyOf(r);
    counts[k] = (counts[k] || 0) + (pach ? 1 : ((r.comms || []).length || 1));
  });
  // 共用码表：这台机只抄一份数、几个卡头喂一块表，组头挂个标（和设置页一致）
  const sharedGroup = new Set();
  d.rows.forEach((r) => {
    if (r.shared && (r.comms || []).length > 1 && counts[keyOf(r)] === (r.comms || []).length) {
      sharedGroup.add(keyOf(r));
    }
  });
  let lastGroup = null;

  d.rows.forEach((r) => {
    const g = keyOf(r);
    if (g !== lastGroup) {
      const gtr = el('tr', 'recon-group');
      const gtd = el('td');
      gtd.colSpan = Math.max(1, cols.length);
      gtd.appendChild(el('span', 'recon-group__name', g));
      gtd.appendChild(el('span', 'recon-group__n',
        pach ? (counts[g] + ' 台') : (counts[g] + ' 个卡头')));
      if (sharedGroup.has(g)) {
        gtd.appendChild(el('span', 'recon-group__tag machine__tag machine__tag--shared', '共用码表'));
      }
      gtr.appendChild(gtd);
      body.appendChild(gtr);
      lastGroup = g;
    }

    const tr = el('tr');
    cols.forEach((c) => {
      if (c.key === 'name') {
        const tdName = el('td', 'name');
        // 组头已经有名字了，卡上写「P位：通讯ID」（合并的写 P1–P4）
        // 弹珠机一台一行，行里还得带机台名，不然同品牌下分不出是哪台
        if (pach) tdName.appendChild(el('span', 'tbl__mach', baseName(r.name) + ' '));
        const ps = (r.poss || []).filter(Boolean);
        const one = ps.length === 1;
        const nm2 = /^(.*?)\s+(\d{5,})$/.exec(r.name || '');
        const comms = (r.comms || []);
        tdName.appendChild(el('span', 'tbl__pos',
          one ? ps[0] : (posLabel(r) || '')));
        const cid = one ? (comms[0] || (nm2 ? nm2[2] : '')) : '';
        if (cid) tdName.appendChild(el('span', 'tbl__comm', '：' + cid));
        if (!ps.length && !cid) {
          tdName.textContent = baseName(r.name) || r.name;
        }
        // 照片：有就点开看，没有就提示（核对页也能翻这一天的照片）
        const n = r.photos || 0;
        const ex = !!r.photos_expired;
        const chip = el('button', 'pic-chip', n ? (ex ? '照片已过期' : ('照片 ' + n)) : '无照片');
        chip.type = 'button';
        chip.dataset.has = n && !ex ? 'true' : 'false';
        if (n && !ex) {
          chip.addEventListener('click', () => openDayPhotos(r));
        } else {
          chip.disabled = true;
        }
        tdName.appendChild(chip);
        tr.appendChild(tdName);
        return;
      }
      if (c.key === 'status') {
        const st = el('td');
        st.appendChild(statusTag(r.status));
        tr.appendChild(st);
        return;
      }
      if (c.key === 'reason') {
        tr.appendChild(reasonCell(r));
        return;
      }
      const v = c.text(r);
      const td = el('td', c.cls);
      if (v === null || v === undefined) {
        td.textContent = '-';
      } else if (c.raw) {
        td.textContent = String(v);
      } else if (c.cls && c.cls.split(' ').indexOf('num') >= 0) {
        td.textContent = nf.format(v);
      } else {
        td.textContent = String(v);
      }
      if (c.bad && c.bad(r)) td.dataset.tone = 'bad';
      tr.appendChild(td);
    });
    // 手机上每行变卡片，用 data-label 显示列名
    [...tr.children].forEach((td, i) => {
      if (cols[i]) td.dataset.label = cols[i].label;
    });
    body.appendChild(tr);
  });
}

// P 位：连着的一组写成 P1–P4，其它情况用斜杠拼
function posLabel(r) {
  const arr = (r.poss || []).filter(Boolean);
  if (!arr.length) return null;
  const m = arr.map((x) => /^P(\d+)$/.exec(String(x).trim()));
  if (m.every(Boolean)) {
    const ns = m.map((x) => Number(x[1])).sort((a, b) => a - b);
    const seq = ns.every((n, i) => i === 0 || n === ns[i - 1] + 1);
    if (seq && ns.length > 1) return 'P' + ns[0] + '–P' + ns[ns.length - 1];
    if (seq) return 'P' + ns[0];
  }
  return arr.join(' / ');
}

// 核对表的列定义：页面和导出共用这一份，避免两边各写一套导致文件对不上页面
// mode='page' 页面（推币机只留 卡头收币/码表收币/误差/状态/原因）
// mode='file' 导出（多带 卡头ID 和 上/下币，明细能复盘）
function reconColumns(rows, pach, mode = 'page') {
  const file = mode === 'file';
  const uni = {};
  (rows || []).forEach((r) => (r.fields || []).forEach((f) => { uni[f] = 1; }));
  const hasF = (k) => !!uni[k];
  const num = (v) => (v === null || v === undefined || v === '' ? null : Math.round(v));
  // 比率列自己带 %，不再走 nf.format（那会把 "8.3%" 当数字）
  const pct = (v) => (v === null || v === undefined ? null : (v * 100).toFixed(1) + '%');
  const pm = (r) => r.p || {};
  const cols = [
    {
      key: 'name', label: '机台',
      // 导出的机台名不带尾部通讯ID（卡头ID 单独一列）；页面沿用 名字+ID 的写法
      text: file
        ? (r) => String(r.name || '').replace(/\s+\d{5,}$/, '')
        : (r) => r.name,
    },
  ];
  if (file) {
    cols.push({
      key: 'comms', label: '卡头ID', cls: 'comm',
      text: (r) => ((r.comms && r.comms.length) ? r.comms.join(' / ') : null),
    });
    cols.push({ key: 'poss', label: '卡头P位', cls: 'comm', text: posLabel });
  }
  // 页面上的 P 位写在机台格子里（P2：733670），不再单占一列
  if (pach) {
    if (hasF('in_r')) {
      cols.push({ key: 'meter_in', label: '码表投币', cls: 'num', text: (r) => num(r.meter_in) });
      cols.push({ key: 'up', label: '卡头投币', cls: 'num', text: (r) => num(r.up) });
    }
    if (hasF('p_tokens')) {
      cols.push({ key: 'tokens', label: '码表投珠', cls: 'num', text: (r) => num(pm(r).tokens) });
    }
    // 中奖出珠：含首出珠的机器 = 总出珠 − 投币×一币几珠；只计中奖的机器 = 总出珠
    if (hasF('p_out')) {
      cols.push({ key: 'win', label: '中奖出珠', cls: 'num', text: (r) => num(pm(r).win) });
    }
    if (hasF('p_cards')) {
      cols.push({ key: 'cards', label: '码表出卡', cls: 'num', text: (r) => num(pm(r).cards) });
    }
    cols.push({
      key: 'diff', label: '收币误差', cls: 'num lead', text: (r) => num(r.diff),
      bad: (r) => r.diff !== null && Math.abs(r.diff) >= 1,
    });
    if (hasF('p_cards') && hasF('p_out')) {
      cols.push({
        key: 'card_rate', label: '出卡率', cls: 'num', raw: true,
        text: (r) => pct(pm(r).card_rate),
        bad: (r) => pm(r).card_rate !== null && pm(r).card_rate > 1,
      });
      cols.push({
        key: 'cost_rate', label: '成本率', cls: 'num', raw: true,
        text: (r) => pct(pm(r).cost_rate),
      });
    }
  } else {
    if (file) {
      cols.push({ key: 'up', label: '卡头上币', cls: 'num', text: (r) => num(r.up) });
      cols.push({ key: 'down', label: '卡头下币', cls: 'num', text: (r) => num(r.down) });
    }
    cols.push({ key: 'head', label: '卡头收币', cls: 'num', text: (r) => num(r.head) });
    if (file) {
      cols.push({ key: 'meter_in', label: '码表上币', cls: 'num', text: (r) => num(r.meter_in) });
      cols.push({ key: 'meter_out', label: '码表下币', cls: 'num', text: (r) => num(r.meter_out) });
    }
    cols.push({ key: 'meter', label: '码表收币', cls: 'num', text: (r) => num(r.meter) });
    cols.push({
      key: 'diff', label: '误差', cls: 'num lead', text: (r) => num(r.diff),
      bad: (r) => r.diff !== null && Math.abs(r.diff) >= 1000,
    });
  }
  cols.push({ key: 'status', label: '状态', text: (r) => r.status });
  cols.push({
    key: 'reason', label: '原因', cls: 'reason',
    text: (r) => [r.reason, r.reason_note].filter(Boolean).join(' ') || null,
  });
  return cols;
}

const REASONS = ['换码表或归零重计', '机器故障没走系统', '未下完分断电',
  '人为抄错已改', '其他'];

function reasonCell(r) {
  const td = el('td', 'reason');
  const hasDiff = r.diff !== null && r.diff !== undefined && Math.abs(r.diff) >= 1;
  if (r.reason) {
    const b = el('button', 'reason__chip', r.reason);
    b.type = 'button';
    b.addEventListener('click', () => reasonDialog(r));
    td.appendChild(b);
    if (r.reason_note) {
      td.appendChild(document.createTextNode(' '));
      td.appendChild(el('span', 'reason__note', r.reason_note));
    }
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
  const closeAll = () => {
    pops.forEach((p) => p.remove());
    scrim.remove();
  };
  cancel.addEventListener('click', closeAll);
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
  trackDlg(closeBrand);
  save.focus();
}

function exportDialog() {
  const scrim = el('div', 'scrim');
  const dlg = el('div', 'dlg');
  dlg.setAttribute('role', 'dialog');
  dlg.setAttribute('aria-modal', 'true');
  dlg.appendChild(el('h2', 'dlg__t', '导出核对表'));
  dlg.appendChild(el('p', 'dlg__d', '选一个区间，导出这期间每天的核对结果'));

  const row = el('div', 'exp-row');
  row.appendChild(el('span', 'exp-label', '区间'));
  const wrap = el('div', 'pick');
  const btn = el('button', 'pick__btn');
  btn.type = 'button';
  btn.setAttribute('aria-haspopup', 'dialog');
  btn.setAttribute('aria-expanded', 'false');
  const valEl = el('span', 'pick__val mono');
  btn.appendChild(valEl);
  wrap.appendChild(btn);
  row.appendChild(wrap);
  dlg.appendChild(row);

  const pops = [];
  const pop = el('div', 'dp__pop');
  pop.hidden = true;
  pop.style.position = 'fixed';
  pop.style.zIndex = '80';
  document.body.appendChild(pop);
  btn.addEventListener('click', () => {
    const r = btn.getBoundingClientRect();
    const w = 300;
    pop.style.top = Math.round(r.bottom + 4) + 'px';
    pop.style.left = Math.round(Math.min(r.left, window.innerWidth - w - 8)) + 'px';
  });

  let range = { from: S.day, to: S.day };
  const rp = UI.dateRangePicker({
    btn, pop, valueEl: valEl, from: S.day, to: S.day, max: S.editDay,
    onChange: (a, b) => { range = { from: a, to: b }; },
  });
  pops.push(pop);

  const acts = el('div', 'dlg__acts');
  const cancel = el('button', 'btn btn--quiet', '取消');
  cancel.type = 'button';
  const closeAll = () => {
    pops.forEach((p) => p.remove());
    scrim.remove();
  };
  cancel.addEventListener('click', closeAll);
  const go = el('button', 'btn btn--primary', '导出');
  go.type = 'button';
  go.addEventListener('click', async () => {
    go.disabled = true;
    go.textContent = '生成中';
    try {
      await doExport(range.from, range.to);
      closeAll();
    } catch (e) {
      toast(String(e.message || e), 'bad');
      go.disabled = false;
      go.textContent = '导出';
    }
  });
  acts.appendChild(cancel);
  acts.appendChild(go);
  dlg.appendChild(acts);
  scrim.appendChild(dlg);
  document.body.appendChild(scrim);
  go.focus();
}

async function doExport(fromDay, toDay) {
  const pach = S.kind === 'pachinko';
  const d = await api('/api/recon/range?shop_id=' + encodeURIComponent(S.shopId)
    + '&kind=' + encodeURIComponent(S.kind)
    + '&from=' + encodeURIComponent(fromDay) + '&to=' + encodeURIComponent(toDay));
  if (!d.rows.length) throw new Error('这段时间没有数据');

  const text = buildReconCsv(d, pach);
  const shop = (S.shops.find((x) => x.id === S.shopId) || {}).name || '门店';
  const blob = new Blob(['\ufeff' + text], { type: 'text/csv;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = '核对_' + shop + '_' + d.from + '_' + d.to + (pach ? '_弹珠机' : '_推币机') + '.csv';
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  toast('已导出 ' + d.rows.length + ' 行');
}

// 导出内容用核对页同一份列定义（导出模式多带卡头ID 与上/下币），列名和取整不会两边走样
function buildReconCsv(d, pach) {
  const cols = reconColumns(d.rows, pach, 'file');
  const head = ['日期'].concat(cols.map((c) => c.label));
  const cell = (v) => {
    if (v === null || v === undefined) return '';
    const t = String(v);
    return /[",\n\r]/.test(t) ? '"' + t.replace(/"/g, '""') + '"' : t;
  };
  const lines = [head.map(cell).join(',')];
  d.rows.forEach((r) => {
    const row = [r.day];
    cols.forEach((c) => {
      const v = c.text(r);
      row.push(v === null || v === undefined ? '' : v);
    });
    lines.push(row.map(cell).join(','));
  });
  return lines.join('\r\n');
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
  if ($('btnBdSync')) $('btnBdSync').hidden = true;
  if ($('btnJjSync')) $('btnJjSync').hidden = true;
  $('shopPane').hidden = false;
  $('unitPane').hidden = true;

  const d = await api('/api/manage/shops');
  const body = $('shopBody');
  body.innerHTML = '';
  d.rows.forEach((r) => {
    const tr = el('tr');
    const tdName = el('td', 'name', r.name);
    tdName.dataset.label = '门店';
    tr.appendChild(tdName);
    const tdN = el('td', 'num', nf.format(r.units));
    tdN.dataset.label = '码表机台';
    tr.appendChild(tdN);

    // 取数方式：宝点 / 鲸舰 / 纯手工
    const tdP = el('td');
    tdP.dataset.label = '取数方式';
    const sel = el('select', 'input add-panel__sel');
    [['baodian', '宝点'], ['jingjian', '鲸舰'], ['shiruan', '世软'], ['manual', '纯手工']].forEach(([v, t]) => {
      const o = el('option', null, t);
      o.value = v;
      if ((r.provider || 'baodian') === v) o.selected = true;
      sel.appendChild(o);
    });
    sel.setAttribute('aria-label', '取数方式');
    sel.addEventListener('change', async () => {
      try {
        await api('/api/manage/shop/provider', { shop_id: r.id, provider: sel.value });
        toast('已改为' + sel.options[sel.selectedIndex].text);
        loadSettings();
      } catch (e) {
        toast(String(e.message || e), 'bad');
        sel.value = r.provider || 'baodian';
      }
    });
    tdP.appendChild(sel);
    tr.appendChild(tdP);
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
  BRANDS = null;
  MUNIT = null;
  MGROUP = null;
  EDIT_KEY = null;
  $('shopPane').hidden = true;
  $('unitPane').hidden = false;
  $('unitTitle').textContent = shop.name;
  $('btnBackShops').hidden = S.role === 'store';
  if ($('btnBdSync')) $('btnBdSync').hidden = (shop.provider || 'baodian') !== 'baodian';
  if ($('btnJjSync')) $('btnJjSync').hidden = (shop.provider || '') !== 'jingjian';
  await renderUnits();
}

let EDIT_KEY = null;

/* 从鲸舰同步卡头：把每台机的 P位 + 通讯ID 补进来（只补卡头，不改机台名） */
async function syncJjComms(btn) {
  if (!MSHOP) return;
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '同步中…';
  try {
    const r = await api('/api/manage/jj-comms/sync', { shop_id: MSHOP.id, days: 7 });
    toast('卡头已同步：' + r.machines + ' 台，新增 ' + r.comms_added + ' 个卡头'
      + ((r.missed || []).length ? '；名字对不上的 ' + r.missed.length + ' 台' : ''));
    await renderUnits();
  } catch (e) {
    toast(String(e.message || e), 'bad');
  } finally {
    btn.disabled = false;
    btn.textContent = old;
  }
}

/* 从后台同步机台信息：拉清单 → 勾选 → 写入（名称/类型/卡头/P位 以后台为准） */
async function openBdSync(btn) {
  if (btn) { btn.disabled = true; btn.textContent = '拉取中…（约15秒）'; }
  let d;
  try {
    d = await api('/api/manage/bd-sync/preview?shop_id=' + encodeURIComponent(MSHOP.id));
  } catch (e) {
    toast(String(e.message || e), 'bad');
    if (btn) { btn.disabled = false; btn.textContent = '从后台同步机台信息'; }
    return;
  }
  if (btn) { btn.disabled = false; btn.textContent = '从后台同步机台信息'; }

  const rows = d.rows || [];
  const root = el('div');
  root.style.cssText = 'position:fixed;inset:0;z-index:80;display:flex;align-items:center;'
    + 'justify-content:center;background:rgba(0,0,0,.45);padding:16px';
  const close = () => {
    document.removeEventListener('keydown', onKey);
    document.body.style.overflow = '';
    root.remove();
  };
  const onKey = (e) => {
    if (e.key === 'Escape') { e.preventDefault(); close(); }
  };
  document.addEventListener('keydown', onKey);
  document.body.style.overflow = 'hidden';
  root.addEventListener('click', (e) => { if (e.target === root) close(); });
  const box = el('div');
  box.style.cssText = 'width:min(980px,96vw);max-height:86vh;overflow:auto;padding:20px;'
    + 'background:#fff;color:#1a1a1a;border-radius:12px;box-shadow:0 20px 60px rgba(0,0,0,.35)';
  const dlgHead = el('div');
  dlgHead.style.cssText = 'display:flex;align-items:flex-start;justify-content:space-between;gap:12px';
  const dlgTitle = el('h2', 'dlg__t', '从后台同步机台信息');
  dlgTitle.style.cssText = 'margin:0;font-size:20px;font-weight:600';
  dlgHead.appendChild(dlgTitle);
  const xBtn = el('button', 'btn btn--quiet', '×');
  xBtn.type = 'button';
  xBtn.setAttribute('aria-label', '关闭');
  xBtn.style.cssText = 'font-size:20px;line-height:1;padding:2px 10px';
  xBtn.addEventListener('click', close);
  dlgHead.appendChild(xBtn);
  box.appendChild(dlgHead);
  box.appendChild(el('p', 'dlg__d', '后台共 ' + d.devices + ' 台要写码表 · P位取到 '
    + d.pos_have + ' 个卡头（近 ' + d.days + ' 天）· 没勾的不会动'));

  const tbl = el('table');
  tbl.style.cssText = 'width:100%;border-collapse:collapse;font-size:14px';
  const thead = el('thead');
  const htr = el('tr');
  htr.appendChild(el('th'));
  ['机台', '卡头ID', 'P位'].forEach((t) => htr.appendChild(el('th', null, t)));
  const master = document.createElement('input');
  master.type = 'checkbox';
  master.title = '全选 / 全不选';
  master.setAttribute('aria-label', '全选');
  htr.firstChild.appendChild(master);
  thead.appendChild(htr);
  tbl.appendChild(thead);
  const tbody = el('tbody');
  const items = [];
  // 按机台种类分组：推币机一组、弹珠机一组（组里没东西就不出现）
  const addRow = (r) => {
    const tr = el('tr');
    const td0 = el('td');
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = r.status !== 'same';
    cb.setAttribute('aria-label', '同步 ' + r.name);
    td0.appendChild(cb);
    items.push({ cb, r });
    tr.appendChild(td0);
    tr.appendChild(el('td', null, r.name));
    tr.appendChild(el('td', 'comm', r.comms.join(' / ')));
    tr.appendChild(el('td', 'comm', (r.pos || []).filter(Boolean).join(' / ') || '未标'));
    tbody.appendChild(tr);
  };
  [['pusher', '推币机'], ['pachinko', '弹珠机']].forEach(([kind, label]) => {
    const list = rows.filter((r) => (r.kind || 'pusher') === kind);
    if (!list.length) return;
    const gr = el('tr', 'bd-sync__grp');
    const th = el('th', 'bd-sync__grp-t', label + ' · ' + list.length + ' 台');
    th.colSpan = 4;
    th.scope = 'colgroup';
    gr.appendChild(th);
    tbody.appendChild(gr);
    list.forEach(addRow);
  });
  tbl.appendChild(tbody);
  const refreshMaster = () => {
    const on = items.filter((x) => x.cb.checked).length;
    master.checked = items.length > 0 && on === items.length;
    master.indeterminate = on > 0 && on < items.length;
  };
  master.addEventListener('change', () => {
    items.forEach((x) => { x.cb.checked = master.checked; });
    refreshMaster();
  });
  items.forEach((x) => x.cb.addEventListener('change', refreshMaster));
  refreshMaster();
  [].forEach.call(tbl.querySelectorAll('th,td'), (c) => {
    c.style.padding = '7px 8px';
    c.style.borderBottom = '1px solid #eee';
    c.style.verticalAlign = 'middle';
    c.style.whiteSpace = 'nowrap';
  });
  box.appendChild(tbl);

  // 没认出来的宝点分组只在接口数据里留着，界面不显示（用户要求去掉这个框）
  if ((d.orphan || []).length) {
    box.appendChild(el('p', 'dlg__d', '后台已没有这些本地机台（不会自动停用）：'
      + d.orphan.map((x) => x.name).join('、')));
  }

  const acts = el('div', 'dlg__acts');
  const cancel = el('button', 'btn btn--quiet', '取消');
  cancel.type = 'button';
  cancel.addEventListener('click', close);
  const ok = el('button', 'btn btn--primary', '同步');
  ok.type = 'button';
  ok.addEventListener('click', async () => {
    const picked = items.filter((x) => x.cb.checked).map((x) => ({
      unit_id: x.r.local ? x.r.local.unit_id : null,
      name: x.r.name, kind: x.r.kind, comms: x.r.comms, pos: x.r.pos,
    }));
    if (!picked.length) { toast('一台都没勾', 'bad'); return; }
    ok.disabled = true;
    ok.textContent = '同步中…';
    try {
      const res = await api('/api/manage/bd-sync/apply', { shop_id: MSHOP.id, items: picked });
      toast('同步完成：新增 ' + res.created + ' · 更新 ' + res.updated
        + ' · 写入P位 ' + res.pos + (res.merged ? ' · 并掉旧行 ' + res.merged : ''));
      close();
      await renderUnits();
    } catch (e) {
      toast(String(e.message || e), 'bad');
      ok.disabled = false;
      ok.textContent = '同步';
    }
  });
  acts.appendChild(cancel);
  acts.appendChild(ok);
  box.appendChild(acts);
  root.appendChild(box);
  document.body.appendChild(root);
}

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

  const mkHead = (title, count, kind, brand) => {
    const head = el('h2', 'machine-group');
    head.appendChild(el('span', null, title));
    head.appendChild(el('span', 'machine-group__n', count + ' 台'));
    const addBtn = el('button', 'btn btn--sm btn--quiet machine-group__add', '+ 新增');
    addBtn.type = 'button';
    addBtn.setAttribute('aria-label', '新增一台' + title);
    addBtn.addEventListener('click', () => {
      const same = ADD_KIND === kind && ADD_BRAND === brand;
      ADD_KIND = same ? null : kind;
      ADD_BRAND = same ? null : (brand || null);
      renderUnits();
    });
    head.appendChild(addBtn);
    return head;
  };

  const mkGrid = (rows) => {
    const grid = el('div', 'machines');
    rows.forEach((g) => {
      const card = el('article', 'machine');
      if (EDIT_KEY === g.key) renderMachineEdit(card, g);
      else renderMachineView(card, g);
      grid.appendChild(card);
    });
    return grid;
  };

  // ---------- 推币机 ----------
  const pushers = d.rows.filter((g) => (g.kind || 'pusher') === 'pusher');
  if (pushers.length) {
    wrap.appendChild(mkHead('推币机', pushers.length, 'pusher', null));
    if (ADD_KIND === 'pusher') wrap.appendChild(addMachineForm('pusher', null));
    wrap.appendChild(mkGrid(pushers));
  }

  // ---------- 弹珠机：先品牌管理，再按品牌分块 ----------
  const pachs = d.rows.filter((g) => g.kind === 'pachinko');
  if (pachs.length) {
    // 先加载品牌，新增面板里才有品牌可选
    const cfgNode = await brandCfg();
    wrap.appendChild(mkHead('弹珠机', pachs.length, 'pachinko', null));
    if (ADD_KIND === 'pachinko') wrap.appendChild(addMachineForm('pachinko'));
    wrap.appendChild(cfgNode);

    const byBrand = {};
    pachs.forEach((g) => {
      const b = g.brand || '未分品牌';
      (byBrand[b] = byBrand[b] || []).push(g);
    });
    const names = Object.keys(byBrand).sort((a, b) => byBrand[b].length - byBrand[a].length);
    names.forEach((b) => {
      const bh = el('h3', 'brand-group');
      bh.appendChild(el('span', 'brand-group__name', b));
      bh.appendChild(el('span', 'brand-group__n', byBrand[b].length + ' 台'));
      wrap.appendChild(bh);
      wrap.appendChild(mkGrid(byBrand[b]));
    });
  }

  if (EDIT_KEY) {
    const f = wrap.querySelector('.machine__name-edit');
    if (f) { f.focus(); f.select(); }
  }
}

let ADD_KIND = null;
let ADD_BRAND = null;


function closeAddPanel() {
  untrackDlg(closeAddPanel);
  ADD_KIND = null;
  ADD_BRAND = null;
  renderUnits();
}

function addMachineForm(kind) {
  const box = el('div', 'add-panel');
  trackDlg(closeAddPanel);

  // ---------- 品牌 ----------
  let getBrand = () => undefined;
  let brandWrap = null;
  let newIn = null;
  let sel = null;
  if (kind === 'pachinko') {
    brandWrap = el('div', 'add-panel__brand');
    brandWrap.appendChild(el('span', 'add-panel__k', '品牌'));
    sel = el('select', 'input add-panel__sel');
    (BRANDS || []).forEach((b) => {
      const o = el('option', null, b.brand);
      o.value = b.brand;
      sel.appendChild(o);
    });
    sel.appendChild(el('option', null, '＋ 新品牌')).value = '__new__';
    brandWrap.appendChild(sel);

    newIn = el('input', 'input add-panel__newbrand');
    newIn.type = 'text';
    newIn.autocomplete = 'off';
    newIn.placeholder = '新品牌名';
    newIn.setAttribute('aria-label', '新品牌名称');
    newIn.hidden = true;
    brandWrap.appendChild(newIn);

    sel.addEventListener('change', () => {
      newIn.hidden = sel.value !== '__new__';
      if (!newIn.hidden) newIn.focus();
    });
    box.appendChild(brandWrap);
    getBrand = () => (sel.value === '__new__' ? newIn.value.trim() : sel.value);
  }

  // ---------- 机台列表 ----------
  const bar = el('div', 'add-panel__bar');
  bar.appendChild(el('span', 'add-panel__k', '从宝点选择'));
  const countTip = el('span', 'add-panel__count', '');
  bar.appendChild(countTip);
  const pull = el('button', 'btn btn--sm btn--quiet', '重新拉取');
  pull.type = 'button';
  bar.appendChild(pull);
  box.appendChild(bar);

  const listBox = el('div', 'add-panel__list');
  box.appendChild(listBox);

  const foot = el('div', 'add-panel__foot');
  const addSel = el('button', 'btn btn btn--primary', '添加选中');
  addSel.type = 'button';
  addSel.disabled = true;
  const closeBtn = el('button', 'btn btn--quiet', '取消');
  closeBtn.type = 'button';
  closeBtn.addEventListener('click', closeAddPanel);
  foot.appendChild(addSel);
  foot.appendChild(closeBtn);
  box.appendChild(foot);

  let cbs = [];
  const sync = () => {
    const n = cbs.filter((c) => c.checked).length;
    addSel.disabled = n === 0;
    addSel.textContent = n ? ('添加选中 ' + n + ' 台') : '添加选中';
    cbs.forEach((c) => { c.closest('tr').dataset.on = c.checked ? 'true' : 'false'; });
  };

  const load = async () => {
    pull.disabled = true;
    listBox.innerHTML = '';
    listBox.appendChild(el('p', 'add-panel__hint', '正在从宝点取最近三天的机台…'));
    try {
      const d = await api('/api/manage/devices?shop_id=' + encodeURIComponent(MSHOP.id));
      const mine = (d.rows || []).filter((x) => x.kind === kind);
      const todo = mine.filter((x) => !x.added);
      const done = mine.filter((x) => x.added);
      listBox.innerHTML = '';
      cbs = [];

      if (!mine.length) {
        countTip.textContent = '';
        listBox.appendChild(el('p', 'add-panel__hint', '宝点最近三天没有这类机台'));
        sync();
        return;
      }
      countTip.textContent = '未添加 ' + todo.length + ' / 共 ' + mine.length;

      if (!todo.length) {
        listBox.appendChild(el('p', 'add-panel__hint', '宝点里的这类机台都已经在系统里了'));
      }

      const tb = el('table', 'tbl add-tbl');
      const body = el('tbody');
      todo.forEach((x) => {
        const tr = el('tr');
        const td0 = el('td', 'add-tbl__cb');
        const cb = el('input');
        cb.type = 'checkbox';
        cb.value = x.comm;
        cb.dataset.name = x.name;
        cb.setAttribute('aria-label', '选择 ' + x.name + ' ' + x.comm);
        cbs.push(cb);
        td0.appendChild(cb);
        tr.appendChild(td0);
        tr.appendChild(el('td', 'name', x.name));
        tr.appendChild(el('td', 'add-tbl__comm', x.comm));
        body.appendChild(tr);
        tb.appendChild(body);
      });
      if (todo.length) {
        tb.appendChild(body);
        listBox.appendChild(tb);
        cbs.forEach((c) => c.addEventListener('change', sync));
      }

      if (done.length) {
        const more = el('details', 'add-panel__done');
        more.appendChild(el('summary', '', '已添加的 ' + done.length + ' 台'));
        const dl = el('ul', 'add-panel__donelist');
        done.forEach((x) => {
          const li = el('li');
          li.appendChild(el('span', null, x.name));
          li.appendChild(el('span', 'add-panel__comm', x.comm));
          dl.appendChild(li);
        });
        more.appendChild(dl);
        listBox.appendChild(more);
      }
      sync();
    } catch (e) {
      listBox.innerHTML = '';
      listBox.appendChild(el('p', 'add-panel__hint', String(e.message || e)));
    } finally {
      pull.disabled = false;
    }
  };

  pull.addEventListener('click', load);

  addSel.onclick = async () => {
    const picked = cbs.filter((c) => c.checked);
    if (!picked.length) return;
    const br = getBrand();
    if (kind === 'pachinko' && !br) { toast('先选品牌', 'bad'); return; }
    addSel.disabled = true;
    addSel.textContent = '添加中…';
    let ok = 0;
    for (const cb of picked) {
      try {
        await api('/api/manage/unit/save', {
          shop_id: MSHOP.id,
          name: cb.dataset.name + ' ' + cb.value,
          comms: cb.value,
          kind,
          brand: kind === 'pachinko' ? br : undefined,
          shared: false,
        });
        ok += 1;
      } catch (e) { /* 单台失败不影响其他 */ }
    }
    toast('已添加 ' + ok + ' 台');
    untrackDlg(closeAddPanel);
    ADD_KIND = null;
    ADD_BRAND = null;
    BRANDS = null;
    await renderUnits();
  };

  // ---------- 手工添加 ----------
  const manual = el('details', 'add-panel__manual');
  manual.appendChild(el('summary', '', '宝点里没有，手工录'));
  const form = el('div', 'add-form');

  const f1 = el('label', 'field');
  f1.appendChild(el('span', 'field__label', '机台名'));
  const name = el('input', 'input');
  name.type = 'text';
  name.autocomplete = 'off';
  name.placeholder = kind === 'pachinko' ? '塔兰弹珠机-017' : '凤凰城';
  f1.appendChild(name);
  form.appendChild(f1);

  // 卡头：P1 / P2 / … 一行一个
  const f2 = el('div', 'field');
  f2.appendChild(el('span', 'field__label', '卡头'));
  const ul = el('ul', 'mf__chips');
  ul.setAttribute('aria-label', '卡头通讯ID');
  f2.appendChild(ul);

  const XSvg = '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 3l6 6M9 3l-6 6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';

  function renumber() {
    [...ul.querySelectorAll('.chip--edit')].forEach((li, i) => {
      const b = li.querySelector('.chip__p');
      if (b) b.textContent = 'P' + (i + 1);
    });
  }

  function addChip(val) {
    const li = el('li', 'chip chip--edit');
    li.appendChild(el('b', 'chip__p', 'P1'));
    const inp = el('input', 'chip__in');
    inp.type = 'text';
    inp.inputMode = 'numeric';
    inp.autocomplete = 'off';
    inp.value = val || '';
    inp.placeholder = '通讯ID';
    inp.size = Math.max(6, (val || '').length);
    inp.setAttribute('aria-label', '卡头通讯ID');
    inp.addEventListener('input', () => { inp.size = Math.max(6, inp.value.length); });
    inp.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); save.click(); }
    });
    li.appendChild(inp);
    const x = el('button', 'chip__x');
    x.type = 'button';
    x.setAttribute('aria-label', '删掉这个卡头');
    x.innerHTML = XSvg;
    x.addEventListener('click', () => {
      li.remove();
      renumber();
      const first = ul.querySelector('.chip__in');
      if (first) first.focus();
    });
    li.appendChild(x);
    ul.appendChild(li);
    renumber();
    return inp;
  }

  addChip('');

  const addLi = el('li', 'chip chip--add');
  const addBtn = el('button', 'chip__add');
  addBtn.type = 'button';
  addBtn.setAttribute('aria-label', '加一个卡头');
  addBtn.innerHTML = '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 2.5v7M2.5 6h7" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';
  addBtn.addEventListener('click', () => {
    const inp = addChip('');
    inp.focus();
  });
  addLi.appendChild(addBtn);
  ul.appendChild(addLi);
  form.appendChild(f2);

  const mActs = el('div', 'add-form__acts');
  const save = el('button', 'btn btn--sm btn--primary', '保存');
  save.type = 'button';
  mActs.appendChild(save);
  form.appendChild(mActs);
  manual.appendChild(form);
  box.appendChild(manual);

  save.addEventListener('click', async () => {
    const nm = name.value.trim();
    const parts = [...ul.querySelectorAll('.chip__in')].map((i) => i.value.trim()).filter(Boolean);
    if (!nm) { toast('填机台名', 'bad'); name.focus(); return; }
    if (!parts.length) { toast('填至少一个卡头', 'bad'); ul.querySelector('.chip__in').focus(); return; }
    if (new Set(parts).size !== parts.length) { toast('卡头有重复', 'bad'); return; }
    const br = getBrand();
    if (kind === 'pachinko' && !br) { toast('先选品牌', 'bad'); return; }
    save.disabled = true;
    try {
      await api('/api/manage/unit/save', {
        shop_id: MSHOP.id, name: nm, comms: parts.join(','), kind,
        brand: kind === 'pachinko' ? br : undefined,
        shared: parts.length > 1,
      });
      toast('已新增');
      untrackDlg(closeAddPanel);
      ADD_KIND = null;
      ADD_BRAND = null;
      BRANDS = null;
      await renderUnits();
    } catch (e) {
      toast(String(e.message || e), 'bad');
      save.disabled = false;
    }
  });

  load();
  return box;
}

let BRANDS = null;
let BRANDS_SHOP = null;
let BRANDS_SHOP_DATA = null;

// 门店参数：珠值 / 卡值 / 一币几珠 —— 价格按店走，不跟品牌走
function shopParamsBlock() {
  const v = BRANDS_SHOP_DATA || {};
  const box = el('div', 'brand-cfg__params');
  const mk = (label, key, val, hint) => {
    const f = el('label', 'machine-cfg__f');
    f.appendChild(el('span', 'machine-cfg__k', label));
    const i = el('input', 'input machine-cfg__in');
    i.type = 'text';
    i.inputMode = 'decimal';
    i.value = (val === null || val === undefined) ? '' : String(val);
    i.placeholder = hint;
    i.setAttribute('aria-label', label);
    f.appendChild(i);
    box.appendChild(f);
    return i;
  };
  const iBead = mk('珠值（元/珠）', 'bead_value', v.bead_value, '留空');
  const iCard = mk('卡值（元/张）', 'card_value', v.card_value, '留空');
  const iPer = mk('一币几珠', 'beads_per_coin', v.beads_per_coin, '例 3');
  const b = el('button', 'btn btn--sm btn--primary', '保存参数');
  b.type = 'button';
  b.addEventListener('click', async () => {
    b.disabled = true;
    try {
      const r = await api('/api/manage/shop/values', {
        shop_id: MSHOP.id,
        bead_value: iBead.value.trim(),
        card_value: iCard.value.trim(),
        beads_per_coin: iPer.value.trim(),
      });
      BRANDS_SHOP_DATA = { ...(BRANDS_SHOP_DATA || {}), ...r };
      toast('已保存');
      if (S.view === 'recon') await loadRecon();
    } catch (e) {
      toast(String(e.message || e), 'bad');
    }
    b.disabled = false;
  });
  box.appendChild(b);
  return box;
}

function fillBrandOptions() {
  let dl = $('brandOptions');
  if (!dl) {
    dl = el('datalist');
    dl.id = 'brandOptions';
    document.body.appendChild(dl);
  }
  dl.innerHTML = '';
  (BRANDS || []).forEach((b) => {
    const o = el('option');
    o.value = b.brand;
    o.label = b.label || b.brand;
    dl.appendChild(o);
  });
}

async function brandCfg() {
  const box = el('div', 'brand-cfg');
  const head = el('div', 'brand-cfg__head');
  head.appendChild(el('span', 'brand-cfg__t', '机台品牌'));
  box.appendChild(head);

  if (!BRANDS || BRANDS_SHOP !== MSHOP.id) {
    box.appendChild(el('p', 'brand-cfg__sub', '读取中'));
    try {
      const d = await api('/api/brand/list?shop_id=' + encodeURIComponent(MSHOP.id));
      BRANDS = d.rows;
      BRANDS_SHOP = MSHOP.id;
      BRANDS_SHOP_DATA = d.shop || null;
      fillBrandOptions();
    } catch (e) {
      box.appendChild(el('p', 'brand-cfg__sub', String(e.message || e)));
      return box;
    }
    box.innerHTML = '';
    box.appendChild(head);
  }

  if (BRANDS_SHOP_DATA) box.appendChild(shopParamsBlock());

  const tb = el('table', 'tbl tbl--tight brand-tbl');
  const thead = el('thead');
  const hr = el('tr');
  ['品牌', '台数', '玩法', '总出珠计法', '操作'].forEach((t, i) => {
    const th = el('th', i === 1 ? 'num' : '', t);
    th.scope = 'col';
    hr.appendChild(th);
  });
  thead.appendChild(hr);
  tb.appendChild(thead);

  const body = el('tbody');
  BRANDS.forEach((b) => {
    const tr = el('tr');
    const bName = el('td', 'name', b.label || b.brand);
    bName.dataset.label = '品牌';
    tr.appendChild(bName);
    const bUnits = el('td', 'num', String(b.units));
    bUnits.dataset.label = '台数';
    tr.appendChild(bUnits);
    const modeName = ({ swipe_bead_card: '刷卡出珠出卡', swipe_card: '刷卡出卡',
      bead_card: '投珠出卡', bead_out_card: '投珠出珠出卡' })[b.mode] || b.mode;
    const bMode = el('td', '', modeName);
    bMode.dataset.label = '玩法';
    tr.appendChild(bMode);
    const bIncl = el('td', '', b.out_incl === false ? '总出珠不含投币出珠' : '总出珠含投币出珠');
    bIncl.dataset.label = '总出珠计法';
    tr.appendChild(bIncl);
    const td = el('td');
    const b2 = el('button', 'btn btn--sm brand-edit', '编辑');
    b2.type = 'button';
    b2.setAttribute('aria-label', '编辑品牌 ' + (b.label || b.brand));
    b2.addEventListener('click', () => brandDialog(b));
    td.appendChild(b2);
    tr.appendChild(td);
    body.appendChild(tr);
  });
  tb.appendChild(body);
  box.appendChild(tb);
  return box;
}

function brandDialog(b) {
  const scrim = el('div', 'scrim');
  const dlg = el('div', 'dlg');
  dlg.setAttribute('role', 'dialog');
  dlg.setAttribute('aria-modal', 'true');
  dlg.appendChild(el('h2', 'dlg__t', '品牌设置'));

  const nameWrap = el('label', 'machine-cfg__f');
  nameWrap.appendChild(el('span', 'machine-cfg__k', '名称'));
  const nameIn = el('input', 'input');
  nameIn.type = 'text';
  nameIn.value = b.label || b.brand;
  nameIn.setAttribute('aria-label', '品牌名称');
  nameWrap.appendChild(nameIn);
  dlg.appendChild(nameWrap);

  const MODES = [
    ['swipe_bead_card', '刷卡出珠出卡'],
    ['swipe_card', '刷卡出卡'],
    ['bead_card', '投珠出卡'],
    ['bead_out_card', '投珠出珠出卡'],
  ];
  let mode = b.mode;
  let outIncl = b.out_incl === false ? '0' : '1';

  const mkGroup = (label, opts, cur, onPick) => {
    dlg.appendChild(el('p', 'brand-cfg__sub', label));
    const list = el('div', 'mode-list mode-list--row');
    const rs = [];
    opts.forEach(([k, text]) => {
      const f = el('label', 'mode');
      const r = el('input');
      r.type = 'radio';
      r.name = label;
      r.value = k;
      r.checked = k === cur;
      rs.push(r);
      f.appendChild(r);
      const txt = el('span', 'mode__txt');
      txt.appendChild(el('b', null, text));
      f.appendChild(txt);
      if (k === cur) f.dataset.on = 'true';
      r.addEventListener('change', () => {
        list.querySelectorAll('.mode').forEach((x) => { delete x.dataset.on; });
        if (r.checked) f.dataset.on = 'true';
        onPick(k);
      });
      list.appendChild(f);
    });
    dlg.appendChild(list);
    return rs;
  };

  mkGroup('玩法', MODES, mode, (v) => { mode = v; });
  mkGroup('总出珠怎么算', [['1', '含投币出珠'], ['0', '不含投币出珠']], outIncl,
    (v) => { outIncl = v; });
  dlg.appendChild(el('p', 'brand-cfg__sub',
    '含投币出珠：机器上显示的总出珠，把投币换出来的珠也算进去了。'
    + '不含投币出珠：总出珠里只有中奖出的珠，投币换的不算。'));

  const acts = el('div', 'dlg__acts');
  const cancel = el('button', 'btn btn--quiet', '取消');
  cancel.type = 'button';
  const closeBrand = () => { untrackDlg(closeBrand); scrim.remove(); };
  cancel.addEventListener('click', closeBrand);
  const save = el('button', 'btn btn--primary', '保存');
  save.type = 'button';
  save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      await api('/api/manage/brand/flags',
        { brand: b.brand, label: nameIn.value.trim() || b.brand, mode,
          out_incl: outIncl });
      BRANDS = null;
      closeBrand();
      await renderUnits();
      await loadEntry();
      if (S.view === 'recon') await loadRecon();
      toast('已保存');
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

function renderMachineView(card, g) {
  card.setAttribute('aria-labelledby', 'm-' + slug(g.key));

  const head = el('header', 'machine__head');
  const h = el('h3', 'machine__name', g.key);
  h.id = 'm-' + slug(g.key);
  head.appendChild(h);
  if (g.kind === 'pachinko') head.appendChild(el('span', 'machine__tag', '弹珠'));
  head.appendChild(el('span', 'machine__meta',
    g.heads + ' 个卡头'));
  if (g.merged) head.appendChild(el('span', 'machine__tag machine__tag--shared', '共用码表'));
  card.appendChild(head);

  const ul = el('ul', 'machine__comms');
  ul.setAttribute('aria-label', g.key + ' 的卡头');
  (g.pcomms || []).forEach((x) => {
    const li = el('li', 'chip chip--pos');
    li.appendChild(el('b', null, (x.p || '未标') + '：'));
    li.appendChild(el('span', null, x.comm));
    ul.appendChild(li);
  });
  card.appendChild(ul);

  const foot = el('footer', 'machine__foot');

  const edit = el('button', 'btn btn--sm', '编辑');
  edit.type = 'button';
  edit.setAttribute('aria-label', '编辑机台 ' + g.key);
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

  const del = el('button', 'btn btn--sm btn--quiet', '停用');
  del.type = 'button';
  del.setAttribute('aria-label', '停用机台 ' + g.key);
  del.addEventListener('click', async () => {
    const ok = await UI.confirm({
      title: '停用机台',
      body: '「' + g.key + '」今天起不再出现在核对页；今天已经填过数的会留最后一天，历史日期都保留。',
      okText: '停用',
    });
    if (!ok) return;
    try {
      for (const u of g.units) await api('/api/manage/unit/retire', { id: u.id });
      toast('已停用');
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

  const rowN = el('div', 'mf__row');
  rowN.appendChild(el('span', 'mf__k', '名称'));
  const nameIn = el('input', 'input mf__name');
  nameIn.type = 'text';
  nameIn.autocomplete = 'off';
  nameIn.value = g.key;
  nameIn.setAttribute('aria-label', '机台名');
  rowN.appendChild(nameIn);
  card.appendChild(rowN);

  // 品牌（只对弹珠机）：跟在名称右边，就一个正常大小的下拉，不占一整行
  let brandVal = g.brand || '';
  if ((g.kind || 'pusher') === 'pachinko') {
    rowN.appendChild(el('span', 'mf__k mf__k--inline', '品牌'));
    const wrap = el('div', 'pick mf__brand');
    const btn = el('button', 'pick__btn');
    btn.type = 'button';
    btn.setAttribute('aria-haspopup', 'listbox');
    btn.setAttribute('aria-expanded', 'false');
    btn.setAttribute('aria-label', '机台品牌');
    const valEl = el('span', 'pick__val');
    btn.appendChild(valEl);
    btn.insertAdjacentHTML('beforeend', UI.CHEV);
    const pop = el('ul', 'pick__pop');
    pop.hidden = true;
    pop.setAttribute('role', 'listbox');
    wrap.appendChild(btn);
    wrap.appendChild(pop);
    rowN.appendChild(wrap);

    const picker = UI.picker({
      btn, pop, valueEl: valEl,
      getItems: () => (BRANDS || []).map((b) => ({
        label: b.label || b.brand,
        value: b.brand,
        selected: b.brand === brandVal,
      })),
      onPick: (it) => { brandVal = it.value; picker.set(it.label); },
    });
    picker.set(brandVal || '选择品牌');
  }

  // 卡头
  const rowC = el('div', 'mf__row mf__row--top');
  rowC.appendChild(el('span', 'mf__k', '卡头'));
  const ul = el('ul', 'mf__chips');
  ul.setAttribute('aria-label', '通讯ID');
  rowC.appendChild(ul);
  card.appendChild(rowC);

  const XSvg = '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 3l6 6M9 3l-6 6" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';

  function addChip(val, p) {
    const li = el('li', 'chip chip--edit');
    li.appendChild(el('b', 'chip__p', p || '未标'));
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
      renumber();
      refreshMeta();
      const first = ul.querySelector('.chip__in');
      if (first) first.focus();
    });
    li.appendChild(x);
    ul.appendChild(li);
    return inp;
  }

  const posOf = {};
  (g.pcomms || []).forEach((x) => { posOf[String(x.comm)] = x.p; });
  g.comms.forEach((c) => addChip(c, posOf[String(c)] || ''));

  const addLi = el('li', 'chip chip--add');
  const addBtn = el('button', 'chip__add');
  addBtn.type = 'button';
  addBtn.setAttribute('aria-label', '加一个卡头');
  addBtn.innerHTML = '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 2.5v7M2.5 6h7" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';
  addBtn.addEventListener('click', () => {
    const cnt = ul.querySelectorAll('.chip--edit').length;
    const inp = addChip('', cnt + 1);
    renumber();
    refreshMeta();
    inp.focus();
  });
  addLi.appendChild(addBtn);
  ul.appendChild(addLi);

  function renumber() {
    [...ul.querySelectorAll('.chip--edit')].forEach((li, i) => {
      const b = li.querySelector('.chip__p');
      if (b) b.textContent = 'P' + (i + 1);
    });
  }

  function refreshMeta() {
    const n = ul.querySelectorAll('.chip__in').length;
    $('machineLive').textContent = '现在 ' + n + ' 个卡头';
  }

  const foot = el('footer', 'mf__acts');

  const cancel = el('button', 'btn btn--sm btn--quiet', '取消');
  cancel.type = 'button';
  cancel.addEventListener('click', () => { EDIT_KEY = null; renderUnits(); });
  foot.appendChild(cancel);

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
      const brand = (g.kind || 'pusher') === 'pachinko' ? (brandVal || '').trim() : undefined;
      if (g.units.length > 1) {
        await api('/api/manage/machine/save', {
          shop_id: MSHOP.id, ids: g.units.map((u) => u.id),
          name: nm, comms: cm.join(','), shared: cm.length > 1, brand,
        });
      } else {
        await api('/api/manage/unit/save', {
          id: g.units[0].id, shop_id: MSHOP.id,
          name: nm, comms: cm.join(','), shared: cm.length > 1, brand,
        });
      }
      BRANDS = null;
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

  // 扫机台码进来：不直达单台机，落到它所在的机台列表，再高亮那一台。
  const qsScan = new URLSearchParams(location.search);
  const scanCode = qsScan.get('c');
  const scanId = qsScan.get('u');
  let scanHit = null;
  if (scanCode || scanId) {
    try {
      scanHit = await api('/api/unit?' + (scanCode
        ? 'c=' + encodeURIComponent(scanCode)
        : 'id=' + encodeURIComponent(scanId)));
    } catch (e) {
      toast(String(e.message || e), 'bad');
    }
  }

  const b = await api('/api/bootstrap');
  S.shopId = b.shop_id;
  S.shops = b.shops;
  S.provider = ((b.shops || []).find((x) => x.id === S.shopId) || {}).provider || 'baodian';
  S.maxDay = b.max_day || S.editDay;
  S.units = b.units;
  S.bdOk = b.baodian_ok;
  S.day = b.edit_day || b.today;
  S.editDay = b.edit_day || b.today;
  S.brand = b.brand || null;
  S.brandFlags = b.brand_flags || { mode: 'swipe_bead_card' };
  S.pachFields = (b.brand_flags && b.brand_flags.fields)
    || ['in_r', 'p_tokens', 'p_out', 'p_cards'];
  S.meterUnit = (b.brand_flags && b.brand_flags.unit) || 'coin';

  const conn = $('conn');
  conn.dataset.state = S.bdOk ? 'ok' : 'bad';
  $('connText').textContent = (S.provider || 'baodian') === 'baodian'
    ? (S.bdOk ? '宝点已连接' : '宝点未连接')
    : provLabel() + '取数';

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
      S.provider = ((S.shops || []).find((x) => x.id === S.shopId) || {}).provider || 'baodian';
      shopPicker.set(it.label);
      await api('/api/shops', { name: it.label }).catch(() => {});
      const bb = await api('/api/bootstrap');
      S.units = bb.units;
      await loadEntry();
      if (S.view === 'recon') loadRecon();
      if (S.view === 'overview') loadOverview();
      refreshBdState();
    },
  });
  shopPicker.set(shop ? shop.name : '未选门店');

  dayPicker = UI.datePicker({
    btn: $('dayBtn'),
    pop: $('dayPop'),
    valueEl: $('dayVal'),
    value: S.day,
    // 门店只能填/看昨天和今天，更早更晚的日期都是灰的；总部可以往前翻历史
    min: () => (S.role === 'store' ? S.editDay : ''),
    max: () => (S.role === 'store' ? S.today : (S.maxDay || S.editDay)),
    onChange: async (v) => {
      S.day = v;
      if (S.view === 'entry') await loadEntry();
      else if (S.view === 'recon') await loadRecon();
      else if (S.view === 'overview') await loadOverview();
    },
  });

  applyRole();

  // 上次点了宝点登录跳去微信，回来后接着等结果
  try {
    const pending = sessionStorage.getItem('bdSid');
    if (pending) {
      sessionStorage.removeItem('bdSid');
      const poll = setInterval(async () => {
        try {
          const st = await api('/api/bd/login/poll', { sid: pending, shop_id: S.shopId });
          if (st.state === 'ok' && st.ok) {
            clearInterval(poll);
            toast('宝点已登录');
            await refreshBdState();
          } else if (st.state === 'fail' || st.state === 'expired') {
            clearInterval(poll);
          }
        } catch (e) { clearInterval(poll); }
      }, 2000);
      setTimeout(() => clearInterval(poll), 120000);
    }
  } catch (e) { /* 忽略 */ }
  if (!b.units_confirmed && !isPhone()) welcomeDialog(b);

  if (scanHit) {
    if (S.role === 'hq' && scanHit.shop && scanHit.shop.id) {
      S.shopId = scanHit.shop.id;
      const sp = S.shops.find((x) => x.id === S.shopId);
      shopPicker.set(sp ? sp.name : (scanHit.shop.name || ''));
    }
    S.kind = scanHit.kind;
    const firstRow = (scanHit.rows || [])[0];
    if (firstRow) S.pendingUnit = firstRow.unit_id;
    history.replaceState({ view: 'entry', nav: true }, '', location.pathname + '#entry');
  }

  // 刷新回原页：地址栏里的 #view 优先，其次是扫码进来的填写页
  let startView = (S.pendingUnit ? 'entry' : null) || viewFromHash() || 'home';
  // 手机上不提供设置页：地址栏带着 #settings 也直接回首页
  if (startView === 'settings' && isPhone()) startView = 'home';
  initHistory(startView);
  if (S.pendingUnit) {
    setView('entry', { silent: true });
    await loadEntry();
    focusUnit(S.pendingUnit);
    S.pendingUnit = null;
  } else {
    setView(startView, { silent: true });
    await loadEntry();
  }
}

async function refreshBdState() {
  const btn = $('conn');
  if (!btn) return;
  // 宝点店才有扫码登录；世软/鲸舰/纯手工由后台配置，不显示宝点状态，免得误报
  const prov = S.provider || 'baodian';
  if (prov !== 'baodian') {
    btn.dataset.state = 'ok';
    btn.disabled = true;
    $('connText').textContent = provLabel() + '取数';
    btn.title = '这家店的取数走' + provLabel();
    btn.setAttribute('aria-label', '这家店的取数走' + provLabel());
    return;
  }
  btn.disabled = false;
  try {
    const st = await api('/api/bd/status?shop_id=' + encodeURIComponent(S.shopId || ''));
    const ok = !!st.alive;
    const own = st.source === 'shop';
    btn.dataset.state = ok ? 'ok' : 'bad';
    btn.title = ok
      ? ('宝点已连接' + (own ? '（本店）' : '（总部账号）') + (st.exp_text ? '，有效到 ' + st.exp_text : ''))
      : (st.exp_text ? '宝点登录已过期（' + st.exp_text + '），点这里扫码' : '宝点未登录，点这里扫码');
    $('connText').textContent = ok
      ? (own ? '宝点已连接' : '宝点·总部')
      : '宝点登录';
    btn.setAttribute('aria-label', ok ? '宝点已连接' : '点击扫码登录宝点');
  } catch (e) { /* 忽略 */ }
}

async function bdLoginClick() {
  const btn = $('conn');
  if (btn.dataset.state === 'ok') { refreshBdState(); return; }
  btn.disabled = true;
  const old = $('connText').textContent;
  $('connText').textContent = '…';
  try {
    const r = await api('/api/bd/login/start', { shop_id: S.shopId });
    bdQrDialog(r);
  } catch (e) {
    toast(String(e.message || e), 'bad');
  } finally {
    btn.disabled = false;
    $('connText').textContent = old;
  }
}

function bdQrDialog(pre) {
  const scrim = el('div', 'scrim');
  const dlg = el('div', 'dlg bd-dlg');
  dlg.setAttribute('role', 'dialog');
  dlg.setAttribute('aria-modal', 'true');
  dlg.appendChild(el('h2', 'dlg__t', '扫码登录宝点'));
  const inWx = /MicroMessenger/i.test(navigator.userAgent);
  const tip = el('p', 'bd-dlg__tip', inWx
    ? '长按下面的二维码 →「识别图中二维码」→ 确认登录'
    : '用手机微信扫这个码，扫完这里会自动完成');
  dlg.appendChild(tip);
  const box = el('div', 'bd-dlg__qr');
  box.appendChild(el('p', 'bd-dlg__wait', '正在生成…'));
  dlg.appendChild(box);
  const acts = el('div', 'dlg__acts');
  const saveImg = el('button', 'btn btn--quiet', '保存二维码');
  saveImg.type = 'button';
  saveImg.hidden = true;
  acts.appendChild(saveImg);
  const cancel = el('button', 'btn btn--quiet', '关闭');
  cancel.type = 'button';
  acts.appendChild(cancel);
  dlg.appendChild(acts);
  scrim.appendChild(dlg);
  document.body.appendChild(scrim);

  let timer = null;
  let sid = null;
  const stop = () => { if (timer) clearInterval(timer); timer = null; };
  const close = () => { stop(); untrackDlg(close); scrim.remove(); };
  trackDlg(close);

  cancel.addEventListener('click', close);

  (async () => {
    try {
      const r = pre || await api('/api/bd/login/start', {});
      sid = r.sid;
      box.innerHTML = '';
      const img = el('img', 'bd-dlg__img');
      img.alt = '宝点登录二维码';
      img.src = /^data:/.test(r.png) ? r.png : ('data:image/png;base64,' + r.png);
      box.appendChild(img);
      saveImg.hidden = false;
      saveImg.addEventListener('click', () => {
        const a = document.createElement('a');
        a.href = img.src;
        a.download = '宝点登录二维码.png';
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        toast('已保存，用宝点 App 扫一扫 → 相册');
      });
      timer = setInterval(async () => {
        if (!sid) return;
        try {
          const st = await api('/api/bd/login/poll', { sid, shop_id: S.shopId });
          if (st.state === 'ok' && st.ok) {
            stop();
            box.innerHTML = '';
            box.appendChild(el('p', 'bd-dlg__done', '登录成功'));
            await refreshBdState();
            setTimeout(close, 800);
          } else if (st.error) {
            tip.textContent = String(st.error).slice(0, 80);
          }
        } catch (e) { /* 轮询失败不打断 */ }
      }, 2000);
    } catch (e) {
      box.innerHTML = '';
      box.appendChild(el('p', 'bd-dlg__err', String(e.message || e)));
    }
  })();

  return { close };
}

function showBootErr(msg) {
  const box = $('bootErr');
  if (!box) { alert(msg); return; }
  box.hidden = false;
  box.textContent = String(msg);
}

window.addEventListener('error', (e) => {
  showBootErr('脚本出错：' + (e.message || '') + '\n' + (e.filename || '') + ':' + (e.lineno || ''));
});
window.addEventListener('unhandledrejection', (e) => {
  showBootErr('未处理的异常：' + ((e.reason && (e.reason.message || e.reason)) || ''));
});

function applyRole() {
  const store = S.role === 'store';
  document.body.dataset.role = store ? 'store' : 'hq';
  $('tools').hidden = false;
  const navOv = document.querySelector('.nav__item[data-view="overview"]');
  if (navOv) navOv.hidden = store;   // 门店看不到全链
  const shopPick = $('shopPick');
  if (shopPick) shopPick.hidden = store;   // 门店只有一家店，不用选
  $('shopBtn').disabled = store;
  $('shopBtn').setAttribute('aria-label', store ? '当前门店' : '选择门店');
  if (store && S.view === 'overview') setView('entry');
}

document.addEventListener('DOMContentLoaded', () => {
  document.querySelectorAll('.nav__item').forEach((b) => {
    b.addEventListener('click', () => setView(b.dataset.view));
  });
  $('btnSave').addEventListener('click', () => saveDraft($('btnSave')));
  $('btnSubmit').addEventListener('click', () => submitDay($('btnSubmit')));
  $('btnToRecon').addEventListener('click', () => setView('recon'));

  $('btnExport').addEventListener('click', exportDialog);
  $('conn').addEventListener('click', bdLoginClick);
  document.querySelectorAll('#reconKind .seg__b').forEach((b) => {
    b.addEventListener('click', async () => {
      if (S.kind === b.dataset.kind) return;
      S.kind = b.dataset.kind;
      await loadRecon();
    });
  });
  $('btnConfirm').addEventListener('click', confirmUnits);
  if ($('btnBdSync')) $('btnBdSync').addEventListener('click', () => openBdSync($('btnBdSync')));
  if ($('btnJjSync')) $('btnJjSync').addEventListener('click', () => syncJjComms($('btnJjSync')));
  $('btnBackShops').addEventListener('click', loadSettings);
  $('btnScanBack').addEventListener('click', () => {
    if (HIST.ready && history.length > 1) history.back();
    else setView('home');
  });
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
    // 退出 = 连任务平台的登录态一起退掉
    localStorage.removeItem('meter_logout');
    localStorage.removeItem(TOKEN_KEY);
    localStorage.removeItem(CWSJ_KEY);
    const back = location.href.split('?')[0].split('#')[0];
    location.href = '/cwsj/?next=' + encodeURIComponent(back);
  });

  boot().catch((e) => toast(String(e.message || e), 'bad'));
});
