# -*- coding: utf-8 -*-
"""校验修正版：公式落盘 + 用原件缓存值独立复算新逻辑的预期结果。"""
import io, zipfile, re
from collections import Counter, defaultdict
from openpyxl import load_workbook
from openpyxl.worksheet.formula import ArrayFormula

SRC = r"C:\Users\Tuilge\Desktop\资产核对表-鄂州店.xlsx"
with open(r"C:\Users\Tuilge\Desktop\码表线上化\_last_output.txt", encoding="utf-8") as _f:
    DST = _f.read().strip()
OUT = r"C:\Users\Tuilge\Desktop\码表线上化\verify_fix.txt"
ST_DOWN = "读数下降待确认"

buf = io.StringIO()
def P(*a): print(*a, file=buf)

new = load_workbook(DST, data_only=False)
ws = new["推币机收币误差表"]
old = load_workbook(SRC, data_only=False)
wss = old["推币机收币误差表"]
oldv = load_workbook(SRC, data_only=True)
s1v = oldv["卡头数据"]

P("### 1. 公式落盘检查")
for coord in ("F2", "G2", "H2", "I2", "J2", "K2", "L2", "M2", "O2"):
    v = ws[coord].value
    txt = v.text if isinstance(v, ArrayFormula) else str(v)
    P(f"  {coord} {'[ARRAY] ' if isinstance(v, ArrayFormula) else ''}{txt}")

P("\n### 2. XML 层面")
with zipfile.ZipFile(DST) as z:
    xml = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
P("  array 公式个数:", xml.count('t="array"'))
P("  仍含整列引用(A:A 之类):", bool(re.search(r'<f[^>]*>[^<]*![A-Z]{1,3}:[A-Z]{1,3}[^<]*</f>', xml)))

P("\n### 3. 原始数据保留情况")
bad = [(r, c) for r in range(2, 1721) for c in (1, 2, 3, 4, 5, 14)
       if wss.cell(row=r, column=c).value != ws.cell(row=r, column=c).value]
P(f"  A-E + N 列差异: {len(bad)} 处")
P(f"  批注 D1: {'有' if ws['D1'].comment else '无'} / 日期有效性: "
  f"{'有' if ws.data_validations.dataValidation else '无'}")
P(f"  卡头数据 dims: {old['卡头数据'].dimensions} -> {new['卡头数据'].dimensions}")
P(f"  数据案板 dims: {old['数据案板'].dimensions} -> {new['数据案板'].dimensions}")

# ---------- 复算（用原件缓存值）----------
P("\n### 4. 新逻辑复算（预期值，用原件缓存值）")
def dt(v): return v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else None

comm2p = {}
for r in range(2, s1v.max_row + 1):
    c, p = s1v.cell(row=r, column=3).value, s1v.cell(row=r, column=4).value
    if c is not None and p:
        comm2p.setdefault(c, p)

qty, award, coin = {}, {}, {}
head_rows = defaultdict(int)          # (date, 机台名称) -> 右块行数
for r in range(2, s1v.max_row + 1):
    d = dt(s1v.cell(row=r, column=1).value)
    m, p = s1v.cell(row=r, column=2).value, s1v.cell(row=r, column=4).value
    if d and m and p:
        qty.setdefault((d, m, p), s1v.cell(row=r, column=7).value)
        award.setdefault((d, m, p), s1v.cell(row=r, column=6).value)
    m2 = s1v.cell(row=r, column=8).value
    if d and m2:
        p2 = s1v.cell(row=r, column=10).value
        p2 = p2 if isinstance(p2, str) else comm2p.get(s1v.cell(row=r, column=9).value)
        if p2:
            l = s1v.cell(row=r, column=12).value
            mm = s1v.cell(row=r, column=13).value
            l = l if isinstance(l, (int, float)) else 0
            mm = mm if isinstance(mm, (int, float)) else 0
            coin.setdefault((d, m2, p2), l + mm)
            head_rows[(d, m2)] += 1

