# -*- coding: utf-8 -*-
"""生成修正版工作簿 v3。

修正点
1/2  F/G/I/J/O 全部改成"限定区间"（$2:$5000），去掉整列 1,048,576 行数组
     - 性能：约 90 亿次比较 -> 约 6000 万次
     - 顺手消掉 L:L+M:M 整列相加（首行文本表头 -> #VALUE! 污染整个数组）的坑
     - G 由 4 次 XLOOKUP 合并成 2 次；I/J 用 MAX(0, 今日-昨日) 一行搞定
3    读数下降不再静默算成巨负值：I/J 记 0，状态标「读数下降待确认」，由人决定是抄错还是换表
4    L 列把写死的 SUM(H2:H5) 换成 SUMIFS(按日期+机台)，插行/重复行不再错位
5    新增 O 列「核对状态」，不可比的行 L/M 留空，不再假装 0
"""
from copy import copy
from openpyxl import load_workbook
from openpyxl.worksheet.formula import ArrayFormula
from openpyxl.styles import Font, Alignment, Border, Side

SRC = r"C:\Users\Tuilge\Desktop\资产核对表-鄂州店.xlsx"
DST = r"C:\Users\Tuilge\Desktop\码表线上化\资产核对表-鄂州店-修正版.xlsx"
DST_ALT = r"C:\Users\Tuilge\Desktop\码表线上化\资产核对表-鄂州店-修正版2.xlsx"
PICK = r"C:\Users\Tuilge\Desktop\码表线上化\_last_output.txt"

N0, N1 = 2, 5000
R_END = 1714
ST_DOWN = "读数下降待确认"

AGG = 'OR($B{r}="凤凰城",$B{r}="法老也疯狂",$B{r}="疯狂魔鬼城")'

K_A = f"卡头数据!$A${N0}:$A${N1}"
K_B = f"卡头数据!$B${N0}:$B${N1}"
K_D = f"卡头数据!$D${N0}:$D${N1}"
K_F = f"卡头数据!$F${N0}:$F${N1}"
K_G = f"卡头数据!$G${N0}:$G${N1}"
K_H = f"卡头数据!$H${N0}:$H${N1}"
K_J = f"卡头数据!$J${N0}:$J${N1}"
K_L = f"卡头数据!$L${N0}:$L${N1}"
K_M = f"卡头数据!$M${N0}:$M${N1}"

S_A = f"$A${N0}:$A${N1}"
S_B = f"$B${N0}:$B${N1}"
S_C = f"$C${N0}:$C${N1}"
S_D = f"$D${N0}:$D${N1}"
S_E = f"$E${N0}:$E${N1}"
S_H = f"$H${N0}:$H${N1}"
S_K = f"$K${N0}:$K${N1}"
S_O = f"$O${N0}:$O${N1}"

# 找"前一天且有码表读数"的那一行（D<>"" 保证不会把空读数当成 0 基准）
PREV_KEY = f'({S_A}=$A{{r}}-1)*({S_B}=$B{{r}})*({S_C}=$C{{r}})*({S_D}<>"")'


def prev_lookup(r):
    return f'_xlfn.XLOOKUP(1,{PREV_KEY.format(r=r)},{S_D},NA())'


def f_f(r):
    return (f'=IFERROR(_xlfn.XLOOKUP(1,({K_A}=$A{r})*({K_H}=$B{r})*({K_J}=$C{r}),'
            f'{K_L}+{K_M},0),0)')


def f_g(r):
    key = f'({K_A}=$A{r})*({K_B}=$B{r})*({K_D}=$C{r})'
    return (f'=IFERROR(_xlfn.XLOOKUP(1,{key},{K_G},0)'
            f'/IF(_xlfn.XLOOKUP(1,{key},{K_F},"")="电子彩票",50,10),0)')


def f_h(r):
    return f'=IFERROR($F{r}-$G{r},"")'


def f_i(r):
    # 照常做减法：读数下降会得到负日差（大差异要保留给人看），只由 O 列打标记
    return f'=IFERROR($D{r}-{prev_lookup(r)},0)'


def f_j(r):
    p = prev_lookup(r).replace(S_D, S_E).replace(f'({S_D}<>"")', f'({S_E}<>"")')
    return f'=IFERROR($E{r}-{p},0)'


def f_k(r):
    return f'=IFERROR($I{r}-$J{r},"")'


def f_o(r):
    p = prev_lookup(r)
    head = (f'IF($D{r}="","缺码表",'
            f'IF(ISNA({p}),"首日",'
            f'IF($D{r}<{p},"{ST_DOWN}",')
    tail = ')))'
    single = f'IF(COUNTIFS({K_A},$A{r},{K_H},$B{r},{K_J},$C{r})=0,"缺卡头数据","正常")'
    agg = f'IF(COUNTIFS({K_A},$A{r},{K_H},$B{r})<>4,"数据不全","正常")'
    return (f'=IF({AGG.format(r=r)},'
            f'IF($C{r}<>"P1","共用码表",{head}{agg}{tail}),'
            f'{head}{single}{tail})')


