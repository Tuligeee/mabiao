# -*- coding: utf-8 -*-
"""Targeted checks: error cells in 卡头数据, machine rename date, 09-26 anomaly, J templates."""
import io
from collections import Counter, defaultdict
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.formula import ArrayFormula

path = r"C:\Users\Tuilge\Desktop\资产核对表-鄂州店.xlsx"
out = r"C:\Users\Tuilge\Desktop\码表线上化\check2.txt"
wb = load_workbook(path, data_only=False)
wbv = load_workbook(path, data_only=True)
buf = io.StringIO()
def P(*a): print(*a, file=buf)

s0, s0v = wb["推币机收币误差表"], wbv["推币机收币误差表"]
s1, s1v = wb["卡头数据"], wbv["卡头数据"]

P("### 1) error values in 卡头数据 (cached) per column")
for cc in range(1, 17):
    col = get_column_letter(cc)
    errs = Counter()
    for r in range(2, s1v.max_row + 1):
        v = s1v.cell(row=r, column=cc).value
        if isinstance(v, str) and v.startswith("#"):
            errs[v] += 1
    if errs:
        P(f"  {col}: {dict(errs)}")

P("\n### 1b) error values in sheet0 (cached) per column")
for cc in range(1, 15):
    col = get_column_letter(cc)
    errs = Counter()
    for r in range(2, s0v.max_row + 1):
        v = s0v.cell(row=r, column=cc).value
        if isinstance(v, str) and v.startswith("#"):
            errs[v] += 1
    if errs:
        P(f"  {col}: {dict(errs)}")

P("\n### 2) 卡头数据 J formula template split by row range")
ranges = defaultdict(list)
for r in range(2, s1.max_row + 1):
    v = s1.cell(row=r, column=10).value
    txt = v.text if isinstance(v, ArrayFormula) else v
    if isinstance(txt, str) and txt.startswith("="):
        kind = "concat" if "&" in txt else "single"
        ranges[kind].append(r)
for k, rs in ranges.items():
    P(f"  {k}: n={len(rs)} rows {min(rs)}..{max(rs)}")
    # contiguous segments
    segs = []
    start = prev = rs[0]
    for x in rs[1:]:
        if x == prev + 1:
            prev = x
        else:
            segs.append((start, prev)); start = prev = x
    segs.append((start, prev))
    P(f"     segments: {segs}")

P("\n### 3) machine name presence per date in sheet0")
first_last = {}
for r in range(2, s0.max_row + 1):
    d = s0.cell(row=r, column=1).value
    m = s0.cell(row=r, column=2).value
    if d is None or m is None: continue
    key = d.strftime("%Y-%m-%d")
    first_last.setdefault(m, [key, key])
    first_last[m][0] = min(first_last[m][0], key)
    first_last[m][1] = max(first_last[m][1], key)
for m, (a, b) in first_last.items():
    P(f"  {m}: {a} .. {b}")

P("\n### 3b) 彩虹王国 vs 碧海传说 day-by-day switch (sheet0)")
switch = []
for r in range(2, s0.max_row + 1):
    d = s0.cell(row=r, column=1).value
    m = s0.cell(row=r, column=2).value
    if d is None: continue
    if m in ("彩虹王国", "碧海传说"):
        switch.append((d.strftime("%Y-%m-%d"), m))
seen = []
prev = None
for k, m in switch:
    if (k, m) != prev:
        seen.append((k, m)); prev = (k, m)
P("  transitions:", [x for i, x in enumerate(seen) if i == 0 or x[1] != seen[i-1][1]][:20])

P("\n### 4) sheet0 2026-09-26 block (17 rows)")
for r in range(2, s0.max_row + 1):
    d = s0.cell(row=r, column=1).value
    if d is None: continue
    if d.strftime("%Y-%m-%d") == "2026-09-26":
        vals = [s0v.cell(row=r, column=c).value for c in (1,2,3,4,5,6,7,8,11,12,13)]
        P(f"  R{r}: {vals}")
        P(f"      Lformula={s0.cell(row=r,column=12).value}")

