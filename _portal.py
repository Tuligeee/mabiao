# -*- coding: utf-8 -*-
"""营销账本看板 · 在线服务端（纯标准库 / 零第三方依赖，可直接跑在 Linux ECS）

页面上的两个按钮：
  「刷新数据」→ GET  /api/data[?force=1]   重新从钉钉 AI 表格拉一遍数据
  「保存预算」→ POST /api/budget           写回钉钉「投放预算」表（增量 upsert）

鉴权不依赖 dws CLI：用钉钉企业内部应用 AppKey/AppSecret 换 access_token（7200s 自动续期），
读写在 query 带 operatorId(unionId)、header 带 x-acs-dingtalk-access-token。
→ 服务器只需 python3，无需安装 node / dws，也不依赖 WorkBuddy。

环境变量（带默认值的可省）：
  DINGTALK_APP_KEY        必填   企业内部应用 AppKey
  DINGTALK_APP_SECRET     必填   企业内部应用 AppSecret
  DINGTALK_OPERATOR_ID    必填   操作人 unionId（AI 表格 OpenAPI 强制要求）
  LEDGER_PORT             8787   监听端口
  LEDGER_BASE_ID          jb9Y4gmKWr6Bp0emHeRXDapZWGXn6lpz
  LEDGER_COST_TABLE       pWKFDJH   投放花费
  LEDGER_METRIC_TABLE     GoK5oYk   账本指标
  LEDGER_BUDGET_TABLE     9TJvIrW   投放预算
  LEDGER_CACHE_TTL        60     /api/data 缓存秒数（「刷新数据」带 force=1 绕过）
  LEDGER_ACCESS_CODE      ""     设了则要求 ?code=xxx 或 X-Access-Code 头（防外人直接访问）
  LEDGER_DOC_NODE         gvNG4YZ7Jn6wjdp2H97nBr9lJ2LD0oRE  「业绩记录及预测」在线表格节点（增收实时计算用）

本地起服务：python ledger_server.py [端口]
"""
import os, sys, json, time, re, datetime, threading, hashlib, urllib.request, urllib.error, urllib.parse, calendar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import gen_ledger as G          # 复用 make_data() / build_html()

# ── 配置 ────────────────────────────────────────────────────────────────
APP_KEY      = os.environ.get("DINGTALK_APP_KEY", "").strip()
APP_SECRET   = os.environ.get("DINGTALK_APP_SECRET", "").strip()
OPERATOR_ID  = os.environ.get("DINGTALK_OPERATOR_ID", "UaVaAnIfZpJrTiS1543xBOAiEiE").strip()   # 李灏铭 unionId
BASE_ID      = os.environ.get("LEDGER_BASE_ID", "jb9Y4gmKWr6Bp0emHeRXDapZWGXn6lpz").strip()
COST_TABLE   = os.environ.get("LEDGER_COST_TABLE", "pWKFDJH").strip()
LEDGER_TABLE = os.environ.get("LEDGER_METRIC_TABLE", "GoK5oYk").strip()
BUDGET_TABLE = os.environ.get("LEDGER_BUDGET_TABLE", "9TJvIrW").strip()
CONF_TABLE   = os.environ.get("LEDGER_CONF_TABLE", "ExRSkGP").strip()   # 账本配置（去年月投入）
CH_AMOUNT_TABLE = os.environ.get("LEDGER_CH_AMOUNT_TABLE", "HPz7LVk").strip()   # 渠道成交（逐日）
DOC_NID      = os.environ.get("LEDGER_DOC_NODE", "gvNG4YZ7Jn6wjdp2H97nBr9lJ2LD0oRE").strip()  # 在线表「业绩记录及预测」
PORT         = int(os.environ.get("LEDGER_PORT", "8787"))
CACHE_TTL    = int(os.environ.get("LEDGER_CACHE_TTL", "60"))
ACCESS_CODE  = os.environ.get("LEDGER_ACCESS_CODE", "").strip()

# 🔴 2026-10-28：登录 cookie。
#    为什么不再只靠 ?code=：手机上一长串带码的链接很难保存，
#    而且码会留在地址栏/历史/截图里。改成「输一次 → 种 cookie → 之后免输」。
#    cookie 里存的是口令的派生值，不是口令本身（泄露 cookie ≠ 泄露口令）。
GATE_COOKIE  = "ledger_gate"
GATE_DAYS    = 30


def gate_token(code: str) -> str:
    """口令 → cookie 值（确定性派生，改口令即刻让所有旧 cookie 失效）。"""
    return hashlib.sha256(("ledger::" + str(code)).encode("utf-8")).hexdigest()[:32]


# 🔴 2026-11-11：支持挂在子路径下（public /ledger/ → 本服务 /）。
#    为什么必须做：mancado.cn 把整站反代到这台机器，但 /api/ 被它自己的前端占了；
#    页面里的绝对路径（表单 action="/api/login"、302 Location、fetch('/api/data')）
#    会解析到域名根 → 打到人家的 /api/ → POST 静态文件 → 405。
#    nginx 用 `proxy_pass http://127.0.0.1:8787/`（尾斜杠）把 /ledger 剥掉再交给我们，
#    同时用 `proxy_set_header X-Forwarded-Prefix /ledger` 把前缀告诉我们，
#    这里所有对外 URL 一律拼上它。直连 127.0.0.1:8787 调试时没有这个头，前缀为空。
def prefix_of(handler) -> str:
    """本次请求的对外前缀，形如 "/ledger"；没有则返回空串。"""
    raw = (handler.headers.get("X-Forwarded-Prefix")
           or os.environ.get("LEDGER_BASE_PATH") or "").strip()
    raw = raw.rstrip("/")
    if raw and not raw.startswith("/"):
        raw = "/" + raw
    return "" if raw in ("", "/") else raw

API_ROOT = "https://api.dingtalk.com/v1.0"

# ── HTTP 封装 ───────────────────────────────────────────────────────────
def _req(method, url, body=None, token=None, timeout=40):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["x-acs-dingtalk-access-token"] = token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"_raw": raw[:400]}
    except Exception as e:
        return -1, {"_err": repr(e)[:300]}


_tok = {"v": None, "exp": 0.0}
_tok_lock = threading.Lock()