rows = []
for r in range(2, 1715):
    d = dt(ws.cell(row=r, column=1).value)
    m, p = ws.cell(row=r, column=2).value, ws.cell(row=r, column=3).value
    if d is None or m is None:
        continue
    D = ws.cell(row=r, column=4).value
    E = ws.cell(row=r, column=5).value
    rows.append({"r": r, "date": d, "m": m, "p": p,
                 "D": D if isinstance(D, (int, float)) else None,
                 "E": E if isinstance(E, (int, float)) else None})

AGG = {"凤凰城", "法老也疯狂", "疯狂魔鬼城"}
prev = {}
for row in rows:
    k = (row["m"], row["p"])
    pv = prev.get(k)
    row["I"] = 0 if (pv is None or row["D"] is None) else row["D"] - pv[0]
    row["J"] = 0 if (pv is None or row["E"] is None) else row["E"] - pv[1]
    row["K"] = row["I"] - row["J"]
    if row["D"] is not None:
        prev[k] = row["D"], row["E"]
    row["prevD"] = pv[0] if pv else None
    row["agg"] = row["m"] in AGG

for row in rows:
    key = (row["date"], row["m"], row["p"])
    row["F"] = coin.get(key, 0)
    q = qty.get(key)
    rate = 50 if award.get(key) == "电子彩票" else 10
    row["G"] = (q / rate) if isinstance(q, (int, float)) else 0
    row["H"] = row["F"] - row["G"]
    if row["agg"] and row["p"] != "P1":
        row["O"] = "共用码表"
    elif row["D"] is None:
        row["O"] = "缺码表"
    elif row["prevD"] is None:
        row["O"] = "首日"
    elif row["D"] < row["prevD"]:
        row["O"] = ST_DOWN
    elif row["agg"]:
        row["O"] = "数据不全" if head_rows.get((row["date"], row["m"]), 0) != 4 else "正常"
    else:
        row["O"] = "缺卡头数据" if key not in coin else "正常"

for row in rows:
    # L/M 一律照算，不因状态留空
    if row["agg"]:
        if row["p"] != "P1":
            row["L"] = 0
        else:
            blk = [x for x in rows if x["date"] == row["date"] and x["m"] == row["m"]]
            row["L"] = sum(x["H"] for x in blk) - sum(x["K"] for x in blk)
    else:
        row["L"] = row["H"] - row["K"]
    row["M"] = (row["L"] / row["K"]) if row["K"] else None

st = Counter(row["O"] for row in rows)
P("  状态分布:", dict(st))
P(f"  可核对行数: {st['正常']}")
P(f"  误差合计（全部）: {sum(row['L'] for row in rows):.0f}")
P(f"  误差合计（仅正常行）: {sum(row['L'] for row in rows if row['O'] == '正常'):.0f}")
big_all = [row for row in rows if abs(row["L"]) >= 1000]
big_ok = [row for row in big_all if row["O"] == "正常"]
P(f"  |误差|>=1000（全部）: {len(big_all)}    （仅正常行）: {len(big_ok)}")
for row in sorted(big_all, key=lambda x: -abs(x["L"]))[:10]:
    P(f"    {row['date']} {row['m']} {row['p']}: H={row['H']:.0f} K={row['K']:.0f} "
      f"L={row['L']:.0f} [{row['O']}]")

P("\n  逐日抽查:")
for row in rows:
    if row["date"] in ("2026-07-24", "2026-07-25", "2026-10-28", "2026-10-29", "2026-10-30") \
            and row["m"] in ("凤凰城", "法老也疯狂") and row["p"] in ("P1", "P2"):
        P(f"    {row['date']} {row['m']} {row['p']}: D={row['D']} I={row['I']} K={row['K']} "
          f"{row['O']} L={row['L']}")

P("\n  '读数下降待确认' 明细:")
for row in rows:
    if row["O"] == ST_DOWN:
        P(f"    {row['date']} {row['m']} {row['p']}: 昨日={row['prevD']} 今日={row['D']}")

with open(OUT, "w", encoding="utf-8") as f:
    f.write(buf.getvalue())
print("written", OUT)
