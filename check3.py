# -*- coding: utf-8 -*-
"""Are 卡头数据 G/L/M cumulative or daily? Compare with 码表 deltas."""
import io
from collections import defaultdict, Counter
from openpyxl import load_workbook

path = r"C:\Users\Tuilge\Desktop\资产核对表-鄂州店.xlsx"
out = r"C:\Users\Tuilge\Desktop\码表线上化\check3.txt"
wbv = load_workbook(path, data_only=True)
buf = io.StringIO()
def P(*a): print(*a, file=buf)

s0 = wbv["推币机收币误差表"]
s1 = wbv["卡头数据"]

P("### 卡头数据 left block time series for a few commids (数量 G)")
series = defaultdict(dict)   # commid -> date -> (数量, P位, 机台)
for r in range(2, s1.max_row + 1):
    d = s1.cell(row=r, column=1).value
    c = s1.cell(row=r, column=3).value
    g = s1.cell(row=r, column=7).value
    if d is None or c is None: continue
    series[c][d.strftime("%Y-%m-%d")] = (g, s1.cell(row=r, column=4).value, s1.cell(row=r, column=2).value)

for c in [758516, 733720, 733676, 733577, 733573]:
    P(f"\n  commid {c}:")
    for k in sorted(series[c])[:8]:
        P(f"    {k}: 数量={series[c][k][0]} {series[c][k][2]} {series[c][k][1]}")

P("\n### 卡头数据 right block time series (L 总本币数, M 总赠币数) by commid")
rseries = defaultdict(dict)
for r in range(2, s1.max_row + 1):
    d = s1.cell(row=r, column=1).value
    i = s1.cell(row=r, column=9).value
    if d is None or i is None: continue
    rseries[i][d.strftime("%Y-%m-%d")] = (s1.cell(row=r, column=12).value,
                                          s1.cell(row=r, column=13).value,
                                          s1.cell(row=r, column=8).value,
                                          s1.cell(row=r, column=10).value)
for c in [758516, 733720, 733676, 733577, 733573]:
    P(f"\n  commid {c}:")
    for k in sorted(rseries[c])[:8]:
        P(f"    {k}: L={rseries[c][k][0]} M={rseries[c][k][1]} {rseries[c][k][2]} {rseries[c][k][3]}")

P("\n### per machine-day: 码表出币 vs sum(数量)/10  ratio")
# sheet0 machine-day aggregates
agg = defaultdict(lambda: {"in": 0, "out": 0})
for r in range(2, s0.max_row + 1):
    d = s0.cell(row=r, column=1).value
    m = s0.cell(row=r, column=2).value
    if d is None or m is None: continue
    k = (d.strftime("%Y-%m-%d"), m)
    i = s0.cell(row=r, column=9).value
    j = s0.cell(row=r, column=10).value
    if isinstance(i, (int, float)): agg[k]["in"] += i
    if isinstance(j, (int, float)): agg[k]["out"] += j

# 卡头 left block machine-day sum 数量
kagg = defaultdict(float)
for r in range(2, s1.max_row + 1):
    d = s1.cell(row=r, column=1).value
    m = s1.cell(row=r, column=2).value
    g = s1.cell(row=r, column=7).value
    if d is None or m is None or not isinstance(g, (int, float)): continue
    kagg[(d.strftime("%Y-%m-%d"), m)] += g

P("  date | machine | 码表入 | 码表出 | sum数量 | 数量/码表出")
for k in sorted(agg)[:24]:
    a = agg[k]
    q = kagg.get(k)
    ratio = (q / a["out"]) if q and a["out"] else None
    P(f"  {k[0]} | {k[1]} | {a['in']} | {a['out']} | {q} | {ratio}")

P("\n### ratio stats 数量/码表出 by machine")
rs = defaultdict(list)
for k, a in agg.items():
    q = kagg.get(k)
    if q and a["out"]:
        rs[k[1]].append(q / a["out"])
for m, v in rs.items():
    v2 = sorted(v)
    P(f"  {m}: n={len(v2)} min={v2[0]:.2f} p50={v2[len(v2)//2]:.2f} max={v2[-1]:.2f}")

P("\n### per machine-day: 码表收币 vs (L+M) - 数量/10")
lagg = defaultdict(lambda: [0.0, 0.0])
for r in range(2, s1.max_row + 1):
    d = s1.cell(row=r, column=1).value
    if d is None: continue
    k = d.strftime("%Y-%m-%d")
    for c, idx in ((12, 0), (13, 0)):
        pass
# right block sums per machine-day
ragg = defaultdict(lambda: {"L": 0.0, "M": 0.0})
for r in range(2, s1.max_row + 1):
    d = s1.cell(row=r, column=1).value
    m = s1.cell(row=r, column=8).value
    if d is None or m is None: continue
    k = (d.strftime("%Y-%m-%d"), m)
    l = s1.cell(row=r, column=12).value
    mm = s1.cell(row=r, column=13).value
    if isinstance(l, (int, float)): ragg[k]["L"] += l
    if isinstance(mm, (int, float)): ragg[k]["M"] += mm

P("  date | machine | 码表收币 | sumL+sumM | sum数量/10 | 卡头收币 | 差")
for k in sorted(agg)[:20]:
    a = agg[k]
    rr = ragg.get(k, {"L": 0, "M": 0})
    q = kagg.get(k, 0) / 10
    net = a["in"] - a["out"]
    kh = rr["L"] + rr["M"] - q
    P(f"  {k[0]} | {k[1]} | {net} | {rr['L']+rr['M']} | {q:.1f} | {kh:.1f} | {kh-net:.1f}")

with open(out, "w", encoding="utf-8") as f:
    f.write(buf.getvalue())
print("written", out)