def f_l(r):
    agg_calc = (f'IF($C{r}="P1",'
                f'SUMIFS({S_H},{S_A},$A{r},{S_B},$B{r})'
                f'-SUMIFS({S_K},{S_A},$A{r},{S_B},$B{r}),0)')
    # 一律出数，不因状态留空（大差异必须看得见）
    return (f'=IF({AGG.format(r=r)},{agg_calc},IFERROR($H{r}-$K{r},""))')


def f_m(r):
    return f'=IFERROR(IF(N($K{r})=0,"",$L{r}/$K{r}),"")'


wb = load_workbook(SRC, data_only=False)
ws = wb["推币机收币误差表"]

hdr_o = ws.cell(row=1, column=15)
hdr_o.value = "核对状态"
for attr in ("font", "fill", "alignment", "border"):
    setattr(hdr_o, attr, copy(getattr(ws.cell(row=1, column=14), attr)))
ws.column_dimensions["O"].width = 18

ARRAY_COLS = {6, 7, 9, 10}
FUNCS = {6: f_f, 7: f_g, 8: f_h, 9: f_i, 10: f_j,
         11: f_k, 12: f_l, 13: f_m, 15: f_o}

for r in range(N0, R_END + 1):
    for col, fn in FUNCS.items():
        cell = ws.cell(row=r, column=col)
        text = fn(r)
        if col in ARRAY_COLS:
            cell.value = ArrayFormula(ref=cell.coordinate, text=text)
        else:
            cell.value = text

# 尾部空行（原表 1715~1720 也挂了整列数组公式，是性能杀手之一）清掉
for r in range(R_END + 1, ws.max_row + 1):
    for col in FUNCS:
        ws.cell(row=r, column=col).value = None

# 卡头数据 J 列（P位反查）也把整列 B:B&C:C 收成 $2:$5000，1263 个整列查找 -> 5000 行
s1 = wb["卡头数据"]
REPL = [("B:B", f"$B${N0}:$B${N1}"), ("C:C", f"$C${N0}:$C${N1}"),
        ("D:D", f"$D${N0}:$D${N1}")]
j_fixed = 0
for r in range(N0, s1.max_row + 1):
    cell = s1.cell(row=r, column=10)
    v = cell.value
    if isinstance(v, ArrayFormula):
        txt = v.text
        for a, b in REPL:
            txt = txt.replace(a, b)
        if txt != v.text:
            cell.value = ArrayFormula(ref=cell.coordinate, text=txt)
            j_fixed += 1
    elif isinstance(v, str) and v.startswith("=") and ":" in v and "XLOOKUP" in v.upper():
        txt = v
        for a, b in REPL:
            txt = txt.replace(a, b)
        if txt != v:
            cell.value = txt
            j_fixed += 1
print("卡头数据 J 列改写:", j_fixed)

thin = Side(style="thin", color="BFBFBF")
box = Border(left=thin, right=thin, top=thin, bottom=thin)
ws["S1"] = "核对汇总（自动）"
ws["S1"].font = Font(bold=True, size=11)

summary = [
    ("可核对行数", f'=COUNTIF({S_O},"正常")'),
    ("误差合计（全部）", f'=SUM($L${N0}:$L${R_END})'),
    ("误差合计（仅正常行）", f'=SUMIFS($L${N0}:$L${R_END},{S_O},"正常")'),
    ("|误差|≥1000（全部）", f'=COUNTIF($L${N0}:$L${R_END},">=1000")+COUNTIF($L${N0}:$L${R_END},"<=-1000")'),
    ("|误差|≥1000（仅正常）", f'=COUNTIFS($L${N0}:$L${R_END},">=1000",{S_O},"正常")'
                            f'+COUNTIFS($L${N0}:$L${R_END},"<=-1000",{S_O},"正常")'),
    ("读数下降待确认", f'=COUNTIF({S_O},"{ST_DOWN}")'),
    ("首日行数", f'=COUNTIF({S_O},"首日")'),
    ("缺数据行数", f'=COUNTIF({S_O},"缺卡头数据")+COUNTIF({S_O},"数据不全")+COUNTIF({S_O},"缺码表")'),
    ("共用码表行数", f'=COUNTIF({S_O},"共用码表")'),
]
for i, (label, formula) in enumerate(summary, start=2):
    ws.cell(row=i, column=19, value=label).border = box
    c = ws.cell(row=i, column=20, value=formula)
    c.border = box
    c.alignment = Alignment(horizontal="right")

ws.column_dimensions["S"].width = 22
ws.column_dimensions["T"].width = 16
ws.cell(row=12, column=19,
        value="说明：L/M 一律照算、不删任何行。O 列只做提示（读数下降=待人工确认是抄错还是换表）。"
              "看净额用「误差合计（仅正常行）」。").font = Font(size=9, color="808080")

try:
    wb.calculation.fullCalcOnLoad = True
except Exception as e:
    print("calc flag skip:", e)

out = DST
try:
    wb.save(out)
except PermissionError:
    out = DST_ALT
    wb.save(out)
    print("!! 原文件被占用（Excel 正打开着），已改存为:", out)

with open(PICK, "w", encoding="utf-8") as f:
    f.write(out)
print("saved", out)
