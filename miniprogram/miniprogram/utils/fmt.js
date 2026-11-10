// 数字格式化：千分位，空值显示短横
function n(v) {
  if (v === null || v === undefined || v === '') return '-';
  const x = Math.round(Number(v));
  if (!isFinite(x)) return '-';
  return String(x).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

function n1(v) {
  if (v === null || v === undefined || v === '') return '-';
  const x = Number(v);
  if (!isFinite(x)) return '-';
  const r = Math.round(x * 10) / 10;
  return String(r).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

function signed(v) {
  if (v === null || v === undefined || v === '') return '';
  const x = Math.round(Number(v));
  if (!isFinite(x)) return '';
  return (x >= 0 ? '+' : '-') + n(Math.abs(x));
}

module.exports = { n, n1, signed };