def get_token(force=False):
    """企业内部应用 access_token（7200s），缓存并在过期前 5 分钟自动续期"""
    with _tok_lock:
        now = time.time()
        if not force and _tok["v"] and now < _tok["exp"] - 300:
            return _tok["v"]
        st, res = _req("POST", API_ROOT + "/oauth2/accessToken",
                       {"appKey": APP_KEY, "appSecret": APP_SECRET}, timeout=20)
        if st != 200 or not isinstance(res, dict) or not res.get("accessToken"):
            raise RuntimeError("换取 access_token 失败: %s %s" % (st, str(res)[:200]))
        _tok["v"] = res["accessToken"]
        _tok["exp"] = now + int(res.get("expireIn") or 7200)
        return _tok["v"]


def notable(method, sheet_id, suffix="", params=None, body=None, retry_401=True):
    params = dict(params or {})
    params["operatorId"] = OPERATOR_ID
    url = "%s/notable/bases/%s/sheets/%s%s?%s" % (
        API_ROOT, BASE_ID, sheet_id, suffix, urllib.parse.urlencode(params))
    st, res = _req(method, url, body, get_token())
    if st == 401 and retry_401:                       # token 失效 → 强刷一次
        st, res = _req(method, url, body, get_token(force=True))
    return st, res


# ── 字段取值 ────────────────────────────────────────────────────────────
def _s(v):
    """文本字段（singleSelect 是 {name,id}）"""
    if isinstance(v, dict):
        return v.get("name") or ""
    return "" if v is None else str(v)


def _n(v):
    try:
        return float(v)
    except Exception:
        return 0.0


def fetch_records(sheet_id, page_size=100, max_pages=60):
    """分页拉全量（maxResults 上限 100，用 nextToken 翻页）

    🔴 2026-11-09：钉钉会偶发 500 internalError（同一请求立刻重试就 200）。
    以前一遇到就整页 500，现在每页最多重试 3 次（2s/4s 退避）。
    """
    recs, token, pages = [], None, 0
    while True:
        params = {"maxResults": page_size}
        if token:
            params["nextToken"] = token
        res = None
        for attempt in range(3):
            try:
                st, res = noted_ok(sheet_id, params)
                break
            except Exception as ex:
                if attempt >= 2:
                    raise
                print("[retry] 读表 %s 第 %d 次失败（%s）→ %d 秒后重试"
                      % (sheet_id, attempt + 1, str(ex)[:100], 2 + attempt * 2))
                time.sleep(2 + attempt * 2)
        recs += (res or {}).get("records") or []
        token = (res or {}).get("nextToken")
        pages += 1
        if not (res or {}).get("hasMore") or not token or pages >= max_pages:
            break
    return recs


def noted_ok(sheet_id, params):
    st, res = notable("GET", sheet_id, "/records", params)
    if st != 200 or not isinstance(res, dict):
        raise RuntimeError("读表 %s 失败: %s %s" % (sheet_id, st, str(res)[:200]))
    return st, res


# ── 数据组装 ────────────────────────────────────────────────────────────
def read_cost_rows():
    rows = []
    for r in fetch_records(COST_TABLE):
        f = r.get("fields") or {}
        week = _s(f.get("统计周"))
        if not week:
            continue
        rows.append({"week": week, "store": _s(f.get("门店")) or "未命名",
                     "channel": _s(f.get("渠道")) or "其他",
                     "category": _s(f.get("分类")) or "未分类",
                     "amount": _n(f.get("金额"))})
    return rows


def read_led_rows():
    rows = []
    for r in fetch_records(LEDGER_TABLE):
        f = r.get("fields") or {}
        rows.append({"metric": _s(f.get("指标")), "week": _s(f.get("统计周")),
                     "group": _s(f.get("分组")), "value": _n(f.get("值")),
                     "note": _s(f.get("说明"))})
    return rows


def read_budget_map():
    """→ {月份: {渠道: {'monthly': 金额|'', 'weeks': {周: 金额|''}}}}（前端 budget 结构）"""
    out = {}
    for r in fetch_records(BUDGET_TABLE):
        f = r.get("fields") or {}
        mo = _s(f.get("月份"))
        if not mo:
            continue
        ch = _s(f.get("渠道"))
        wk = _s(f.get("统计周"))
        raw = f.get("预算金额")
        amt = "" if raw is None or str(raw).strip() == "" else float(raw)
        m = out.setdefault(mo, {}).setdefault(ch, {"monthly": "", "weeks": {}})
        if wk:
            m["weeks"][wk] = amt
        else:
            m["monthly"] = amt
    return out


def read_last_year_map():
    """「账本配置」表 → {账本月 'YYYY-MM': 去年该月整月投入}
    页面上的「去年整月投入」入口写的就是这张表（账本「增投」的基数）。"""
    out = {}
    for r in fetch_records(CONF_TABLE):
        f = r.get("fields") or {}
        if _s(f.get("配置项")) != "去年月投入":
            continue
        mo = _s(f.get("月份")).strip()
        v = _n(f.get("数值"))
        if len(mo) == 7 and mo[4] == "-" and mo[:4].isdigit() and mo[5:7].isdigit() and v > 0:
            out[mo] = v
    return out


def save_last_year(month, amount):
    """upsert「去年月投入」：值变了走「删旧建新」；填 0 → 删除该月配置"""
    deletes, keep = [], None
    for r in fetch_records(CONF_TABLE):
        f = r.get("fields") or {}
        if _s(f.get("配置项")) != "去年月投入" or _s(f.get("月份")) != month:
            continue
        if amount > 0:
            keep = (r.get("id"), _n(f.get("数值")))
        else:
            deletes.append(r.get("id"))
    if keep and abs(keep[1] - amount) > 0.004:
        deletes.append(keep[0])
        keep = None
    errs = []
    if deletes:
        st, res = notable("POST", CONF_TABLE, "/records/delete", body={"recordIds": deletes})
        if st != 200:
            errs.append("删除失败 %s %s" % (st, str(res)[:120]))
    if amount > 0 and not keep:
        st, res = notable("POST", CONF_TABLE, "/records",
                          body={"records": [{"fields": {"配置项": "去年月投入",
                                                        "月份": month, "数值": amount}}]})
        if st not in (200, 201):
            errs.append("写入失败 %s %s" % (st, str(res)[:120]))
    return errs


