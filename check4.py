# -*- coding: utf-8 -*-
"""Final specifics: outliers, sparse 码表 days, reconcile quality."""
import io
from collections import defaultdict
from openpyxl import load_workbook

path = r"C:\Users\Tuilge\Desktop\资产核对表-鄂州店.xlsx"
out = r"C:\Users\Tuilge\Desktop\码表线上化\check4.txt"
wbv = load_workbook(path, data_only=True)
buf = io.StringIO()
def P(*a): print(*a, file=buf)
s0 = wbv["推币机收币误差表"]
s1 = wbv["卡头数据"]

def dt(v): return v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else None

P("### 2026-10-27..10-30 码表 readings (D/E) for 凤凰城 & 法老也疯狂")
for r in range(2, s0.max_row + 1):
    d = dt(s0.cell(row=r, column=1).value)
    if d and "2026-10-27" <= d <= "2026-10-30":
        m = s0.cell(row=r, column=2).value
        if m in ("凤凰城", "法老也疯狂"):
            P(f"  R{r} {d} {m} {s0.cell(row=r,column=3).value} D={s0.cell(row=r,column=4).value} "
              f"E={s0.cell(row=r,column=5).value} I={s0.cell(row=r,column=9).value} "
              f"J={s0.cell(row=r,column=10).value} K={s0.cell(row=r,column=11).value}")

P("\n### 2026-09-10..09-14 rows: which P位 have 码表 D/E")
for r in range(2, s0.max_row + 1):
    d = dt(s0.cell(row=r, column=1).value)
    if d and "2026-09-10" <= d <= "2026-09-14":
        P(f"  R{r} {d} {s0.cell(row=r,column=2).value} {s0.cell(row=r,column=3).value} "
          f"D={s0.cell(row=r,column=4).value} E={s0.cell(row=r,column=5).value}")

P("\n### 2026-11-01..11-07 sample of last rows")
for r in range(s0.max_row - 16, s0.max_row + 1):
    P(f"  R{r} {dt(s0.cell(row=r,column=1).value)} {s0.cell(row=r,column=2).value} "
      f"{s0.cell(row=r,column=3).value} D={s0.cell(row=r,column=4).value} E={s0.cell(row=r,column=5).value} "
      f"F={s0.cell(row=r,column=6).value} G={s0.cell(row=r,column=7).value}")

P("\n### 卡头数据: 有价赠币 N / 无价赠币 O relation to M")
bad = 0
for r in range(2, s1.max_row + 1):
    n = s1.cell(row=r, column=14).value
    o = s1.cell(row=r, column=15).value
    m = s1.cell(row=r, column=13).value
    if all(isinstance(x, (int, float)) for x in (n, o, m)) and n + o != m:
        bad += 1
        if bad <= 5:
            P(f"  R{r}: N={n} O={o} M={m}")
P(f"  rows where N+O != M: {bad}")

P("\n### 卡头数据 rows 2..17 (manually typed P位?) J values")
for r in range(2, 18):
    P(f"  R{r}: A={dt(s1.cell(row=r,column=1).value)} H={s1.cell(row=r,column=8).value} "
      f"I={s1.cell(row=r,column=9).value} J={s1.cell(row=r,column=10).value} "
      f"L={s1.cell(row=r,column=12).value} M={s1.cell(row=r,column=13).value}")

P("\n### 卡头数据 J formula boundary rows 730..740")
for r in range(730, 741):
    P(f"  R{r}: A={dt(s1.cell(row=r,column=1).value)} H={s1.cell(row=r,column=8).value} "
      f"I={s1.cell(row=r,column=9).value} J={s1.cell(row=r,column=10).value}")

P("\n### 数据案板 vs 卡头数据 2026-07-25 cross-check")
idx = {}
for r in range(2, s1.max_row + 1):
    if dt(s1.cell(row=r, column=1).value) == "2026-07-25":
        idx[s1.cell(row=r, column=9).value] = (s1.cell(row=r, column=12).value, s1.cell(row=r, column=13).value)
s2 = wbv["数据案板"]
for r in range(2, s2.max_row + 1):
    c = s2.cell(row=r, column=3).value
    e = s2.cell(row=r, column=5).value
    f = s2.cell(row=r, column=6).value
    P(f"  commid {c}: 案板 E={e} F={f} | 卡头 07-25 L,M={idx.get(c)}")

with open(out, "w", encoding="utf-8") as f:
    f.write(buf.getvalue())
print("written", out)
