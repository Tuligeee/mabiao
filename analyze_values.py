# -*- coding: utf-8 -*-
"""Value-level analysis: cached results, per-date coverage, block boundaries, gaps."""
import io
from collections import Counter, defaultdict
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.formula import ArrayFormula

path = r"C:\Users\Tuilge\Desktop\资产核对表-鄂州店.xlsx"
out = r"C:\Users\Tuilge\Desktop\码表线上化\value_dump.txt"
wbv = load_workbook(path, data_only=True)
wb = load_workbook(path, data_only=False)

buf = io.StringIO()
def P(*a): print(*a, file=buf)

s0 = wbv["推币机收币误差表"]
s1 = wbv["卡头数据"]
s0f = wb["推币机收币误差表"]
s1f = wb["卡头数据"]

def num(v):
    return v if isinstance(v, (int, float)) else None

P("### SHEET0 number formats per column")
for cc in range(1, 16):
    c = s0f.cell(row=2, column=cc)
    P(f"  {get_column_letter(cc)}1={s0f.cell(row=1,column=cc).value!r} fmt={c.number_format!r}")

P("\n### SHEET1 number formats")
for cc in range(1, 17):
    c = s1f.cell(row=2, column=cc)
    P(f"  {get_column_letter(cc)}1={s1f.cell(row=1,column=cc).value!r} fmt={c.number_format!r}")

# sheet0: first 20 rows with values
P("\n### SHEET0 rows 1..24 cached values")
hdr = [s0.cell(row=1, column=c).value for c in range(1, 16)]
P("   " + " | ".join(f"{get_column_letter(i+1)}:{h}" for i, h in enumerate(hdr)))
for r in range(2, 25):
    vals = []
    for c in range(1, 16):
        v = s0.cell(row=r, column=c).value
        if hasattr(v, "strftime"):
            v = v.strftime("%Y-%m-%d")
        vals.append("" if v is None else str(v))
    P(f"R{r}: " + " | ".join(vals))

P("\n### SHEET0 per-date coverage")
per_date = defaultdict(lambda: {"rows": 0, "d": 0, "e": 0, "F": 0, "G": 0, "K": 0, "L": 0})
dates = []
for r in range(2, s0.max_row + 1):
    d = s0.cell(row=r, column=1).value
    if d is None:
        continue
    key = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
    if not dates or dates[-1] != key:
        dates.append(key)
    rec = per_date[key]
    rec["rows"] += 1
    for col, name in ((4, "d"), (5, "e"), (6, "F"), (7, "G"), (11, "K"), (12, "L")):
        if num(s0.cell(row=r, column=col).value) is not None:
            rec[name] += 1
P(f"  distinct dates: {len(dates)}  first={dates[0]} last={dates[-1]}")
P("  date | rows | D(码表入) | E(码表出) | F(卡头上) | G(卡头下) | K(码表收) | L(误差)")
for key in dates:
    rec = per_date[key]
    P(f"  {key} | {rec['rows']} | {rec['d']} | {rec['e']} | {rec['F']} | {rec['G']} | {rec['K']} | {rec['L']}")

P("\n### SHEET0 row-order check: machine/P sequence per date (first 3 dates)")
seen = 0
prev = None
for r in range(2, s0.max_row + 1):
    d = s0.cell(row=r, column=1).value
    if d is None:
        continue
    key = d.strftime("%Y-%m-%d")
    if prev != key:
        if seen >= 3:
            break
        seen += 1
        P(f"  --- {key} ---")
        prev = key
    P(f"   R{r}: {s0.cell(row=r,column=2).value} {s0.cell(row=r,column=3).value}")

P("\n### SHEET1 block boundary inspection (rows 935..950, 1080..1095, 1640..1660)")
for lo, hi in ((935, 950), (1080, 1095), (1640, 1660)):
    P(f"  --- rows {lo}..{hi} ---")
    for r in range(lo, hi + 1):
        parts = []
        for c in range(1, 17):
            v = s1.cell(row=r, column=c).value
            if hasattr(v, "strftime"):
                v = v.strftime("%m-%d")
            if v is None:
                continue
            parts.append(f"{get_column_letter(c)}={v}")
        P(f"   R{r}: " + " | ".join(parts))

P("\n### SHEET1 counts by block")
left = sum(1 for r in range(2, s1.max_row + 1) if s1.cell(row=r, column=1).value is not None)
right = sum(1 for r in range(2, s1.max_row + 1) if s1.cell(row=r, column=8).value is not None)
P(f"  rows with date in A (left block): {left}")
P(f"  rows with 机台名称 in H (right block): {right}")

# duplicate keys in each block
lk = Counter()
for r in range(2, s1.max_row + 1):
    d = s1.cell(row=r, column=1).value
    if d is None: continue
    lk[(d.strftime("%Y-%m-%d"), s1.cell(row=r,column=2).value, s1.cell(row=r,column=4).value)] += 1
dup = {k: v for k, v in lk.items() if v > 1}
P(f"  left-block duplicate (date,机台,P位) keys: {len(dup)}")
for k, v in list(dup.items())[:10]:
    P("    ", k, v)

rk = Counter()
for r in range(2, s1.max_row + 1):
    d = s1.cell(row=r, column=1).value
    if d is None: continue
    rk[(d.strftime("%Y-%m-%d"), s1.cell(row=r,column=8).value, s1.cell(row=r,column=10).value)] += 1
dupr = {k: v for k, v in rk.items() if v > 1}
P(f"  right-block duplicate (date,机台名称,P位) keys: {len(dupr)}")
for k, v in list(dupr.items())[:10]:
    P("    ", k, v)

P("\n### SHEET0: rows where F or G resolved to 0 (lookup miss) count")
miss = 0
miss_dates = Counter()
for r in range(2, s0.max_row + 1):
    d = s0.cell(row=r, column=1).value
    if d is None: continue
    f = num(s0.cell(row=r, column=6).value)
    g = num(s0.cell(row=r, column=7).value)
    if f == 0 or g == 0:
        miss += 1
        miss_dates[d.strftime("%Y-%m-%d")] += 1
P(f"  rows with F=0 or G=0: {miss}")
P("  by date:", dict(miss_dates.most_common(20)))

P("\n### SHEET0: totals of L (误差) and distribution")
tot = 0.0
cnt = 0
big = []
for r in range(2, s0.max_row + 1):
    v = num(s0.cell(row=r, column=12).value)
    if v is None: continue
    tot += v; cnt += 1
    if abs(v) >= 1000:
        big.append((r, s0.cell(row=r,column=1).value.strftime("%Y-%m-%d"), s0.cell(row=r,column=2).value, s0.cell(row=r,column=3).value, v, s0.cell(row=r,column=14).value))
P(f"  L cells numeric={cnt} sum={tot:.1f}")
P(f"  |L|>=1000 count={len(big)}")
for x in big[:15]:
    P("   ", x)

P("\n### SHEET2 数据案板 full")
for r in range(1, s2.max_row + 1) if False else []:
    pass
s2 = wbv["数据案板"]
for r in range(1, s2.max_row + 1):
    P("   R%d: " % r + " | ".join("" if s2.cell(row=r, column=c).value is None else str(s2.cell(row=r, column=c).value) for c in range(1, 9)))

with open(out, "w", encoding="utf-8") as f:
    f.write(buf.getvalue())
print("written", out, len(buf.getvalue()))