def apply_last_year(data):
    """用「去年月投入」重算「增投 / ROI」——页面一改基数，增投与 ROI 立即联动
    （不必等本机跑 `_ledger_week_sync.py`；口径与它完全一致）
      增投(周) = 该周总费用 − 去年该月投入 ÷ 去年该月天数 × 该周天数
      ROI     = 增收 ÷ 增投
    ⚠️ 该月**没填**去年月投入时，增投/ROI 一律置 None（前端显示 `-`）——
       不能沿用账本表里的旧值，那会让人误以为是当月正确数（2026-10-10 用户要求）"""
    led = data.get("ledger") or []
    if not led:
        return data
    ly = data.get("lastYear") or {}
    tot = {}
    for r in data.get("cost") or []:
        w = r.get("week") or ""
        tot[w] = tot.get(w, 0.0) + float(r.get("amount") or 0)
    zs = {r.get("week"): r.get("value") for r in led if r.get("metric") == "增收"}
    wdays = {}
    for mo in (data.get("weeksByMonth") or {}):
        if not (len(mo) == 7 and mo[4] == "-" and mo[:4].isdigit() and mo[5:7].isdigit()):
            continue
        try:
            wdays.update(G.week_days_map(int(mo[:4]), int(mo[5:7])))
        except Exception:
            pass
    for r in led:
        w = r.get("week") or ""
        mo = w[:7]
        if not (len(mo) == 7 and mo[4] == "-"):
            continue
        if mo not in ly:                       # 未填基数 → 增投/ROI 留空
            if r.get("metric") in ("增投", "ROI"):
                r["value"] = None
            continue
        if not wdays.get(w):
            continue
        try:
            y, m = int(mo[:4]), int(mo[5:7])
            base = ly[mo] / calendar.monthrange(y - 1, m)[1] * wdays[w]
        except Exception:
            continue
        zt = round(tot.get(w, 0.0) - base, 2)
        if r.get("metric") == "增投":
            r["value"] = zt
        elif r.get("metric") == "ROI":
            z = zs.get(w)
            r["value"] = round(z / zt, 2) if (z is not None and zt) else None
    return data


def read_ch_amount_rows():
    """渠道成交（逐日）→ [{'date','week','channel','cost','increase','nature','total','orders'}]
    日期字段是 date 类型（ms 时间戳），按 UTC 解释还原成 YYYY-MM-DD"""
    rows = []
    for r in fetch_records(CH_AMOUNT_TABLE):
        f = r.get("fields") or {}
        ch, wk = _s(f.get("渠道")), _s(f.get("统计周"))
        if not ch or not wk:
            continue
        ms = f.get("日期")
        d = ""
        if isinstance(ms, (int, float)) and ms > 0:
            import datetime as _dt
            d = (_dt.datetime(1970, 1, 1) + _dt.timedelta(milliseconds=ms)).strftime("%Y-%m-%d")
        rows.append({"date": d, "week": wk, "channel": ch,
                     "cost": _n(f.get("消耗")), "increase": _n(f.get("增量成交")),
                     "nature": _n(f.get("自然成交")), "total": _n(f.get("总成交")),
                     "orders": _n(f.get("成交订单数"))})
    return rows


# ── 「增收」实时计算：直读钉钉在线电子表格「业绩记录及预测」 ─────────────────
# 口径与 `_ledger_week_sync.py` 完全一致：
#   今年(业绩记录 s2) = A列日期落周 → D~R 列（15 店）加总
#   去年(目标分解 s4) = A列同区间  → B~P 列加总（排除 Q=合计）
#   增收 = 今年 − 去年（正差 ×0.8；负差按原值不打折）
S_REC, S_TGT = "s2", "s4"
_EXCEL_EPOCH = datetime.date(1899, 12, 30)
_COL_REC = (3, 18)      # D~R（0-based 半开区间）
_COL_TGT = (1, 16)      # B~P
_TGT_DP  = 1            # 「目标分解」取值小数位 —— 与钉钉显示值 / 本机 dws 口径一致


def read_doc_range(sheet, addr):
    """读在线电子表格区域 → values 二维数组（日期列返回 Excel 序列号）。
    接口：GET /v1.0/doc/workbooks/{nodeId}/sheets/{sheetId}/ranges/{rangeAddress}"""
    url = "%s/doc/workbooks/%s/sheets/%s/ranges/%s?select=values&operatorId=%s" % (
        API_ROOT, DOC_NID, sheet, addr, OPERATOR_ID)
    st, res = _req("GET", url, token=get_token())
    if st != 200 or not isinstance(res, dict):
        raise RuntimeError("读在线表 %s!%s 失败: %s %s" % (sheet, addr, st, str(res)[:200]))
    return res.get("values") or []


def _xdate(v):
    """Excel 日期序列号 → date（在线表接口返回数字；dws 返回的才是 '2026/8/30' 文本）"""
    try:
        return _EXCEL_EPOCH + datetime.timedelta(days=int(v))
    except Exception:
        return None


def _xf(v):
    try:
        return float(str(v).replace(",", ""))
    except Exception:
        return 0.0


def _month_segments(y, mo):
    """月界优先周分段（与 gen_ledger.month_weeks / _ledger_week_sync.py 同一规则）"""
    first = datetime.date(y, mo, 1)
    nxt = datetime.date(y + 1, 1, 1) if mo == 12 else datetime.date(y, mo + 1, 1)
    last = nxt - datetime.timedelta(days=1)
    segs, end_first = [], first
    while end_first.weekday() != 6:      # 周日=6
        end_first += datetime.timedelta(days=1)
    segs.append((first, end_first))
    cur = end_first + datetime.timedelta(days=1)
    while cur <= last:
        e = min(cur + datetime.timedelta(days=6), last)
        segs.append((cur, e))
        cur = e + datetime.timedelta(days=1)
    return segs


def _read_span(sheet, d0, d1):
    """读 (d0~d1) 覆盖的行 → {date: cells}。行号按 A2 的日期锚点推导（每日一行）。"""
    head = read_doc_range(sheet, "A1:A2")
    anc = _xdate(head[1][0]) if (len(head) >= 2 and head[1]) else None
    if not anc:
        raise RuntimeError("%s 无法定位日期锚点（A2 不是日期）" % sheet)
    r0 = max(2, 2 + (d0 - anc).days)
    r1 = max(r0, 2 + (d1 - anc).days)
    vals = read_doc_range(sheet, "A%d:R%d" % (r0, r1))
    out = {}
    for row in vals:
        if not row:
            continue
        dt = _xdate(row[0])
        if dt:
            out[dt] = row
    return out


def _span_sum(daily, d0, d1):
    d, s = d0, 0.0
    while d <= d1:
        s += daily.get(d, 0.0)
        d += datetime.timedelta(days=1)
    return s


