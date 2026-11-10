// app.js
const config = require('./config');

App({
  globalData: {
    env: config.env,
    shopId: config.shopId,
    shopName: config.shopName,
    rate: config.rate,
  },
  onLaunch() {
    if (!config.useCloud || !wx.cloud) return;
    const opt = { traceUser: true };
    if (config.env) opt.env = config.env;
    wx.cloud.init(opt);
  },
});
