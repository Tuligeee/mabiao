# -*- coding: utf-8 -*-
"""用宝点两个接口取 2026-11-07 的上币/下币，和 Excel 里同一天的数字逐行对账。

上币 = /reports/report/shop/coinoutplay/list            (机台消费币汇总)
下币 = /terminals/backend/sport/get/sport_integral_summary/list  (机台获分记录-按设备汇总)
"""
import io, sys
sys.path.insert(0, r"C:\Users\Tuilge\Desktop\码表线上化\_probe")
from bdclient import Api, find_shop          # noqa: E402
from openpyxl import load_workbook

DAY = "2026-11-07"
XLSX = r"C:\Users\Tuilge\Desktop\资产核对表-鄂州店.xlsx"
OUT = r"C:\Users\Tuilge\Desktop\码表线上化\api_vs_excel.txt"

buf = io.StringIO()
def P(*a): print(*a, file=buf)

sh = find_shop()
api = Api(sh)

# ---------- 上币 ----------
st1, up = api.post("/reports/report/shop/coinoutplay/list", {
    "paging": {"page": 0, "limit": 1000, "sort": "asc", "sortField": "CreateTime",
               "filters": [{"field": "CreateTime", "value": f"{DAY} ~ {DAY}",
                            "operator": 0, "relation": 0, "fieldType": 1}]},
    "memberLevelIds": []})
P(f"上币接口 {st1} success={up.get('success')} count={up.get('count')}")

# ---------- 下币 ----------
st2, down = api.post("/terminals/backend/sport/get/sport_integral_summary/list", {
    "page": 0, "limit": 1000, "sortField": "CreateTime", "sort": "desc",
    "filters": [{"field": "CreateTime", "value": f"{DAY} ~ {DAY}",
                 "operator": 0, "relation": 0, "fieldType": 1}]})
P(f"下币接口 {st2} success={down.get('success')} count={down.get('count')}")

UP = {}
for r in up.get("listData") or []:
    c = str(r.get("commId") or "")
    if c:
        UP[c] = (r.get("deviceName"), r.get("projectTeamName"),
                 r.get("actualNativeCoinNum") or 0, r.get("actualGiveCoinNum") or 0,
                 r.get("giveCoinA") or 0, r.get("giveCoinB") or 0)

DOWN = {}
for r in down.get("listData") or []:
    c = str(r.get("commId") or "")
    if c:
        prev = DOWN.get(c, 0)
        DOWN[c] = prev + (r.get("integralQuantity") or 0)

P(f"\n上币行数={len(UP)}  下币行数={len(DOWN)}（已按通讯ID汇总）")
P("下币样例:", list(DOWN.items())[:5])
P("上币样例:", list(UP.items())[:3])

# ---------- Excel 侧 ----------
wb = load_workbook(XLSX, data_only=True)
s0, s1 = wb["推币机收币误差表"], wb["卡头数据"]
def dt(v): return v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else None

# 当天 通讯ID -> (机台名称, P位)
comm_map = {}
for r in range(2, s1.max_row + 1):
    if dt(s1.cell(row=r, column=1).value) != DAY:
        continue
    c = s1.cell(row=r, column=9).value
    if c:
        comm_map[str(c)] = (s1.cell(row=r, column=8).value, s1.cell(row=r, column=10).value)
P(f"\nExcel 当天通讯ID: {len(comm_map)} 个")

# Excel 主表当天 (机台, P位) -> F/G
xl = {}
for r in range(2, s0.max_row + 1):
    if dt(s0.cell(row=r, column=1).value) != DAY:
        continue
    m, p = s0.cell(row=r, column=2).value, s0.cell(row=r, column=3).value
    xl[(m, p)] = (s0.cell(row=r, column=6).value, s0.cell(row=r, column=7).value)
P(f"Excel 主表当天行数: {len(xl)}")

P("\n" + "=" * 96)
P(f"{'机台':<10}{'P位':<4}{'通讯ID':<9}{'Excel上币':>10}{'API上币':>9}{'差':>6}"
  f"{'Excel下币':>11}{'API下币':>10}{'差':>7}")
P("=" * 96)
ok_up = ok_down = bad_up = bad_down = 0
for (m, p), (f_x, g_x) in sorted(xl.items()):
    c = next((k for k, v in comm_map.items() if v == (m, p)), None)
    if not c:
        P(f"{m:<10}{p:<4}{'—':<9}  找不到通讯ID")
        continue
    u = UP.get(c)
    up_sum = (u[2] + u[3]) if u else None
    dn = DOWN.get(c)
    dn_coin = (dn / 10) if dn is not None else None
    d1 = "" if up_sum is None else up_sum - (f_x or 0)
    d2 = "" if dn_coin is None else dn_coin - (g_x or 0)
    if up_sum is not None and abs(d1) < 0.5:
        ok_up += 1
    elif up_sum is not None:
        bad_up += 1
    if dn_coin is not None and abs(d2) < 0.5:
        ok_down += 1
    elif dn_coin is not None:
        bad_down += 1
    P(f"{m:<10}{p:<4}{c:<9}{str(f_x):>10}{str(up_sum):>9}{str(d1):>6}"
      f"{str(g_x):>11}{str(dn_coin):>10}{str(d2):>7}")

P("=" * 96)
P(f"上币对上 {ok_up} 行 / 对不上 {bad_up} 行；下币对上 {ok_down} 行 / 对不上 {bad_down} 行")

# 机器级合计（4联机）
P("\n机器级合计（Excel H 与 API 上币-下币）:")
agg_xl = {}
for (m, p), (f_x, g_x) in xl.items():
    a = agg_xl.setdefault(m, [0, 0])
    a[0] += f_x or 0
    a[1] += g_x or 0
for m, (f_s, g_s) in agg_xl.items():
    cs = [k for k, v in comm_map.items() if v[0] == m]
    up_s = sum((UP[c][2] + UP[c][3]) for c in cs if c in UP)
    dn_s = sum((DOWN[c] / 10) for c in cs if c in DOWN)
    P(f"  {m}: Excel 上币={f_s} / API={up_s}   |  Excel 下币={g_s:.0f} / API={dn_s:.0f}"
      f"   | 收币 Excel={f_s - g_s:.0f} API={up_s - dn_s:.0f}")

with open(OUT, "w", encoding="utf-8") as f:
    f.write(buf.getvalue())
print("written", OUT)