def compute_increase(weeks):
    """→ {周标签: 增收值}（weeks 形如 ['2026-09-第1周', ...]）"""
    parsed = []
    for w in sorted(set(weeks)):
        m = re.match(r"^(\d{4})-(\d{2})-第(\d+)周$", w or "")
        if not m:
            continue
        y, mo, idx = int(m.group(1)), int(m.group(2)), int(m.group(3))
        segs = _month_segments(y, mo)
        if 1 <= idx <= len(segs):
            parsed.append((w, segs[idx - 1]))
    if not parsed:
        return {}
    dmin = min(a for _, (a, _b) in parsed)
    dmax = max(b for _, (_a, b) in parsed)
    rec = _read_span(S_REC, dmin, dmax)
    tgt = _read_span(S_TGT, dmin, dmax)
    # ⚠️ 「目标分解」的单元格在钉钉里按 1 位小数显示：dws(本机脚本) 读到的是这个**显示值**，
    #    而 doc API 返回的是原始全精度值（2393.70208823325 vs 2393.7）→ 逐周会差 0.1~0.4 元。
    #    为保证与账本表 / 本机脚本**逐周完全一致**，这里也按 _TGT_DP 位小数取值再求和。
    rec_daily = {d: sum(_xf(row[i]) for i in range(*_COL_REC) if i < len(row)) for d, row in rec.items()}
    tgt_daily = {d: sum(round(_xf(row[i]), _TGT_DP) for i in range(*_COL_TGT) if i < len(row))
                 for d, row in tgt.items()}
    out = {}
    for w, (d0, d1) in parsed:
        diff = _span_sum(rec_daily, d0, d1) - _span_sum(tgt_daily, d0, d1)
        out[w] = round(diff * 0.8 if diff > 0 else diff, 2)
    return out


def read_increase(weeks):
    """读表失败自动重试 1 次 → (增收字典 | None, 最后一次错误)"""
    err = None
    for i in (1, 2):
        try:
            return compute_increase(weeks), None
        except Exception as e:
            err = e
            if i == 1:
                time.sleep(1.0)
    return None, err


_data_cache = {"v": None, "t": 0.0}
_data_lock = threading.Lock()

# ── 磁盘缓存（2026-11-09）────────────────────────────────────────────────
# 为什么要它：pWKFDJH 已 2242 条、每月 +1000，钉钉接口每页上限 100 且只能串行翻页
#   → 全量拉一次 15~17 秒。而数据一天才变一次（一键刷新写表），
#     却每次打开都重拉，纯属白等。
# 用法：一键刷新第 19 步调 /api/data?force=1 → 全量拉完顺手落盘；
#   之后所有人打开页面都直接读这个文件（毫秒级），点「刷新数据」才回源。
# 失效：超过 LEDGER_DISK_MAX_AGE（默认 24h）自动回源，避免长期陈旧。
DISK_CACHE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".cache", "data.json")
DISK_MAX_AGE  = int(os.environ.get("LEDGER_DISK_MAX_AGE", str(24 * 3600)))
DATA_VERSION  = 2          # data 结构变了就 +1，旧缓存自动作废


def _disk_load():
    """读磁盘缓存 → data | None（结构版本不符 / 过期 / 文件坏了，一律返回 None）"""
    try:
        with open(DISK_CACHE, encoding="utf-8") as f:
            d = json.load(f)
        if d.get("_v") != DATA_VERSION:
            return None
        t = float(d.get("_t") or 0)
        if not t or time.time() - t > DISK_MAX_AGE:
            return None
        return {k: v for k, v in d.items() if not k.startswith("_")}
    except Exception:
        return None


def _disk_save(data):
    """原子落盘（tmp → replace）；失败只打日志，绝不影响页面"""
    try:
        os.makedirs(os.path.dirname(DISK_CACHE), exist_ok=True)
        tmp = DISK_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dict(data, _v=DATA_VERSION, _t=time.time()), f, ensure_ascii=False)
        os.replace(tmp, DISK_CACHE)
    except Exception as e:
        print("[cache] 落盘失败（不影响页面）：%s" % str(e)[:160])


def _disk_drop():
    """改了预算/基数后调用：删掉磁盘缓存，否则下次打开还读旧值"""
    try:
        if os.path.exists(DISK_CACHE):
            os.remove(DISK_CACHE)
    except Exception:
        pass


def get_data(force=False):
    """拉数据 + 组装前端 DATA（含 budget / stamp）

    三级缓存：内存(60s) → 磁盘(24h) → 回源钉钉（15~25 秒）
    force=True（页面「刷新数据」/ 一键刷新第 19 步）→ 跳过前两级，强制回源并落盘
    """
    with _data_lock:
        now = time.time()
        if not force and _data_cache["v"] is not None and now - _data_cache["t"] < CACHE_TTL:
            return _data_cache["v"]
        if not force:
            d = _disk_load()
            if d is not None:                  # 命中磁盘：不碰钉钉，毫秒返回
                _data_cache["v"] = d
                _data_cache["t"] = now         # 60 秒内不再读文件
                return d
        budget_map = read_budget_map()
        cost_rows = read_cost_rows()
        led_rows = read_led_rows()
        # ★「增收」实时计算：直读在线表「业绩记录及预测」s2/s4，覆盖账本表里的旧值
        #   失败自动重试 1 次；仍失败 → 保留账本旧值，并置 incOk=False（前端提示"未取到最新"）
        inc_ok, inc_err = True, ""
        inc_weeks = {r["week"] for r in led_rows if r.get("metric") == "增收" and r.get("week")}
        if inc_weeks:
            inc, inc_err = read_increase(inc_weeks)
            if inc is None:
                inc_ok = False
                print("[inc] 增收实时计算失败，沿用账本旧值：%s" % str(inc_err)[:200])
            else:
                for r in led_rows:
                    if r.get("metric") == "增收" and r.get("week") in inc:
                        r["value"] = inc[r["week"]]
        # extra_months：预算表里已填过的月份必须留在下拉里，
        # 否则该月暂时没有花费数据时，它的历史预算会「消失」且改不动
        data = G.make_data(cost_rows, led_rows, read_ch_amount_rows(),
                           extra_months=list(budget_map.keys()))
        data["budget"] = budget_map
        data["lastYear"] = read_last_year_map()      # 「增投」基数（去年月投入），页面可编辑
        data["demoBudget"] = {}          # 在线版不吃示例预算：钉钉里没填就是「未设」
        data["stamp"] = now
        data["incOk"] = inc_ok           # False = 本次未取到在线表（增收显示为账本上次同步值）
        data["incErr"] = str(inc_err)[:200] if inc_err else ""
        apply_last_year(data)            # ★ 用最新「去年月投入」重算 增投/ROI（改基数立即联动）
        _data_cache["v"] = data
        _data_cache["t"] = now
        _disk_save(data)                 # ★ 落盘：下次打开直接读，不再等 15~25 秒
        return data


