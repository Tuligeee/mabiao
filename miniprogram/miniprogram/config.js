// config.js
module.exports = {
  // true = 走云开发；false = 纯演示数据（不连云）
  useCloud: true,
  // 云开发环境 ID。留空 = 用账号的默认环境（只有一个环境时不用填）。
  env: '',
  // 云环境里没数据时，自动退回这份演示数据，并在页面标出来源
  mockDay: '2026-11-07',
  // 门店（本期单店；多店时由 users 集合决定）
  shopId: '3be437fd-d035-4bd8-bb71-15ff31c9c858',
  shopName: '纯玩世界鄂州吾悦店',
  // 彩票 → 币 换算率
  rate: 10,
};
