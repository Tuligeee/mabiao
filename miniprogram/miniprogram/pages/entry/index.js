const api = require('../../utils/api');
const fmt = require('../../utils/fmt');
const config = require('../../config');

function iso(d) {
  const p = (x) => (x < 10 ? '0' + x : '' + x);
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
}

function srcLabel(src) {
  const m = {
    mock: '演示数据',
    cloud: '云数据',
    fallback: '云环境无数据 · 演示',
    error: '云读取失败 · 演示',
  };
  return m[src] || '';
}

Page({
  data: {
    shopName: config.shopName,
    day: '',
    maxDay: '',
    rows: [],
    warnCount: 0,
    saving: false,
    hint: '',
    sourceLabel: '',
    cloudErr: '',
  },

  onLoad() {
    const today = new Date();
    const maxDay = iso(today);
    const day = iso(new Date(today.getTime() - 86400000));
    this.setData({ maxDay, day });
    this.load(day);
  },

  async load(day) {
    wx.showLoading({ title: '读取中', mask: true });
    try {
      const snap = await api.getSnapshot(day);
      const rows = (snap.rows || []).map((r) => this.decorate(r));
      const shownDay = snap.demo_day || day;
      let label = srcLabel(snap.source);
      if (snap.demo_day && snap.demo_day !== day) label += ' · ' + snap.demo_day;
      this.setData({
        day: shownDay,
        rows,
        warnCount: rows.filter((r) => r.warn).length,
        sourceLabel: label,
        cloudErr: snap.err || '',
        hint: '',
      });
    } catch (e) {
      wx.showToast({ title: '读取失败', icon: 'none' });
      this.setData({ hint: String(e && e.message ? e.message : e).slice(0, 80) });
    } finally {
      wx.hideLoading();
    }
  },

  decorate(r) {
    const hasPrev = r.prev_in !== null && r.prev_in !== undefined;
    const dIn = hasPrev && r.in_r !== null && r.in_r !== undefined ? r.in_r - r.prev_in : null;
    const hasPrevOut = r.prev_out !== null && r.prev_out !== undefined;
    const dOut = hasPrevOut && r.out_r !== null && r.out_r !== undefined ? r.out_r - r.prev_out : null;
    const meta = [];
    if (r.comm_count) meta.push(r.comm_count + ' 个通讯ID');
    if (r.shared) meta.push('四联机共用');
    return {
      unit_id: r.unit_id,
      name: r.name,
      meta: meta.join(' · '),
      prev_in: r.prev_in,
      prev_out: r.prev_out,
      in_r: r.in_r,
      out_r: r.out_r,
      prevText: hasPrev ? fmt.n(r.prev_in) + ' → ' + fmt.n(r.prev_out) : '无（首日）',
      dIn,
      dOut,
      dInText: fmt.signed(dIn),
      dOutText: fmt.signed(dOut),
      warn: (dIn !== null && dIn < 0) || (dOut !== null && dOut < 0),
    };
  },

  onDayChange(e) {
    this.load(e.detail.value);
  },

  onInput(e) {
    const i = Number(e.currentTarget.dataset.i);
    const f = e.currentTarget.dataset.f;
    const raw = String(e.detail.value || '').replace(/[,\s]/g, '');
    const v = raw === '' ? null : Number(raw);
    const rows = this.data.rows.slice();
    const row = Object.assign({}, rows[i]);
    row[f] = raw !== '' && isFinite(v) ? v : null;
    rows[i] = this.decorate(row);
    this.setData({ rows, warnCount: rows.filter((r) => r.warn).length });
  },

  async onSave() {
    if (this.data.saving) return;
    const items = this.data.rows
      .filter((r) => r.in_r !== null || r.out_r !== null)
      .map((r) => ({ unit_id: r.unit_id, in_r: r.in_r, out_r: r.out_r }));
    if (!items.length) {
      wx.showToast({ title: '还没填读数', icon: 'none' });
      return;
    }
    this.setData({ saving: true, hint: '保存中' });
    try {
      const res = await api.saveReadings(this.data.day, items);
      if (res.mock) {
        this.setData({ hint: '演示模式：未写库。配好云环境后才会真正保存。' });
        wx.showToast({ title: '演示模式', icon: 'none' });
      } else {
        this.setData({ hint: '已保存 ' + res.saved + ' 台，次日出核对结果' });
        wx.showToast({ title: '已保存', icon: 'success' });
      }
    } catch (e) {
      this.setData({ hint: '保存失败：' + String(e && e.message ? e.message : e).slice(0, 60) });
      wx.showToast({ title: '保存失败', icon: 'none' });
    } finally {
      this.setData({ saving: false });
    }
  },
});
