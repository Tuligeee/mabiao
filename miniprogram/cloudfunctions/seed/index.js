// 一次性初始化：建集合 + 写入 units / snapshot
// 幂等：用固定 _id 的 set()，重复跑只会覆盖，不会重复插入
const cloud = require('wx-server-sdk');
cloud.init({ env: cloud.DYNAMIC_CURRENT_ENV });

const db = cloud.database();
const data = require('./data.json');

async function ensure(name) {
  try {
    await db.createCollection(name);
    return 'created';
  } catch (e) {
    return 'exists';
  }
}

exports.main = async () => {
  const log = {};
  for (const c of ['units', 'snapshot', 'readings']) {
    log[c] = await ensure(c);
  }

  let u = 0;
  for (const doc of data.units) {
    const id = doc._id;
    const body = Object.assign({}, doc);
    delete body._id;
    await db.collection('units').doc(id).set({ data: body });
    u += 1;
  }

  let n = 0;
  for (const doc of data.snapshots) {
    const id = doc._id;
    const body = Object.assign({}, doc);
    delete body._id;
    await db.collection('snapshot').doc(id).set({ data: body });
    n += 1;
  }

  return { ok: true, collections: log, units: u, snapshots: n, days: data.snapshots.map((x) => x.day) };
};
