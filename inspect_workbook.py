# -*- coding: utf-8 -*-
"""Dump structure of an xlsx workbook into a UTF-8 text report."""
import io
from collections import Counter
from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.formula import ArrayFormula

path = r"C:\Users\Tuilge\Desktop\资产核对表-鄂州店.xlsx"
out = r"C:\Users\Tuilge\Desktop\码表线上化\structure_dump.txt"

wb = load_workbook(path, data_only=False)
wbv = load_workbook(path, data_only=True)

buf = io.StringIO()
def P(*a):
    print(*a, file=buf)

def cellval(c):
    v = c.value
    if isinstance(v, ArrayFormula):
        return "ARRAY{" + str(v.text) + "}"
    return v

def short(v, n=60):
    if v is None:
        return ""
    s = str(v).replace("\n", "\\n").replace("\r", "")
    return s if len(s) <= n else s[:n] + "…"

P("=" * 110)
P("FILE:", path)
P("SHEETS:", wb.sheetnames)
for i, name in enumerate(wb.sheetnames):
    ws = wb[name]
    P(f"  [{i}] {name!r} state={ws.sheet_state} dims={ws.dimensions} max_row={ws.max_row} "
      f"max_col={ws.max_column} merged={len(ws.merged_cells.ranges)} "
      f"tables={list(ws.tables.keys())} freeze={ws.freeze_panes} autofilter={ws.auto_filter.ref}")

P("\nDEFINED NAMES:", {n: str(dn.value) for n, dn in wb.defined_names.items()})

for idx, name in enumerate(wb.sheetnames):
    ws = wb[name]
    wsv = wbv[name]
    P("\n" + "#" * 110)
    P(f"##### SHEET [{idx}] {name}   dims={ws.dimensions}")
    P("#" * 110)

    if ws.merged_cells.ranges:
        P("MERGED:", ", ".join(str(r) for r in ws.merged_cells.ranges))
    widths = {k: round(v.width, 1) for k, v in ws.column_dimensions.items() if v.width}
    P("COL WIDTHS:", widths)

    # data validations
    try:
        for dv in ws.data_validations.dataValidation:
            P("DV:", dv.type, dv.formula1, "->", dv.sqref)
    except Exception as e:
        P("DV err", e)

    # conditional formatting
    try:
        cfs = list(ws.conditional_formatting)
        if cfs:
            P("CONDITIONAL FORMAT RANGES:", len(cfs))
    except Exception:
        pass

    # formulas
    fcells = []
    for row in ws.iter_rows():
        for c in row:
            v = c.value
            txt = v.text if isinstance(v, ArrayFormula) else (v if isinstance(v, str) and v.startswith("=") else None)
            if txt:
                fcells.append((c.coordinate, "ARRAY " if isinstance(v, ArrayFormula) else "", txt))
    P(f"FORMULA CELLS: {len(fcells)}")
    seen = set()
    for coord, kind, txt in fcells:
        col = "".join(ch for ch in coord if ch.isalpha())
        # show first formula per column, plus unique templates
        key = (col, txt.replace(coord, "<RC>")[:80])
        if col not in seen:
            seen.add(col)
            P(f"  [col {col}] {coord}: {kind}{txt}")
    # unique formula templates
    tpl = Counter()
    for coord, kind, txt in fcells:
        col = "".join(ch for ch in coord if ch.isalpha())
        t = txt
        import re
        t = re.sub(r"\$?[A-Z]{1,3}\$?\d+", "<R>", t)
        tpl[(col, kind + t)] += 1
    P("  -- distinct formula templates --")
    for (col, t), n in tpl.most_common(30):
        P(f"  col {col} x{n}: {short(t, 200)}")

    # first rows
    maxc = ws.max_column
    P(f"-- FIRST 8 ROWS (A..{get_column_letter(maxc)}) --")
    for r in range(1, min(ws.max_row, 8) + 1):
        parts = []
        for cc in range(1, maxc + 1):
            v = short(cellval(ws.cell(row=r, column=cc)), 45)
            if v != "":
                parts.append(f"{get_column_letter(cc)}{r}={v}")
        P(f"R{r}: " + " | ".join(parts))

    P("-- LAST 3 ROWS --")
    for r in range(max(1, ws.max_row - 2), ws.max_row + 1):
        parts = []
        for cc in range(1, maxc + 1):
            v = short(cellval(ws.cell(row=r, column=cc)), 40)
            if v != "":
                parts.append(f"{get_column_letter(cc)}={v}")
        P(f"R{r}: " + " | ".join(parts))

    # per-column profile
    P("-- COLUMN PROFILE (raw sheet) --")
    for cc in range(1, maxc + 1):
        col = get_column_letter(cc)
        vals = []
        nonnull = 0
        types = Counter()
        for r in range(1, ws.max_row + 1):
            v = cellval(ws.cell(row=r, column=cc))
            if v is None or (isinstance(v, str) and v.strip() == ""):
                continue
            nonnull += 1
            types[type(v).__name__] += 1
            if len(vals) < 4000:
                vals.append(v)
        if nonnull == 0:
            continue
        cnt = Counter(str(x) for x in vals)
        P(f"  {col}: nonnull={nonnull} types={dict(types)} distinct~{len(cnt)}")
        P(f"     top: " + "; ".join(f"{short(k,28)}x{n}" for k, n in cnt.most_common(8)))
        numvals = [x for x in vals if isinstance(x, (int, float))]
        if numvals:
            P(f"     min={min(numvals)} max={max(numvals)}")
        dts = [x for x in vals if hasattr(x, "year")]
        if dts:
            P(f"     date range {min(dts)} .. {max(dts)}")

with open(out, "w", encoding="utf-8") as f:
    f.write(buf.getvalue())
print("written", out, len(buf.getvalue()))
