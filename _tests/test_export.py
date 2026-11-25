# -*- coding: utf-8 -*-
"""导出表格 vs 核对页 一致性测试（真浏览器）

做两件事：
  1. 用真实 server.py 在临时库上造数据，拿到 /api/recon 与 /api/recon/range 的返回；
  2. 用无头 Chrome 打开真 index.html + app.js，渲染核对表、调 buildReconCsv()，
     把「页面每个单元格」和「CSV 每个单元格」都抓出来逐格比对。

用法：python _tests/test_export.py [-v]
不碰正式库；后端用临时 sqlite，前端用本机 Chrome。
"""
import base64
import csv
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP = ROOT / "app"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
sys.path.insert(0, str(APP))

VERBOSE = "-v" in sys.argv
TMP = ROOT / "_tests" / "_rundir" / ("export_" + uuid.uuid4().hex[:8])
TMP.mkdir(parents=True, exist_ok=True)

import server as S                                        # noqa: E402

S.DB = TMP / "export.db"
S.init_db()

PAGE_ASSETS = ["index.html", "app.js", "app.css", "ui.js", "theme.css", "tokens.css"]


def mk_shop(name, provider="manual"):
    sid = str(uuid.uuid4())
    with S.conn() as c:
        c.execute("INSERT INTO shops(id,name,created_at,provider,group_name) VALUES(?,?,?,?,?)",
                  (sid, name, S.now(), provider, "测试集团"))
    S.ensure_codes()
    return sid


def mk_unit(shop_id, name, kind, brand=None):
    with S.conn() as c:
        cur = c.execute(
            "INSERT INTO units(shop_id,name,kind,brand,from_day,active,team) "
            "VALUES(?,?,?,?,'1970-01-01',1,?)", (shop_id, name, kind, brand, brand))
        uid = cur.lastrowid
    S.ensure_codes()
    return uid


def readings(shop_id, uid, day, **kw):
    S.save_readings(shop_id, day, [dict(unit_id=uid, **kw)])


def build_pachinko():
    """嘻哈卡卡那种：投珠出卡，没有卡头、没有投币。"""
    sid = mk_shop("测试店-投珠出卡")
    S.save_brand_flags({"brand": "萌力", "mode": "bead_card", "unit": "bead", "per_coin": None})
    units = [mk_unit(sid, "萌力双胞胎弹珠机-%03d" % i, "pachinko", "萌力") for i in (1, 2, 3)]
    # 11-23 首填：没有前一天，增量应为空
    for i, uid in enumerate(units):
        readings(sid, uid, "2026-11-23", p_tokens=100 + i * 10, p_cards=10 + i)
    # 11-24 有增量
    readings(sid, units[0], "2026-11-24", p_tokens=160, p_cards=25)
    readings(sid, units[1], "2026-11-24", p_tokens=140, p_cards=18)
    readings(sid, units[2], "2026-11-24", p_tokens=130, p_cards=12)
    S.recon_note(sid, "2026-11-24", units[1], "换码表或归零重计", "换表")

    day = "2026-11-24"
    single = S.recon(sid, day, "pachinko")
    ranged = S.api_recon_range({"shop_id": [sid], "kind": ["pachinko"],
                               "from": ["2026-11-23"], "to": [day]})
    return {"shop": sid, "day": day, "kind": "pachinko", "single": single, "range": ranged}


