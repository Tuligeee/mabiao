# -*- coding: utf-8 -*-
"""Simulate the workbook's formulas in Python and compare against cached values."""
import io
from collections import defaultdict, Counter
from openpyxl import load_workbook

path = r"C:\Users\Tuilge\Desktop\资产核对表-鄂州店.xlsx"
out = r"C:\Users\Tuilge\Desktop\码表线上化\simulate.txt"
wbv = load_workbook(path, data_only=True)
buf = io.StringIO()
def P(*a): print(*a, file=buf)

s0 = wbv["推币机收币误差表"]
s1 = wbv["卡头数据"]

def dt(v):
    return v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else None

# 卡头数据 indexes
qty = {}     # (date, 机台, P位) -> 数量
for r in range(2, s1.max_row + 1):
    d = dt(s1.cell(row=r, column=1).value)
    m = s1.cell(row=r, column=2).value
    p = s1.cell(row=r, column=4).value
    g = s1.cell(row=r, column=7).value
    if d and m and p and isinstance(g, (int, float)):
        qty.setdefault((d, m, p), g)

coin = {}    # (date, 机台名称, P位) -> 总本币数+总赠币数
for r in range(2, s1.max_row + 1):
    d = dt(s1.cell(row=r, column=1).value)
    m = s1.cell(row=r, column=8).value
    p = s1.cell(row=r, column=10).value
    l = s1.cell(row=r, column=12).value
    mm = s1.cell(row=r, column=13).value
    if d and m and p:
        l = l if isinstance(l, (int, float)) else 0
        mm = mm if isinstance(mm, (int, float)) else 0
        coin.setdefault((d, m, p), l + mm)

# sheet0 rows
rows = []
prev = {}
for r in range(2, s0.max_row + 1):
    d = s0.cell(row=r, column=1).value
    m = s0.cell(row=r, column=2).value
    p = s0.cell(row=r, column=3).value
    if d is None or m is None: continue
    D = s0.cell(row=r, column=4).value
    E = s0.cell(row=r, column=5).value
    D = D if isinstance(D, (int, float)) else None
    E = E if isinstance(E, (int, float)) else None
    rows.append({"r": r, "date": dt(d), "m": m, "p": p, "D": D, "E": E})

# simulate I/J (delta vs previous day same machine/P)
for row in rows:
    key = (row["m"], row["p"])
    pv = prev.get(key)
    if pv and row["D"] is not None and pv[0] is not None:
        row["I"] = row["D"] - pv[0]
    else:
        row["I"] = 0
    if pv and row["E"] is not None and pv[1] is not None:
        row["J"] = row["E"] - pv[1]
    else:
        row["J"] = 0
    row["K"] = (row["I"] or 0) - (row["J"] or 0)
    prev[key] = (row["D"], row["E"])

# simulate F/G
for row in rows:
    row["F"] = coin.get((row["date"], row["m"], row["p"]), 0)
    q = qty.get((row["date"], row["m"], row["p"]))
    row["G"] = (q / 10) if isinstance(q, (int, float)) else 0
    row["H"] = row["F"] - row["G"]

# simulate L: aggregate machines
AGG = {"疯狂魔鬼城", "凤凰城", "法老也疯狂"}
idx = {row["r"]: i for i, row in enumerate(rows)}
for row in rows:
    if row["m"] in AGG:
        if row["p"] != "P1":
            row["L"] = 0
        else:
            i = idx[row["r"]]
            block = rows[i:i + 4]
            row["L"] = sum(b["H"] for b in block) - sum(b["K"] for b in block)
    else:
        row["L"] = row["H"] - row["K"]
    row["M"] = (row["L"] / row["K"]) if row["K"] else None

P("### cached vs simulated (first 20 rows)")
P("  r | date | machine | P | F_cached | F_sim | G_cached | G_sim | K_cached | K_sim | L_cached | L_sim")
for row in rows[:20]:
    r = row["r"]
    P(f"  {r} | {row['date']} | {row['m']} | {row['p']} | "
      f"{s0.cell(row=r,column=6).value} | {row['F']} | "
      f"{s0.cell(row=r,column=7).value} | {row['G']:.0f} | "
      f"{s0.cell(row=r,column=11).value} | {row['K']:.0f} | "
      f"{s0.cell(row=r,column=12).value} | {row['L']:.0f}")

# mismatch stats
mG = sum(1 for row in rows if abs((s0.cell(row=row["r"], column=7).value or 0) - row["G"]) > 0.5)
mK = sum(1 for row in rows if abs((s0.cell(row=row["r"], column=11).value or 0) - row["K"]) > 0.5)
mF = sum(1 for row in rows if abs((s0.cell(row=row["r"], column=6).value or 0) - row["F"]) > 0.5)
mI = sum(1 for row in rows if abs((s0.cell(row=row["r"], column=9).value or 0) - row["I"]) > 0.5)
mJ = sum(1 for row in rows if abs((s0.cell(row=row["r"], column=10).value or 0) - row["J"]) > 0.5)
P(f"\n### mismatches: F={mF} G={mG} I={mI} J={mJ} K={mK} of {len(rows)} rows")

P("\n### simulated totals per machine (sum L, count rows)")
agg = defaultdict(lambda: [0.0, 0, 0.0])
for row in rows:
    a = agg[row["m"]]
    a[0] += row["L"]
    a[1] += 1
    a[2] += row["F"]
for m, a in agg.items():
    P(f"  {m}: sumL={a[0]:.0f} rows={a[1]} sumF={a[2]:.0f}")

P("\n### simulated 误差 distribution: |L| buckets")
b = defaultdict(int)
for row in rows:
    v = abs(row["L"])
    if v == 0: b["0"] += 1
    elif v < 100: b["<100"] += 1
    elif v < 1000: b["100-1000"] += 1
    elif v < 10000: b["1k-10k"] += 1
    else: b[">10k"] += 1
P("  " + str(dict(b)))

P("\n### top 15 |L| with simulated values")
for row in sorted(rows, key=lambda x: -abs(x["L"]))[:15]:
    P(f"  {row['date']} {row['m']} {row['p']}: F={row['F']:.0f} G={row['G']:.0f} H={row['H']:.0f} "
      f"K={row['K']:.0f} L={row['L']:.0f}")

P("\n### 卡头数据 coverage: keys present but 数量 missing, and vice versa")
missing_q = [k for k in coin if k not in qty]
missing_c = [k for k in qty if k not in coin]
P(f"  coin keys={len(coin)} qty keys={len(qty)} coin-without-qty={len(missing_q)} qty-without-coin={len(missing_c)}")
P("  sample coin-without-qty:", missing_q[:8])

P("\n### sheet0 keys not found in 卡头数据 at all")
miss = [(row["date"], row["m"], row["p"]) for row in rows
        if (row["date"], row["m"], row["p"]) not in qty and (row["date"], row["m"], row["p"]) not in coin]
P(f"  {len(miss)} of {len(rows)}")
P("  by date:", dict(Counter(x[0] for x in miss).most_common(10)))
P("  by machine:", dict(Counter(x[1] for x in miss)))

with open(out, "w", encoding="utf-8") as f:
    f.write(buf.getvalue())
print("written", out)
