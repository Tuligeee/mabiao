// 数据访问层
// 云环境有数据就用云数据；没数据或出错就退回 mock（鄂州店真实数据），并在页面标出来源。
const config = require('../config');
const mock = require('../mock/data');

const isCloud = () => !!(config.useCloud && wx.cloud);

function db() {
  return wx.cloud.database();
}

async function fetchAll(coll, where, order) {
  let out = [];
  let skip = 0;
  for (;;) {
    let q = db().collection(coll).where(where);
    if (order) q = q.orderBy(order.field, order.dir);
    const res = await q.skip(skip).limit(20).get();
    out = out.concat(res.data);
    if (res.data.length < 20) break;
    skip += 20;
    if (skip > 2000) break;
  }
  return out;
}

function friendlyErr(e) {
  const s = String((e && e.errMsg) || (e && e.message) || e || '');
  if (s.indexOf('601034') >= 0 || s.indexOf('没有权限') >= 0) return '云开发未开通';
  if (s.indexOf('env') >= 0 && s.indexOf('not exist') >= 0) return '云环境不存在';
  if (s.indexOf('collection not exists') >= 0 || s.indexOf('DATABASE_COLLECTION_NOT_EXIST') >= 0) {
    return '云数据库还没建集合';
  }
  return s.slice(0, 90);
}

// 演示数据只有固定的几天；请求别的日子时，退到最近的那一天
function nearestMockDay(day) {
  const keys = Object.keys(mock.entry || {}).sort();
  if (!keys.length) return null;
  if (mock.entry[day]) return day;
  const before = keys.filter((k) => k <= day);
  return before.length ? before[before.length - 1] : keys[keys.length - 1];
}

function mockSnapshot(day, source) {
  const demoDay = nearestMockDay(day) || day;
  const rows = (mock.entry[demoDay] || []).map((r) => {
    const u = mock.units.find((x) => x.id === r.unit_id) || {};
    return {
      unit_id: r.unit_id,
      name: r.name,
      prev_in: r.prev_in,
      prev_out: r.prev_out,
      in_r: r.in_r,
      out_r: r.out_r,
      shared: r.shared,
      comm_count: (u.comms || []).length,
    };
  });
  const rc = mock.recon[demoDay] || { summary: {}, rows: [] };
  return {
    day,
    demo_day: demoDay,
    shop_id: config.shopId,
    shop_name: config.shopName,
    rows,
    recon: rc.rows,
    summary: rc.summary,
    source: source || 'mock',
  };
}

async function getSnapshot(day) {
  if (!isCloud()) return mockSnapshot(day, 'mock');
  try {
    const res = await db().collection('snapshot')
      .where({ shop_id: config.shopId, day }).limit(1).get();
    if (!res.data.length) return mockSnapshot(day, 'fallback');
    const d = res.data[0];
    if (!d.rows || !d.rows.length) return mockSnapshot(day, 'fallback');
    d.source = 'cloud';
    return d;
  } catch (e) {
    const s = mockSnapshot(day, 'error');
    s.err = friendlyErr(e);
    return s;
  }
}

async function getDays() {
  if (!isCloud()) return mock.days.slice().reverse();
  try {
    const res = await db().collection('snapshot')
      .where({ shop_id: config.shopId })
      .orderBy('day', 'desc').limit(30).get();
    if (!res.data.length) return mock.days.slice().reverse();
    return res.data.map((d) => d.day);
  } catch (e) {
    return mock.days.slice().reverse();
  }
}

async function saveReadings(day, items) {
  if (!isCloud()) return { saved: items.length, mock: true };
  const coll = db().collection('readings');
  const now = Date.now();
  let n = 0;
  for (const it of items) {
    const res = await coll.where({
      shop_id: config.shopId, unit_id: it.unit_id, day,
    }).limit(1).get();
    const body = {
      shop_id: config.shopId,
      unit_id: it.unit_id,
      day,
      in_r: it.in_r,
      out_r: it.out_r,
      updated_at: now,
    };
    if (res.data.length) {
      await coll.doc(res.data[0]._id).update({ data: body });
    } else {
      await coll.add({ data: body });
    }
    n += 1;
  }
  return { saved: n, mock: false };
}

async function getUnits() {
  if (!isCloud()) {
    return mock.units.map((u) => ({
      id: u.id, name: u.name, team: u.team, shared: u.shared, comms: u.comms,
    }));
  }
  try {
    const rows = await fetchAll('units', { shop_id: config.shopId });
    if (!rows.length) throw new Error('empty');
    return rows.map((u) => ({
      id: u._id, name: u.name, team: u.team, shared: u.shared, comms: u.comms || [],
    }));
  } catch (e) {
    return mock.units.map((u) => ({
      id: u.id, name: u.name, team: u.team, shared: u.shared, comms: u.comms,
    }));
  }
}

// 一次性把演示数据灌进云数据库（调用云函数 seed）
async function seedCloud() {
  if (!isCloud()) return { ok: false, msg: '未开启云模式' };
  const r = await wx.cloud.callFunction({ name: 'seed' });
  return (r && r.result) || { ok: false, msg: '无返回' };
}

module.exports = { isCloud, getSnapshot, getDays, getUnits, saveReadings, seedCloud };