P("\n### 5) sheet0 rows with date but no machine")
cnt = 0
for r in range(2, s0.max_row + 1):
    if s0.cell(row=r, column=1).value is not None and s0.cell(row=r, column=2).value is None:
        cnt += 1
        if cnt <= 10:
            P(f"  R{r}: {[s0.cell(row=r,column=c).value for c in range(1,15)]}")
P(f"  total: {cnt}")

P("\n### 6) 卡头数据 rows with date but no 机台 (left block gaps)")
cnt = 0
seg = []
for r in range(2, s1.max_row + 1):
    if s1.cell(row=r, column=1).value is not None and s1.cell(row=r, column=2).value is None:
        cnt += 1
        seg.append(r)
P(f"  total: {cnt}; rows={seg[:60]}")

P("\n### 7) 卡头数据 per-date machine counts (left block), dates with < 16")
per = defaultdict(Counter)
for r in range(2, s1.max_row + 1):
    d = s1.cell(row=r, column=1).value
    m = s1.cell(row=r, column=2).value
    if d is None or m is None: continue
    per[d.strftime("%Y-%m-%d")][m] += 1
for k in sorted(per):
    tot = sum(per[k].values())
    if tot != 16:
        P(f"  {k}: total={tot} {dict(per[k])}")

P("\n### 8) 卡头数据 machine first/last date (left block)")
fl = {}
for r in range(2, s1.max_row + 1):
    d = s1.cell(row=r, column=1).value
    m = s1.cell(row=r, column=2).value
    if d is None or m is None: continue
    key = d.strftime("%Y-%m-%d")
    fl.setdefault(m, [key, key])
    fl[m][0] = min(fl[m][0], key); fl[m][1] = max(fl[m][1], key)
for m, v in fl.items():
    P(f"  {m}: {v[0]} .. {v[1]}")

P("\n### 9) 卡头数据 通讯id -> P位 mapping consistency (left block)")
mp = defaultdict(set)
for r in range(2, s1.max_row + 1):
    c = s1.cell(row=r, column=3).value
    d = s1.cell(row=r, column=4).value
    if c is None or d is None: continue
    mp[c].add(d)
P("  commid count:", len(mp))
for c, ps in sorted(mp.items(), key=lambda x: str(x[0])):
    P(f"   {c} -> {sorted(ps)}")

P("\n### 10) 卡头数据 项目分组 per machine")
mp2 = defaultdict(set)
for r in range(2, s1.max_row + 1):
    m = s1.cell(row=r, column=2).value
    g = s1.cell(row=r, column=5).value
    if m and g: mp2[m].add(g)
for m, g in mp2.items():
    P(f"  {m}: {sorted(g)}")

P("\n### 11) sheet0 N 误差原因 full list with row context")
for r in range(2, s0.max_row + 1):
    v = s0.cell(row=r, column=14).value
    if v:
        d = s0.cell(row=r, column=1).value
        P(f"  R{r} {d.strftime('%Y-%m-%d') if hasattr(d,'strftime') else d} "
          f"{s0.cell(row=r,column=2).value} {s0.cell(row=r,column=3).value} "
          f"L={s0v.cell(row=r,column=12).value} M={s0v.cell(row=r,column=13).value} 原因={v}")

P("\n### 12) M 误差率 extremes (cached) top 10 abs")
rows = []
for r in range(2, s0.max_row + 1):
    v = s0v.cell(row=r, column=13).value
    if isinstance(v, (int, float)):
        rows.append((abs(v), r, s0v.cell(row=r, column=1).value, s0v.cell(row=r, column=2).value,
                     s0v.cell(row=r, column=3).value, v))
rows.sort(reverse=True)
for x in rows[:10]:
    d = x[2]
    P(f"  R{x[1]} {d.strftime('%Y-%m-%d') if hasattr(d,'strftime') else d} {x[3]} {x[4]} 误差率={x[5]}")

with open(out, "w", encoding="utf-8") as f:
    f.write(buf.getvalue())
print("written", out, len(buf.getvalue()))