def save_budget(month, items):
    """增量 upsert：按 (统计周, 渠道) 对齐；留空的项删除旧记录。
    值变化的采用「删旧建新」（已验证可靠的写法，避免 PUT 更新偶发不在预计内的返回）。"""
    exist = {}
    for r in fetch_records(BUDGET_TABLE):
        f = r.get("fields") or {}
        if _s(f.get("月份")) != month:
            continue
        exist[(_s(f.get("统计周")), _s(f.get("渠道")))] = (r.get("id"), _n(f.get("预算金额")))

    want = {}
    for it in items:
        ch = str(it.get("channel") or "")
        if not ch:
            continue
        want[(str(it.get("week") or ""), ch)] = float(it.get("amount") or 0)

    creates, deletes, n_create, n_update = [], [], 0, 0
    for k, amt in want.items():
        cur = exist.get(k)
        if cur is None:
            n_create += 1
        elif abs(cur[1] - amt) > 0.004:
            n_update += 1
            deletes.append(cur[0])                     # 值变了 → 删旧建新
        else:
            continue
        creates.append({"fields": {"月份": month, "统计周": k[0], "渠道": k[1], "预算金额": amt}})
    for k, (rid, _amt) in exist.items():
        if k not in want:
            deletes.append(rid)                        # 用户清空此项 → 删掉旧记录

    errors = []
    if deletes:
        st, res = notable("POST", BUDGET_TABLE, "/records/delete", body={"recordIds": deletes})
        if st != 200:
            errors.append("删除失败 %s %s" % (st, str(res)[:150]))
    if creates:
        st, res = notable("POST", BUDGET_TABLE, "/records", body={"records": creates})
        if st not in (200, 201):
            errors.append("写入失败 %s %s" % (st, str(res)[:150]))

    return {"created": n_create, "updated": n_update,
            "deleted": max(0, len(deletes) - n_update), "errors": errors}


# ── HTTP 服务 ───────────────────────────────────────────────────────────
def render_page(base: str = ""):
    """首屏直接把当前数据内嵌进 HTML（走 60s 缓存 → 秒开，不白屏）；
    页面上的「刷新数据」才走 /api/data?force=1 重新拉取。

    🔴 2026-11-11：页面里的接口一律用**相对路径**（fetch('api/data')）。
    本页永远挂在 <对外前缀>/board 下，相对路径会自动带上正确的前缀：
    直连 IP 是 /ledger/api/data，经 mancado.cn 是 /cwsj/ledger/api/data，
    不需要两边任何一处知道对方的挂载方式。"""
    html = G.build_html(online=True, data=get_data(False))
    return html.replace("fetch('/api/", "fetch('api/")