def build_pusher():
    sid = mk_shop("测试店-推币机")
    uid = mk_unit(sid, "凤凰城", "pusher")
    with S.conn() as c:
        c.execute("INSERT OR REPLACE INTO comms(comm_id,unit_id,updated_at) VALUES(?,?,?)",
                  ("C700", uid, S.now()))
        S.comm_owner_set(c, "C700", uid, sid, "1970-01-01")
        c.execute("INSERT OR REPLACE INTO card_daily(shop_id,day,comm_id,up_native,up_give,"
                  "up_a,up_b,down_qty,fetched_at) VALUES(?,?,?,?,?,0,0,?,?)",
                  (sid, "2026-11-24", "C700", 5000, 0, 1000, S.now()))
    readings(sid, uid, "2026-11-22", in_r=1000, out_r=100)
    readings(sid, uid, "2026-11-23", in_r=1300, out_r=200)
    readings(sid, uid, "2026-11-24", in_r=1800, out_r=300)
    day = "2026-11-24"
    single = S.recon(sid, day, "pusher")
    ranged = S.api_recon_range({"shop_id": [sid], "kind": ["pusher"],
                               "from": ["2026-11-23"], "to": [day]})
    return {"shop": sid, "day": day, "kind": "pusher", "single": single, "range": ranged}


def make_html(fixtures):
    work = TMP / "web"
    work.mkdir(exist_ok=True)
    for f in PAGE_ASSETS:
        shutil.copy2(APP / f, work / f)

    me = {"type": "store", "name": "测试店", "cwsj_name": "测试店", "bound": True,
          "shop": {"id": fixtures[0]["shop"], "name": "测试店"}}
    boot = {"shop_id": fixtures[0]["shop"], "shops": [{"id": fixtures[0]["shop"],
            "name": "测试店", "provider": "manual"}], "units": [],
            "baodian_ok": False, "edit_day": "2026-11-24", "today": "2026-11-25",
            "units_confirmed": "2026-01-01", "brand": None, "brand_flags": {}}
    data = {
        "/api/me": me,
        "/api/bootstrap": boot,
        "/api/recon/range": fixtures[0]["range"],
        "/api/recon": fixtures[0]["single"],
    }
    keys = sorted(data.keys(), key=len, reverse=True)

    stub = """
<script>
window.__DATA__ = /*DATA*/;
window.__FIX__ = /*FIX*/;
(function(){
  var keys = /*KEYS*/;
  window.fetch = function(url){
    var p = String(url);
    for (var i = 0; i < keys.length; i++) {
      if (p.indexOf(keys[i]) >= 0) {
        return Promise.resolve(new Response(JSON.stringify(window.__DATA__[keys[i]]),
          {status: 200, headers: {'Content-Type': 'application/json'}}));
      }
    }
    return Promise.resolve(new Response('{}',
      {status: 200, headers: {'Content-Type': 'application/json'}}));
  };
})();
</script>
""".replace("/*DATA*/", json.dumps(data, ensure_ascii=False)) \
   .replace("/*KEYS*/", json.dumps(keys)) \
   .replace("/*FIX*/", json.dumps(fixtures, ensure_ascii=False))

    runner = """
<script>
function snap(fx) {
  S.role = 'store';
  S.shops = [{id: fx.shop, name: '测试店'}];
  S.shopId = fx.shop;
  S.day = fx.day;
  S.kind = fx.kind;
  S.recon = fx.single;
  renderRecon();
  var head = [].map.call(document.querySelectorAll('#reconHead th'),
    function (x) { return x.textContent.trim(); });
  var rows = [].map.call(document.querySelectorAll('#reconBody tr'), function (tr) {
    return [].map.call(tr.children, function (td) { return td.textContent.trim(); });
  });
  return {head: head, rows: rows, csv: buildReconCsv(fx.range, fx.kind === 'pachinko')};
}
setTimeout(function () {
  var out;
  try {
    out = {pach: snap(window.__FIX__[0]), pusher: snap(window.__FIX__[1])};
  } catch (e) {
    out = {err: String(e.message || e) + '\\n' + String(e.stack || '')};
  }
  var pre = document.createElement('pre');
  pre.id = '__out';
  pre.textContent = btoa(unescape(encodeURIComponent(JSON.stringify(out))));
  document.body.appendChild(pre);
}, 1500);
</script>
"""
    html = (work / "index.html").read_text(encoding="utf-8")
    html = html.replace("</head>", stub + "</head>", 1)
    html = html.replace("</body>", runner + "</body>", 1)
    (work / "test.html").write_text(html, encoding="utf-8")
    return work / "test.html"


