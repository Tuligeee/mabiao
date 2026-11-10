const api = require('../../utils/api');
const fmt = require('../../utils/fmt');
const config = require('../../config');

const TONE = {
  '正常': 'tag--ok',
  '首日': 'tag--warn',
  '读数下降': 'tag--bad',
  '缺码表': 'tag--warn',
  '缺卡头': 'tag--warn',
};

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
    summary: { ok: 0, diff_ok: 0, diff_all: 0, big: 0 },
    diffOk: '0',
    diffAll: '0',
    rows: [],
    sourceLabel: '',
  },

  onLoad() {
    const today = new Date();
    this.setData({ maxDay: iso(today) });
    this.load(iso(new Date(today.getTime() - 86400000)));
  },

  onShow() {
    if (this.data.day) this.load(this.data.day);
  },

  async load(day) {
    wx.showLoading({ title: '读取中', mask: true });
    try {
      const snap = await api.getSnapshot(day);
      const s = snap.summary || {};
      const shownDay = snap.demo_day || day;
      const rows = (snap.recon || []).map((r) => ({
        name: r.name,
        upText: fmt.n(r.up),
        downText: fmt.n(r.down),
        meterText: fmt.n(r.meter),
        diffText: fmt.n(r.diff),
        status: r.status,
        tone: TONE[r.status] || '',
        bad: r.diff !== null && r.diff !== undefined && Math.abs(r.diff) >= 1000,
      }));
      this.setData({
        day: shownDay,
        summary: s,
        diffOk: fmt.n1(s.diff_ok),
        diffAll: fmt.n1(s.diff_all),
        rows,
        sourceLabel: (srcLabel(snap.source) + (snap.demo_day && snap.demo_day !== day ? ' · ' + snap.demo_day : '')),
      });
    } catch (e) {
      wx.showToast({ title: '读取失败', icon: 'none' });
    } finally {
      wx.hideLoading();
    }
  },

  onDayChange(e) {
    this.load(e.detail.value);
  },
});