# ── 门户页（根路径 /portal）─────────────────────────────────────────────
# 2026-10-28 建立；2026-11-09 17:23 的部署曾把它覆盖成旧版三行列表，
# 当天按用户要求还原成这版设计页（来源：skill/.staging/ledger/index_v2.html，8040 字节）。
# 🔴 门户页是公开页，**绝对不能出现门店名、金额、ROI**。
PORTAL_HTML = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>运营控制台</title>
<style>
  :root{
    --bg:#f6f7f9;
    --surface:#ffffff;
    --surface-2:#fbfbfc;
    --ink:#16181d;
    --ink-2:#565d68;
    --ink-3:#697180;
    --line:#e5e7eb;
    --line-strong:#d3d7de;
    --accent:#1d4ed8;
    --accent-soft:#eef2ff;
    --radius:14px;
    --shadow-1:0 1px 2px rgba(16,24,40,.05);
    --shadow-2:0 10px 28px -12px rgba(16,24,40,.18);
    --ease:cubic-bezier(.2,.7,.3,1);
  }
  @media (prefers-color-scheme: dark){
    :root{
      --bg:#0d1117;
      --surface:#161b22;
      --surface-2:#12171e;
      --ink:#e7ebf2;
      --ink-2:#a2acba;
      --ink-3:#7c8695;
      --line:#242c37;
      --line-strong:#313b48;
      --accent:#8fb0ff;
      --accent-soft:#1b2540;
      --shadow-1:0 1px 2px rgba(0,0,0,.4);
      --shadow-2:0 14px 34px -16px rgba(0,0,0,.7);
    }
  }

  *{box-sizing:border-box;margin:0;padding:0}
  html{-webkit-text-size-adjust:100%}
  body{
    background:var(--bg);
    color:var(--ink);
    min-height:100vh;
    font-family:"PingFang SC","Microsoft YaHei",system-ui,-apple-system,"Segoe UI",sans-serif;
    font-size:15px;
    line-height:1.65;
    padding:clamp(28px,6vw,64px) clamp(16px,4vw,40px) 56px;
    -webkit-font-smoothing:antialiased;
  }

  .wrap{max-width:1060px;margin:0 auto}

  .eyebrow{
    display:inline-flex;align-items:center;gap:8px;
    font-size:11.5px;letter-spacing:.14em;text-transform:uppercase;
    color:var(--ink-3);font-weight:600;
  }
  .eyebrow::before{content:"";width:18px;height:1px;background:var(--line-strong)}

  h1{
    margin-top:14px;
    font-size:clamp(38px,9vw,46px);
    line-height:1.1;
    letter-spacing:-.02em;
    font-weight:700;
    max-width:20ch;
  }
  .lede{margin-top:14px;color:var(--ink-2);font-size:14.5px;max-width:44ch}

  section{margin-top:clamp(22px,3.4vw,34px)}

  /* 主入口 */
  a.lead{
    display:block;text-decoration:none;color:inherit;
    background:var(--surface);
    border:1px solid var(--line);
    border-radius:calc(var(--radius) + 4px);
    padding:clamp(20px,3.4vw,32px);
    box-shadow:var(--shadow-1);
    transition:border-color .18s var(--ease), box-shadow .18s var(--ease), transform .18s var(--ease);
  }
  a.lead:hover{border-color:var(--line-strong);box-shadow:var(--shadow-2);transform:translateY(-2px)}
  a.lead:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
  a.lead:active{transform:translateY(0)}

  .lead-top{display:flex;align-items:flex-start;gap:16px;flex-wrap:wrap}
  .lead-title{
    font-size:clamp(19px,3vw,24px);font-weight:700;letter-spacing:-.01em;
    display:flex;align-items:center;gap:10px;flex-wrap:wrap;
  }
  .lead-desc{color:var(--ink-2);font-size:14px;margin-top:8px;max-width:42ch}
  .lead-scope{
    margin-top:20px;padding-top:18px;border-top:1px solid var(--line);
    display:flex;flex-wrap:wrap;gap:8px 22px;
    font-size:12.5px;color:var(--ink-3);
  }
  .lead-scope b{color:var(--ink);font-weight:600;font-variant-numeric:tabular-nums}

  .go{
    margin-left:auto;align-self:center;
    display:inline-flex;align-items:center;gap:8px;
    font-size:13px;font-weight:600;color:var(--accent);white-space:nowrap;
  }
  .go svg{width:15px;height:15px;transition:transform .18s var(--ease)}
  a.lead:hover .go svg{transform:translateX(3px)}

  /* 次级入口 */
  .grid{margin-top:12px;display:grid;gap:12px;grid-template-columns:1fr}
  a.item{
    display:flex;align-items:center;gap:14px;
    text-decoration:none;color:inherit;
    background:var(--surface);
    border:1px solid var(--line);
    border-radius:var(--radius);
    padding:18px;min-height:64px;
    box-shadow:var(--shadow-1);
    transition:border-color .18s var(--ease), background-color .18s var(--ease), transform .18s var(--ease);
  }
  a.item:hover{border-color:var(--line-strong);background:var(--surface-2);transform:translateY(-1px)}
  a.item:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
  a.item:active{transform:translateY(0)}
  .item-body{min-width:0;flex:1}
  .name{font-size:15px;font-weight:600;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
  .desc{font-size:12.5px;color:var(--ink-2);margin-top:4px;line-height:1.55}
  .path{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:11.5px;color:var(--ink-3)}
  .chev{color:var(--ink-3);flex:none;display:inline-flex}
  .chev svg{width:16px;height:16px}

  .badge{
    display:inline-flex;align-items:center;gap:5px;
    font-size:11px;font-weight:600;line-height:1;
    padding:5px 8px;border-radius:999px;
    background:var(--accent-soft);color:var(--accent);
  }
  .badge svg{width:11px;height:11px}

  footer{
    margin-top:clamp(30px,5vw,48px);
    padding-top:20px;
    border-top:1px solid var(--line);
    color:var(--ink-3);
    font-size:12px;line-height:1.9;
  }
  footer p{max-width:62ch}
  footer strong{color:var(--ink-2);font-weight:600}

  @media (max-width:719px){
    .go{margin-left:0;margin-top:16px;width:100%}
    .lead-scope{gap:6px 18px}
  }

  @media (min-width:720px){
    .grid{grid-template-columns:1fr 1fr}
  }

  @media (min-width:1000px){
    .grid{grid-template-columns:repeat(3,1fr)}
  }



  @media (prefers-reduced-motion:reduce){
    *{transition:none!important}
    a.lead:hover,a.item:hover{transform:none}
  }
</style>
</head>
<body>
<div class="wrap">

  <header>
    <div class="eyebrow">内部工具</div>
    <h1>运营控制台</h1>
    
  </header>

  <section aria-labelledby="lead-h">
    <a class="lead" href="cwsj/">
      <div class="lead-top">
        <div>
          <div class="lead-title" id="lead-h">门店任务管理</div>
          <p class="lead-desc">门店端看今天的任务并回填，总部端下发与查看进度。手机直接打开，不用装东西。</p>
        </div>
        <span class="go">
          打开任务
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
               stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M5 12h13"></path><path d="m12 5 7 7-7 7"></path>
          </svg>
        </span>
      </div>
      <div class="lead-scope">
        <span>门店端 <b>今日任务</b></span>
        <span>总部端 <b>下发与查看</b></span>
        <span>设备 <b>手机可用</b></span>
      </div>
    </a>
  </section>

  <section aria-label="其他入口">
    <div class="grid">
      <a class="item" href="/ledger">
        <span class="item-body">
          <span class="name">
            营销账本看板
            <span class="badge">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                   stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <rect x="4" y="10.5" width="16" height="10" rx="2"></rect>
                <path d="M8 10.5V7a4 4 0 1 1 8 0v3.5"></path>
              </svg>
              口令
            </span>
          </span>
          <span class="desc">费用、增投、增收、ROI 总账</span>
        </span>
        <span class="path">/ledger</span>
        <span class="chev" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
               stroke-linecap="round" stroke-linejoin="round">
            <path d="m9 6 6 6-6 6"></path>
          </svg>
        </span>
      </a>

      <a class="item" href="meter/">
        <span class="item-body">
          <span class="name">
            码表核对
            <span class="badge">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
                   stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
                <rect x="3.5" y="4.5" width="17" height="15" rx="2"></rect>
                <path d="M7.5 9.5h9M7.5 14.5h5"></path>
              </svg>
              手机扫码
            </span>
          </span>
          <span class="desc">店长扫机台二维码填码表，自动和宝点流水对账</span>
        </span>
        <span class="path">meter/</span>
        <span class="chev" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
               stroke-linecap="round" stroke-linejoin="round">
            <path d="m9 6 6 6-6 6"></path>
          </svg>
        </span>
      </a>

      <a class="item" href="taocan/">
        <span class="item-body">
          <span class="name">套餐中枢</span>
          <span class="desc">套餐价目与门店下发</span>
        </span>
        <span class="path">/taocan</span>
        <span class="chev" aria-hidden="true">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"
               stroke-linecap="round" stroke-linejoin="round">
            <path d="m9 6 6 6-6 6"></path>
          </svg>
        </span>
      </a>
    </div>
  </section>

  <footer>
    <p><strong>仅限内部使用</strong>，请勿外传链接。</p>
  </footer>

</div>
</body>
</html>
'''


def render_portal(base: str = ""):
    """根路径门户页（运营控制台）。

    🔴 2026-11-11：站内入口改成相对路径，跟着当前前缀走：
    门户在根 `/`（经 mancado 是 /cwsj/），链接写 `ledger/`
    → 本机得到 /ledger/，经 mancado 得到 /cwsj/ledger/，地址栏始终是规范地址。
    全部入口统一用相对路径：
      门户在根 `/`（经 mancado 是 /cwsj/）→ cwsj/、ledger/、meter/、taocan/
      本机得到 /cwsj/、/meter/ …，经 mancado 得到 /cwsj/cwsj/、/cwsj/meter/ …
    绝对路径从域名进来会多一跳（/taocan/ 先 302 到 /cwsj/taocan/），相对路径一步到位。"""
    return PORTAL_HTML.replace('href="/ledger"', 'href="ledger/"')


AUTH_HINT = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>需要口令</title><style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#f4f6fb;color:#1e293b;font-family:"Microsoft YaHei",system-ui,sans-serif;padding:20px}
.box{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:26px 30px;
text-align:center;max-width:340px}
h1{font-size:16px;margin:0 0 8px}
p{font-size:12.5px;color:#64748b;margin:0;line-height:1.7}
a{display:inline-block;margin-top:14px;font-size:12.5px;color:#2563EB;text-decoration:none}
</style></head><body><div class="box">
<h1>&#128274; 需要访问口令</h1>
<p>本页需通过带口令的链接打开，<br>或已登录的浏览器访问。</p>
<a href="/">&larr; 返回控制台</a>
</div></body></html>"""


# 登录页（2026-10-28）：%s 位置放错误提示
LOGIN_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>登录 &middot; 营销账本</title><style>
  :root{--bg:#f4f6fb;--card:#fff;--tx:#1e293b;--sub:#64748b;--line:#e2e8f0;--accent:#2563EB}
  @media (prefers-color-scheme:dark){
    :root{--bg:#0f172a;--card:#1e293b;--tx:#e2e8f0;--sub:#94a3b8;--line:#334155;--accent:#60a5fa}
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--tx);min-height:100vh;display:flex;align-items:center;
       justify-content:center;padding:20px;font-family:"Microsoft YaHei","PingFang SC",system-ui,sans-serif}
  .box{background:var(--card);border:1px solid var(--line);border-radius:16px;
       padding:30px 28px;width:100%;max-width:330px;text-align:center}
  h1{font-size:17px;font-weight:700;margin-bottom:5px}
  .sub{font-size:12px;color:var(--sub);margin-bottom:20px}
  input{width:100%;padding:12px 14px;font-size:15px;font-family:inherit;color:var(--tx);
        background:var(--bg);border:1px solid var(--line);border-radius:10px;outline:none;
        text-align:center;letter-spacing:.08em}
  input:focus{border-color:var(--accent)}
  button{width:100%;margin-top:11px;padding:12px;font-size:14px;font-weight:600;font-family:inherit;
         color:#fff;background:var(--accent);border:none;border-radius:10px;cursor:pointer}
  button:active{opacity:.86}
  .err{margin-top:11px;font-size:12px;color:#b91c1c}
  .back{display:inline-block;margin-top:16px;font-size:12px;color:var(--sub);text-decoration:none}
</style></head><body>
<form class="box" method="post" action="/api/login">
  <h1>&#127831; 营销账本</h1>
  <div class="sub">输入访问口令</div>
  <input name="pwd" type="password" placeholder="口令" autofocus autocomplete="current-password">
  <button type="submit">进入</button>
  __ERR__
  <a class="back" href="/">&larr; 返回控制台</a>
</form>
</body></html>"""


# 取数失败时的兜底页（别让浏览器白屏）
ERROR_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>数据加载失败</title><style>
body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
background:#f4f6fb;color:#1e293b;font-family:"Microsoft YaHei",system-ui,sans-serif;padding:20px}
.box{background:#fff;border:1px solid #fecaca;border-radius:14px;padding:26px 30px;
text-align:center;max-width:420px}
h1{font-size:16px;margin:0 0 10px;color:#b91c1c}
p{font-size:12.5px;color:#64748b;margin:0;line-height:1.7}
.err{margin-top:12px;padding:9px 11px;background:#fef2f2;border-radius:8px;
font-family:Consolas,monospace;font-size:11px;color:#991b1b;word-break:break-all;text-align:left}
a{display:inline-block;margin-top:14px;font-size:12.5px;color:#2563EB;text-decoration:none}
</style></head><body><div class="box">
<h1>数据加载失败</h1>
<p>钉钉表格取数出错，看板暂时打不开。<br>常见原因：钉钉令牌过期 / 网络抖动 / 表结构变动。</p>
<div class="err">%s</div>
<a href="/ledger">重试</a> &nbsp;&middot;&nbsp; <a href="/">&larr; 返回控制台</a>
</div></body></html>"""


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "LedgerBoard"

    def _send(self, code, body, ctype, extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        for k, v in (extra or {}).items():      # 302 跳转等额外头（2026-10-28 加）
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False), "application/json; charset=utf-8")

    def _html(self, code, text):
        self._send(code, text, "text/html; charset=utf-8")

    def _authed(self, u):
        if not ACCESS_CODE:
            return True
        code = urllib.parse.parse_qs(u.query).get("code", [""])[0] or self.headers.get("X-Access-Code", "")
        if code and code == ACCESS_CODE:
            return True
        # 登录 cookie（2026-10-28）：输过一次就不用再输
        want = gate_token(ACCESS_CODE)
        for chunk in (self.headers.get("Cookie") or "").split(";"):
            k, _, v = chunk.strip().partition("=")
            if k == GATE_COOKIE and v == want:
                return True
        return False

    def _login_page(self, err: str = "", code: int = 200):
        """登录页：输口令 → 种 cookie → 跳 <前缀>/board。

        🔴 用 replace 不用 % 格式化：LOGIN_PAGE 的 CSS 里有 ``width:100%``，
           走 % 会报 "unsupported format character ';'"（2026-10-28 踩到）。
        🔴 表单 action 必须是「前缀 + /api/login」：绝对路径挂在子路径下会跑到域名根
           （2026-11-11 mancado.cn 上就是这样被 POST 成 405 的）。
        """
        tip = ('<div class="err">%s</div>' % err) if err else ""
        html = (LOGIN_PAGE.replace("__ERR__", tip)
                          .replace('action="/api/login"', 'action="api/login"')
                          .replace('href="/"', 'href="../"'))
        return self._html(code, html)

    def _do_login(self):
        """POST /api/login —— 校验口令，成功种 cookie 并 302 回 <前缀>/board。"""
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "ignore") if n else ""
        form = urllib.parse.parse_qs(raw)
        pwd = (form.get("pwd", [""])[0] or "").strip()
        b = prefix_of(self)
        if not ACCESS_CODE:
            return self._send(302, b"", "text/html; charset=utf-8", {"Location": b + "/board"})
        if pwd != ACCESS_CODE:
            return self._login_page("口令不对，再试一次", 401)
        # cookie 的作用域跟着前缀走：挂在 /ledger 下就只在 /ledger 内生效，
        # 不会撒到整个域名（通过别人的域名访问时尤其重要，2026-11-11）
        ck = "%s=%s; Path=%s; Max-Age=%d; HttpOnly; SameSite=Lax" % (
            GATE_COOKIE, gate_token(ACCESS_CODE), (b + "/") or "/", GATE_DAYS * 86400)
        # 登录成功后回到对外的规范地址：<前缀>/（nginx 会把 /ledger/ 精确转给本服务的 /board）
        return self._send(302, b"", "text/html; charset=utf-8",
                          {"Location": (b + "/") if b else "/board", "Set-Cookie": ck})

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)

        # ── 公开区：门户 + 健康检查（不含任何经营数据，故意免口令）──
        # 🔴 2026-11-09 17:5x 还原：根路径回到 10-28 那版「运营控制台」设计页
        #    （当天 17:23 的部署把线上设计页覆盖成了旧版三行列表，用户要求改回）。
        #    /portal 作为别名同样指向设计页，方便老书签。
        b = prefix_of(self)
        if u.path in ("/", "/index.html", "/portal", "/portal/", "/index.html.portal"):
            return self._html(200, render_portal(b))
        if u.path == "/healthz":
            return self._json(200, {"ok": True, "ts": time.time()})
        # 🔴 2026-11-11：对外只有一个看板地址 <前缀>/（形如 /ledger/）。
        #    nginx 把 /ledger/ 精确转成本服务的 /board，所以这里不再做尾斜杠跳转，
        #    否则 /ledger/ → /ledger/board → /ledger/ 会打转。

        # ── 以下一律要口令 ──
        if not self._authed(u):
            if u.path in ("/ledger", "/board"):
                return self._login_page()          # 给登录页（带输入框），不是提示页
            return self._json(401, {"error": "unauthorized"})

        if u.path in ("/ledger", "/board"):
            # 🔴 2026-10-28：加兜底 —— 钉钉取数失败时给人看的错误页，
            #    否则 render_page() 抛异常会导致连接直接断（浏览器白屏，看不出原因）
            try:
                return self._html(200, render_page(b))
            except Exception as exc:
                html = (ERROR_PAGE % (str(exc)[:200],)).replace(
                    'href="/ledger"', 'href="."').replace('href="/"', 'href="../"')
                return self._html(500, html)
        if u.path == "/api/data":
            force = q.get("force", ["0"])[0] in ("1", "true", "yes")
            try:
                return self._json(200, get_data(force))
            except Exception as e:
                return self._json(500, {"error": str(e)[:300]})
        if u.path == "/api/budget":
            try:
                return self._json(200, read_budget_map())
            except Exception as e:
                return self._json(500, {"error": str(e)[:300]})
        if u.path == "/api/config":
            try:
                return self._json(200, {"lastYear": read_last_year_map()})
            except Exception as e:
                return self._json(500, {"error": str(e)[:300]})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        # 🔴 登录接口必须放行（它就是用来换口令的，不能要求先有口令）
        if u.path == "/api/login":
            return self._do_login()
        if not self._authed(u):
            return self._json(401, {"error": "unauthorized"})
        if u.path == "/api/budget":
            n = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except Exception as e:
                return self._json(400, {"error": "bad json: %s" % e})
            month = str(payload.get("month") or "")
            items = payload.get("items") or []
            if not month:
                return self._json(400, {"error": "month required"})
            # 必须是 YYYY-MM：曾有人在表里手填裸数字 "9"，让看板整页 502，
            # 这里从入口挡住，免得前端/其它调用方再写进脏月份
            if not (len(month) >= 7 and month[:4].isdigit() and month[5:7].isdigit()
                    and 1 <= int(month[5:7]) <= 12 and 1970 <= int(month[:4]) <= 9999):
                return self._json(400, {"error": "bad month: %r (expect YYYY-MM)" % month})
            try:
                res = save_budget(month, items)
                res["ok"] = not res.get("errors")
                res["budget"] = read_budget_map()      # 回读校验
                _data_cache["v"] = None; _disk_drop()                # 预算变了 → 数据缓存失效
                return self._json(200, res)
            except Exception as e:
                return self._json(500, {"error": str(e)[:300]})
        if u.path == "/api/config":
            n = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except Exception as e:
                return self._json(400, {"error": "bad json: %s" % e})
            month = str(payload.get("month") or "")
            # 同 /api/budget：只收 YYYY-MM，挡掉手填的裸数字（"10"）之类脏值
            if not (len(month) == 7 and month[:4].isdigit() and month[5:7].isdigit()
                    and 1 <= int(month[5:7]) <= 12 and 1970 <= int(month[:4]) <= 9999):
                return self._json(400, {"error": "bad month: %r (expect YYYY-MM)" % month})
            try:
                amount = float(payload.get("amount") or 0)
            except Exception:
                return self._json(400, {"error": "bad amount"})
            if amount < 0:
                return self._json(400, {"error": "amount must be >= 0"})
            try:
                errs = save_last_year(month, amount)
                _data_cache["v"] = None; _disk_drop()
                return self._json(200, {"ok": not errs, "errors": errs,
                                        "lastYear": read_last_year_map()})
            except Exception as e:
                return self._json(500, {"error": str(e)[:300]})
        return self._json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), fmt % args))


if __name__ == "__main__":
    if len(sys.argv) > 1:
        PORT = int(sys.argv[1])
    missing = [k for k, v in (("DINGTALK_APP_KEY", APP_KEY), ("DINGTALK_APP_SECRET", APP_SECRET),
                              ("DINGTALK_OPERATOR_ID", OPERATOR_ID)) if not v]
    if missing:
        print("缺少环境变量: " + ", ".join(missing))
        sys.exit(2)
    print("营销账本看板 · 服务已启动 → http://127.0.0.1:%d" % PORT)
    print("  Base=%s  投放花费=%s  账本指标=%s  投放预算=%s  渠道成交=%s"
          % (BASE_ID, COST_TABLE, LEDGER_TABLE, BUDGET_TABLE, CH_AMOUNT_TABLE))
    print("  访问口令: %s" % ("已启用" if ACCESS_CODE else "未设置（任何人可访问）"))
    # 🔴 2026-11-11：只绑回环。对外一律走 nginx（80/443 → /ledger/），
    #    原来绑 0.0.0.0 等于给 8787 留了个绕过 nginx 的后门（当时靠安全组没放行才没事）。
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