def run_chrome(path):
    out = subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
         "--allow-file-access-from-files", "--virtual-time-budget=10000",
         "--dump-dom", "file:///" + str(path).replace("\\", "/")],
        capture_output=True, timeout=180)
    dom = out.stdout.decode("utf-8", "replace")
    m = re.search(r'<pre id="__out">(.*?)</pre>', dom, re.S)
    if not m:
        tail = dom[-2000:]
        raise RuntimeError("没拿到测试输出；DOM 末尾：\n" + tail)
    raw = base64.b64decode(m.group(1).strip()).decode("utf-8")
    return json.loads(raw)


def norm(v):
    s = ("" if v is None else str(v)).strip().replace(",", "").replace("\u2212", "-")
    return "" if s == "-" else s


def check(case_name, fx, snap):
    problems = []
    page_head = snap["head"]
    parsed = list(csv.reader(io.StringIO(snap["csv"])))
    want_head = ["日期"] + page_head
    if parsed[0] != want_head:
        problems.append("表头不一致：文件 %r ≠ 页面 %r" % (parsed[0], want_head))

    body = parsed[1:]
    by_name = {}
    for row in body:
        by_name.setdefault((row[0], row[1]), []).append(row)

    for prow in snap["rows"]:
        day = fx["day"]
        key = (day, prow[0])
        if key not in by_name:
            problems.append("文件里找不到 %s %s" % key)
            continue
        crow = by_name[key].pop(0)
        if len(crow) != len(prow) + 1:
            problems.append("%s 列数不一致：文件 %d ≠ 页面 %d"
                            % (prow[0], len(crow), len(prow) + 1))
            continue
        for j, cell in enumerate(prow):
            # 原因列的「填原因」是按钮，不是数据，文件里不该有
            a = "" if (page_head[j] == "原因" and cell == "填原因") else norm(cell)
            b = norm(crow[j + 1])
            if a != b:
                problems.append("%s · %s：页面 %r ≠ 文件 %r"
                                % (prow[0], page_head[j], cell, crow[j + 1]))

    values = {r[1]: r for r in body if r[0] == fx["day"]}
    if fx["kind"] == "pachinko":
        for col in ("码表投珠", "出卡"):
            if col not in page_head:
                problems.append("页面缺列：" + col)
            if col not in parsed[0]:
                problems.append("文件缺列：" + col)
        if "码表投珠" in parsed[0]:
            k = parsed[0].index("码表投珠")
            if all((not r[k]) for r in values.values()):
                problems.append("文件里「码表投珠」全是空的（跟页面不一致）")
    return problems


def main():
    fixtures = [build_pachinko(), build_pusher()]
    html = make_html(fixtures)
    out = run_chrome(html)
    if "err" in out:
        print("页面脚本报错：", out["err"])
        return 1

    failed = 0
    for name, snap in (("弹珠机（投珠出卡）", out["pach"]), ("推币机", out["pusher"])):
        fx = fixtures[0] if name.startswith("弹珠机") else fixtures[1]
        probs = check(name, fx, snap)
        if probs:
            failed += 1
            print("FAIL", name)
            for p in probs[:20]:
                print("   -", p)
        else:
            print("PASS", name, "· 页面 %d 列 ≤> 文件 %d 列"
                  % (len(snap["head"]), len(snap["csv"].splitlines()[0].split(","))))

    if VERBOSE:
        print("\n弹珠机页面表头:", out["pach"]["head"])
        print("弹珠机文件首行:", out["pach"]["csv"].splitlines()[0])
        print("弹珠机文件 11-24 第一行:", out["pach"]["csv"].splitlines()[2])
        print("推币机页面表头:", out["pusher"]["head"])
        print("推币机文件首行:", out["pusher"]["csv"].splitlines()[0])

    print("=" * 60)
    print("导出/页面一致性：%s" % ("全部通过" if not failed else "有 %d 项不通过" % failed))
    print("=" * 60)
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
