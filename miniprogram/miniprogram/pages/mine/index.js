const api = require('../../utils/api');
const config = require('../../config');

Page({
  data: {
    shopName: config.shopName,
    unitCount: 0,
    commCount: 0,
    units: [],
    sourceLabel: '',
    cloudMode: false,
    seeding: false,
    seedMsg: '',
  },

  onShow() {
    this.setData({ cloudMode: api.isCloud() });
    this.load();
  },

  async load() {
    try {
      const units = await api.getUnits();
      let comm = 0;
      units.forEach((u) => { comm += (u.comms || []).length; });
      this.setData({
        unitCount: units.length,
        commCount: comm,
        units: units.map((u) => ({
          id: u.id,
          name: u.name,
          meta: (u.team || '-') + ' · ' + (u.comms || []).length + ' 通讯ID' + (u.shared ? ' · 共用' : ''),
        })),
        sourceLabel: api.isCloud() ? '云数据库' : '本地演示数据',
      });
    } catch (e) {
      wx.showToast({ title: '读取失败', icon: 'none' });
    }
  },

  async onSeed() {
    if (this.data.seeding) return;
    this.setData({ seeding: true, seedMsg: '正在初始化' });
    try {
      const r = await api.seedCloud();
      if (r && r.ok) {
        this.setData({ seedMsg: '已写入 ' + r.units + ' 个单元、' + r.snapshots + ' 天快照' });
        await this.load();
        wx.showToast({ title: '初始化完成', icon: 'success' });
      } else {
        this.setData({ seedMsg: '失败：' + ((r && r.msg) || '无返回') });
      }
    } catch (e) {
      this.setData({ seedMsg: '失败：' + String(e && e.message ? e.message : e).slice(0, 80) });
    } finally {
      this.setData({ seeding: false });
    }
  },
});
