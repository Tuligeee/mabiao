# -*- coding: utf-8 -*-
"""码表核对 · 全量单元测试

在临时库上跑，不碰服务器和本地正式库。
用法：python _tests/test_all.py        （只跑）
      python _tests/test_all.py -v     （每条都打印）
"""
import os
import sys
import json
import shutil
import sqlite3
import tempfile
import traceback
import uuid
from pathlib import Path

APP = os.environ.get("METER_APP") or str(Path(__file__).resolve().parent.parent / "app")
sys.path.insert(0, APP)

VERBOSE = "-v" in sys.argv
TMP = os.environ.get("METER_TEST_TMP") or tempfile.mkdtemp(prefix="meter_test_")
os.makedirs(TMP, exist_ok=True)

import server as S                                    # noqa: E402

S.DB = Path(TMP) / "test.db"
S.init_db()

RESULTS = []


# ----------------------------------------------------------------- 小工具
def mk_shop(name, provider="baodian", group=None, jj_base=None, jj_shop=None):
    sid = str(uuid.uuid4())
    with S.conn() as c:
        c.execute("INSERT INTO shops(id,name,created_at,provider,group_name,jj_base,jj_shop_id) "
                  "VALUES(?,?,?,?,?,?,?)", (sid, name, S.now(), provider, group, jj_base, jj_shop))
    S.ensure_codes()
    return sid


def mk_unit(shop_id, name, kind="pachinko", brand=None, from_day="1970-01-01",
            pos=None, active=1):
    with S.conn() as c:
        cur = c.execute(
            "INSERT INTO units(shop_id,name,kind,brand,from_day,active,pos,team) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (shop_id, name, kind, brand, from_day, active, pos, brand))
        uid = cur.lastrowid
    S.ensure_codes()
    return uid


def put_reading(shop_id, uid, day, **kw):
    S.save_readings(shop_id, day, [dict(unit_id=uid, **kw)])


def row_of(shop_id, day, name):
    d = S.recon(shop_id, day, "pachinko")
    for r in d["rows"]:
        if r["name"] == name:
            return r, d
    return None, d


def attach_comm(shop_id, uid, comm_id):
    """给机台挂一个卡头，并补上归属历史。"""
    with S.conn() as c:
        c.execute("INSERT OR REPLACE INTO comms(comm_id,unit_id,updated_at) VALUES(?,?,?)",
                  (comm_id, uid, S.now()))
        S.comm_owner_set(c, comm_id, uid, shop_id, "1970-01-01")


def card(shop_id, day, comm_id, up_native=0, up_give=0, down=0):
    with S.conn() as c:
        c.execute("INSERT OR REPLACE INTO card_daily(shop_id,day,comm_id,up_native,up_give,"
                  "up_a,up_b,down_qty,fetched_at) VALUES(?,?,?,?,?,0,0,?,?)",
                  (shop_id, day, comm_id, up_native, up_give, down, S.now()))


class Skip(Exception):
    pass


class Case:
    def __init__(self, name):
        self.name = name

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        if et is Skip:
            RESULTS.append((self.name, "SKIP", str(ev)))
            if VERBOSE:
                print("  SKIP", self.name, str(ev))
            return True
        if et is None:
            RESULTS.append((self.name, "PASS", ""))
            if VERBOSE:
                print("  PASS", self.name)
        else:
            msg = "%s: %s" % (type(ev).__name__, str(ev)[:120])
            RESULTS.append((self.name, "FAIL", msg))
            print("  FAIL", self.name, "->", msg)
            if VERBOSE:
                traceback.print_exc()
        return True


def eq(a, b, why=""):
    assert a == b, "%s 期望 %r 实际 %r" % (why or "值不符", b, a)


# ================================================================= 一、门店与编码
def t_shops():
    sid = mk_shop("测试店-编码")
    d = S.shop_row(sid)
    assert d["code"].startswith("S"), "门店应自动分配 S 编号，实际 %r" % d["code"]
    u1 = mk_unit(sid, "甲弹珠机-001", brand="甲")
    u2 = mk_unit(sid, "甲弹珠机-002", brand="甲")
    with S.conn() as c:
        a = c.execute("SELECT code,pos FROM units WHERE id=?", (u1,)).fetchone()
        b = c.execute("SELECT code,pos FROM units WHERE id=?", (u2,)).fetchone()
    eq(a["code"], d["code"] + "-001", "第一台编码")
    eq(b["code"], d["code"] + "-002", "第二台编码")
    eq(a["pos"], 1, "第一台 P 位")
    eq(b["pos"], 2, "第二台 P 位")


def t_pos_stable():
    sid = mk_shop("测试店-P位")
    mk_unit(sid, "乙弹珠机-001", brand="乙")
    mk_unit(sid, "乙弹珠机-002", brand="乙")
    with S.conn() as c:
        before = {r["name"]: r["pos"] for r in c.execute(
            "SELECT name,pos FROM units WHERE shop_id=?", (sid,))}
    mk_unit(sid, "乙弹珠机-003", brand="乙", pos=None)
    with S.conn() as c:
        after = {r["name"]: r["pos"] for r in c.execute(
            "SELECT name,pos FROM units WHERE shop_id=?", (sid,))}
    for k, v in before.items():
        eq(after[k], v, "加机台后老机台 P 位不能动：" + k)
    eq(after["乙弹珠机-003"], 3, "新机台接在后面")


def t_brand_grouping():
    """同机型的 -001/-002 要归到一组，P 位连续。"""
    sid = mk_shop("测试店-分组")
    for i in range(1, 6):
        mk_unit(sid, "丙双胞胎弹珠机-%03d" % i, brand="丙")
    with S.conn() as c:
        pos = [r["pos"] for r in c.execute(
            "SELECT pos FROM units WHERE shop_id=? ORDER BY name", (sid,))]
    eq(pos, [1, 2, 3, 4, 5], "同机型 P 位应 1..5")


# ================================================================= 二、卡头归属历史
def t_comm_new_same_day():
    sid = mk_shop("测试店-新卡头")
    uid = mk_unit(sid, "丁弹珠机-001", brand="丁")
    S.comm_owner_set(None, "C001", uid, sid, "2026-03-10") if False else None
    with S.conn() as c:
        c.execute("INSERT INTO comms(comm_id,unit_id,updated_at) VALUES(?,?,?)",
                  ("C001", uid, S.now()))
        S.comm_owner_set(c, "C001", uid, sid, "2026-03-10")
    eq(S.comms_on(uid, "2026-03-10"), ["C001"], "新增卡头当天就生效")
    eq(S.comms_on(uid, "2026-03-09"), [], "新增前一天不该有")


def t_comm_move_next_day():
    sid = mk_shop("测试店-搬卡头")
    u1 = mk_unit(sid, "戊弹珠机-001", brand="戊")
    u2 = mk_unit(sid, "戊弹珠机-002", brand="戊")
    with S.conn() as c:
        c.execute("INSERT INTO comms(comm_id,unit_id,updated_at) VALUES(?,?,?)",
                  ("C002", u1, S.now()))
        S.comm_owner_set(c, "C002", u1, sid, "2026-03-01")
        # 3-10 搬到 u2
        S.comm_owner_set(c, "C002", u2, sid, "2026-03-10")
    eq(S.comms_on(u1, "2026-03-10"), ["C002"], "搬迁当天旧机台还算")
    eq(S.comms_on(u2, "2026-03-10"), [], "搬迁当天新机台先不算")
    eq(S.comms_on(u1, "2026-03-11"), [], "次日旧机台不算")
    eq(S.comms_on(u2, "2026-03-11"), ["C002"], "次日新机台算")


def t_comm_history_backfill():
    """历史那天要用当时的归属，不能用现在的。"""
    sid = mk_shop("测试店-历史归属")
    u1 = mk_unit(sid, "己弹珠机-001", brand="己")
    u2 = mk_unit(sid, "己弹珠机-002", brand="己")
    with S.conn() as c:
        c.execute("INSERT INTO comms(comm_id,unit_id,updated_at) VALUES(?,?,?)",
                  ("C003", u2, S.now()))
        S.comm_owner_set(c, "C003", u1, sid, "2026-01-01")
        S.comm_owner_set(c, "C003", u2, sid, "2026-06-01")
    eq(S.comms_on(u1, "2026-03-01"), ["C003"], "3 月还归旧机台")
    eq(S.comms_on(u2, "2026-03-01"), [], "3 月新机台还没有")


# ================================================================= 三、读数与增量
def t_first_fill():
    sid = mk_shop("测试店-首填")
    uid = mk_unit(sid, "庚弹珠机-001", brand="庚")
    put_reading(sid, uid, "2026-04-01", in_r=1000, out_r=10, p_tokens=500, p_out=480, p_cards=20)
    r, _ = row_of(sid, "2026-04-01", "庚弹珠机-001")
    eq(r["status"], "第一次填", "首填状态")
    eq(r["meter_in"], None, "首填没有前一天，投币增量应为空")


def t_normal_delta():
    sid = mk_shop("测试店-正常")
    uid = mk_unit(sid, "辛弹珠机-001", brand="辛")
    attach_comm(sid, uid, "C900")
    card(sid, "2026-04-02", "C900", up_native=300)
    put_reading(sid, uid, "2026-04-01", in_r=1000, out_r=10, p_tokens=500, p_out=480, p_cards=20)
    put_reading(sid, uid, "2026-04-02", in_r=1300, out_r=15, p_tokens=900, p_out=800, p_cards=90)
    r, _ = row_of(sid, "2026-04-02", "辛弹珠机-001")
    eq(r["status"], "正常", "状态")
    eq(r["meter_in"], 300, "投币增量")
    eq(r["p"]["tokens"], 400, "投珠增量")
    eq(r["p"]["out"], 320, "出珠增量")
    eq(r["p"]["cards"], 70, "出卡增量")


def t_backwards():
    sid = mk_shop("测试店-倒退")
    uid = mk_unit(sid, "壬弹珠机-001", brand="壬")
    attach_comm(sid, uid, "C901")
    card(sid, "2026-04-02", "C901", up_native=-200)
    put_reading(sid, uid, "2026-04-01", in_r=1000, out_r=10, p_tokens=500, p_out=480, p_cards=20)
    put_reading(sid, uid, "2026-04-02", in_r=800, out_r=9, p_tokens=400, p_out=380, p_cards=10)
    r, _ = row_of(sid, "2026-04-02", "壬弹珠机-001")
    eq(r["status"], "读数比昨天小", "倒退要报出来")
    eq(r["meter_in"], -200, "倒退增量照样算")


def t_bead_only_delta():
    """回归：只抄珠/卡、没有投币的店也要能算增量。"""
    sid = mk_shop("测试店-只抄珠")
    uid = mk_unit(sid, "癸双胞胎弹珠机-001", brand="癸")
    put_reading(sid, uid, "2026-04-01", p_tokens=100, p_out=90, p_cards=10)
    put_reading(sid, uid, "2026-04-02", p_tokens=250, p_out=200, p_cards=40)
    r, _ = row_of(sid, "2026-04-02", "癸双胞胎弹珠机-001")
    eq(r["p"]["tokens"], 150, "投珠增量必须算出来")
    eq(r["p"]["out"], 110, "出珠增量必须算出来")
    eq(r["p"]["cards"], 30, "出卡增量必须算出来")
    eq(r["meter_in"], None, "没投币就该是空")


def t_partial_field():
    """前一天某字段为空，只影响这个字段。"""
    sid = mk_shop("测试店-缺字段")
    uid = mk_unit(sid, "子弹珠机-001", brand="子")
    put_reading(sid, uid, "2026-04-01", in_r=1000, p_tokens=500)
    put_reading(sid, uid, "2026-04-02", in_r=1200, p_tokens=600, p_out=400, p_cards=30)
    r, _ = row_of(sid, "2026-04-02", "子弹珠机-001")
    eq(r["meter_in"], 200, "有值的字段照算")
    eq(r["p"]["tokens"], 100, "投珠照算")
    eq(r["p"]["out"], None, "前一天没出珠，出珠增量应为空")
    eq(r["p"]["cards"], None, "前一天没出卡，出卡增量应为空")


def t_reset():
    sid = mk_shop("测试店-归零")
    uid = mk_unit(sid, "丑弹珠机-001", brand="丑")
    attach_comm(sid, uid, "C902")
    card(sid, "2026-04-02", "C902", up_native=30)
    put_reading(sid, uid, "2026-04-01", in_r=1000, out_r=10, p_tokens=500, p_out=480, p_cards=20)
    put_reading(sid, uid, "2026-04-02", in_r=30, out_r=2, p_tokens=40, p_out=35, p_cards=5)
    S.unit_reset_set(uid, "2026-04-02", True)
    r, _ = row_of(sid, "2026-04-02", "丑弹珠机-001")
    eq(r["status"], "正常", "归零后不该报倒退")
    eq(r["meter_in"], 30, "归零当天 = 读数本身")
    eq(r["p"]["tokens"], 40, "珠也按归零算")


# ================================================================= 四、生命周期
def t_unit_from_day():
    sid = mk_shop("测试店-新机台")
    mk_unit(sid, "寅弹珠机-001", brand="寅", from_day="2026-05-10")
    eq(len(S.recon(sid, "2026-05-09", "pachinko")["rows"]), 0, "上机前不该出现")
    eq(len(S.recon(sid, "2026-05-10", "pachinko")["rows"]), 1, "当天出现")


def t_unit_retire():
    sid = mk_shop("测试店-停用")
    uid = mk_unit(sid, "卯弹珠机-001", brand="卯")
    S.retire_unit(uid, "2026-05-20")
    days = [r["name"] for r in S.recon(sid, "2026-05-20", "pachinko")["rows"]]
    assert "卯弹珠机-001" in days, "停用当天还要在"
    days2 = [r["name"] for r in S.recon(sid, "2026-05-21", "pachinko")["rows"]]
    assert "卯弹珠机-001" not in days2, "次日起不在"


def t_unit_retired_today_hidden():
    """停用当天还没填数：核对页不再占位；历史日期照旧。"""
    sid = mk_shop("测试店-停用当天")
    uid = mk_unit(sid, "巳弹珠机-001", brand="巳")
    today = S.date.today().isoformat()
    S.retire_unit(uid, today)
    names = [r["name"] for r in S.recon(sid, today, "pachinko")["rows"]]
    assert "巳弹珠机-001" not in names, "停用当天没填数，核对页不该再占位"
    prev = (S.date.fromisoformat(today) - S.timedelta(days=1)).isoformat()
    names2 = [r["name"] for r in S.recon(sid, prev, "pachinko")["rows"]]
    assert "巳弹珠机-001" in names2, "历史日期仍要留着"


def t_split_owner():
    """拆分后归属跟着走：当天核对就按卡头分开，不再合成一台。"""
    sid = mk_shop("测试店-拆分归属")
    uid = mk_unit(sid, "午弹珠机-001", brand="午")
    attach_comm(sid, uid, "4001")
    attach_comm(sid, uid, "4002")
    with S.conn() as c:
        c.execute("UPDATE units SET shared=1 WHERE id=?", (uid,))
    today = S.date.today().isoformat()

    before = [r for r in S.recon(sid, today, "pachinko")["rows"] if r["name"].startswith("午弹珠机")]
    eq(len(before), 1, "拆之前是一台")
    eq(before[0]["comm_count"], 2, "两个卡头")

    S.split_unit(uid)
    after = [r for r in S.recon(sid, today, "pachinko")["rows"] if r["name"].startswith("午弹珠机")]
    eq(len(after), 2, "拆完两台")
    eq(sorted(r["comm_count"] for r in after), [1, 1], "一台一个卡头")


def t_merge_owner():
    """合并后归属跟着走：两个卡头并成一台，当天核对就一行。"""
    sid = mk_shop("测试店-合并归属")
    a = mk_unit(sid, "未弹珠机-001", brand="未")
    b = mk_unit(sid, "未弹珠机-002", brand="未")
    attach_comm(sid, a, "4101")
    attach_comm(sid, b, "4102")
    S.merge_units(sid, [a, b])
    today = S.date.today().isoformat()
    rows = [r for r in S.recon(sid, today, "pachinko")["rows"] if r["name"].startswith("未弹珠机")]
    eq(len(rows), 1, "合并后一台")
    eq(rows[0]["comm_count"], 2, "两个卡头")


# ================================================================= 五、弹珠机玩法
def t_four_fields_win():
    sid = mk_shop("测试店-四字段")
    S.save_brand_flags({"brand": "辰", "mode": "swipe_bead_card", "unit": "coin", "per_coin": 5})
    uid = mk_unit(sid, "辰弹珠机-001", brand="辰")
    put_reading(sid, uid, "2026-06-01", in_r=100, p_tokens=500, p_out=400, p_cards=10)
    put_reading(sid, uid, "2026-06-02", in_r=200, p_tokens=1600, p_out=1500, p_cards=20)
    r, _ = row_of(sid, "2026-06-02", "辰弹珠机-001")
    eq(r["meter_in"], 100, "投币增量")
    eq(r["p"]["tokens"], 1100, "投珠增量")
    eq(r["p"]["win"], 1100 - 100 * 5, "中奖出珠 = 出珠增量 − 投币增量×一币几珠")


def t_three_fields_no_win():
    sid = mk_shop("测试店-三字段")
    S.save_brand_flags({"brand": "巳", "mode": "bead_out_card", "unit": "coin", "per_coin": None})
    uid = mk_unit(sid, "巳双胞胎弹珠机-001", brand="巳")
    put_reading(sid, uid, "2026-06-01", p_tokens=100, p_out=90, p_cards=10)
    put_reading(sid, uid, "2026-06-02", p_tokens=300, p_out=280, p_cards=30)
    r, _ = row_of(sid, "2026-06-02", "巳双胞胎弹珠机-001")
    eq(r["fields"], ["p_tokens", "p_out", "p_cards"], "三字段玩法")
    eq(r["p"]["win"], None, "没有投币就不算中奖出珠")


def t_snapshot_per_coin():
    """改品牌配置后，历史读数仍按当时的一币几珠算。"""
    sid = mk_shop("测试店-快照")
    S.save_brand_flags({"brand": "午", "mode": "swipe_bead_card", "unit": "coin", "per_coin": 5})
    uid = mk_unit(sid, "午弹珠机-001", brand="午")
    put_reading(sid, uid, "2026-06-01", in_r=100, p_tokens=500, p_out=400, p_cards=10)
    put_reading(sid, uid, "2026-06-02", in_r=200, p_tokens=1600, p_out=1500, p_cards=20)
    S.save_brand_flags({"brand": "午", "mode": "swipe_bead_card", "unit": "coin", "per_coin": 99})
    r, _ = row_of(sid, "2026-06-02", "午弹珠机-001")
    eq(r["p"]["win"], 1100 - 100 * 5, "历史快照不能被新配置改掉")


def t_out_only_win():
    """只计中奖的机器：总出珠就是中奖，不用一币几珠。"""
    sid = mk_shop("测试店-只计中奖")
    S.save_brand_flags({"brand": "未", "mode": "swipe_bead_card", "out_incl": False})
    uid = mk_unit(sid, "未弹珠机-001", brand="未")
    put_reading(sid, uid, "2026-08-01", in_r=100, p_tokens=500, p_out=300, p_cards=10)
    put_reading(sid, uid, "2026-08-02", in_r=300, p_tokens=1200, p_out=800, p_cards=40)
    r, _ = row_of(sid, "2026-08-02", "未弹珠机-001")
    eq(r["p"]["win"], 500, "只计中奖：中奖出珠 = 总出珠增量")
    eq(r["p"]["keep"], 200, "实际收珠 = 投珠 700 - 中奖 500")


def t_card_and_cost_rate():
    """出卡率 = 出卡 ÷ 中奖出珠；成本率 = 卡成本 ÷ (实际收珠 × 珠值)。"""
    sid = mk_shop("测试店-两率")
    S.save_brand_flags({"brand": "申", "mode": "swipe_bead_card", "out_incl": False})
    S.save_shop_values({"shop_id": sid, "bead_value": 0.5, "card_value": 0.2})
    uid = mk_unit(sid, "申弹珠机-001", brand="申")
    put_reading(sid, uid, "2026-09-01", in_r=100, p_tokens=500, p_out=300, p_cards=10)
    put_reading(sid, uid, "2026-09-02", in_r=300, p_tokens=1200, p_out=800, p_cards=60)
    r, _ = row_of(sid, "2026-09-02", "申弹珠机-001")
    p = r["p"]
    eq(p["win"], 500, "中奖出珠")
    eq(p["cards"], 50, "出卡增量")
    eq(round(p["card_rate"], 4), 0.1, "出卡率 = 50 ÷ 500")
    # 实际收珠 = 700 − 500 = 200；营收 = 200×0.5 = 100；成本 = 50×0.2 = 10
    eq(round(p["cost_rate"], 4), 0.1, "成本率 = 10 ÷ 100")


def t_rate_blank_when_missing():
    """没配珠值卡值、或中奖为 0 时，两率留空不报错。"""
    sid = mk_shop("测试店-两率空")
    S.save_brand_flags({"brand": "酉", "mode": "swipe_bead_card", "out_incl": False})
    uid = mk_unit(sid, "酉弹珠机-001", brand="酉")
    put_reading(sid, uid, "2026-10-01", in_r=100, p_tokens=500, p_out=0, p_cards=5)
    put_reading(sid, uid, "2026-10-02", in_r=200, p_tokens=600, p_out=0, p_cards=9)
    r, _ = row_of(sid, "2026-10-02", "酉弹珠机-001")
    eq(r["p"]["win"], 0, "中奖 0")
    eq(r["p"]["card_rate"], None, "中奖 0 时出卡率留空")
    eq(r["p"]["cost_rate"], None, "没配珠值卡值时成本率留空")


# ================================================================= 六、取数分流
def t_manual_skip():
    sid = mk_shop("测试店-纯手工", provider="manual")
    r = S.fetch_day(sid, "2026-07-01")
    eq(r.get("provider"), "manual", "纯手工门店")
    assert "skipped" in r, "纯手工应直接跳过"
    with S.conn() as c:
        n = c.execute("SELECT COUNT(*) n FROM card_daily WHERE shop_id=?", (sid,)).fetchone()["n"]
    eq(n, 0, "不应写卡头数据")


def t_jingjian_fetch():
    import types
    fake = types.ModuleType("jingjian")
    fake.DEFAULT_BASE = "http://fake"
    fake.day_summary = lambda base, shop, day, chain="": [
        {"machine": "未弹珠机-001", "coins": 4400, "count": 45},
        {"machine": "娃娃机-001", "coins": 300, "count": 3},
    ]
    fake.day_consoles = lambda base, shop, day, chain="": [
        {"machine": "未弹珠机-001", "pos": 1, "console_id": "c1", "coins": 4400,
         "count": 45, "out_coins": 0, "winning": 0},
        {"machine": "娃娃机-001", "pos": 1, "console_id": "c2", "coins": 300,
         "count": 3, "out_coins": 0, "winning": 0},
    ]
    sys.modules["jingjian"] = fake
    try:
        sid = mk_shop("测试店-鲸舰", provider="jingjian", jj_base="http://fake", jj_shop="23738")
        r = S.fetch_day(sid, "2026-07-02")
        eq(r.get("provider"), "jingjian", "鲸舰门店")
        eq(r.get("machines"), 2, "应落 2 台")
        with S.conn() as c:
            row = c.execute("SELECT coins FROM machine_daily WHERE shop_id=? AND machine_name=?",
                            (sid, "未弹珠机-001")).fetchone()
        eq(row["coins"], 4400, "投币额原样落库（不除 100）")
    finally:
        sys.modules.pop("jingjian", None)


def t_jingjian_match():
    sid = mk_shop("测试店-鲸舰匹配", provider="jingjian")
    uid = mk_unit(sid, "未弹珠机-001", brand="未")
    mk_unit(sid, "未弹珠机-002", brand="未")
    with S.conn() as c:
        c.execute("INSERT INTO machine_daily(shop_id,day,machine_name,coins,play_count,fetched_at) "
                  "VALUES(?,?,?,?,?,?)", (sid, "2026-07-03", "未弹珠机-001", 930, 3, S.now()))
    r, _ = row_of(sid, "2026-07-03", "未弹珠机-001")
    eq(r["up"], 930, "同名机台取到卡头上币")
    r2, _ = row_of(sid, "2026-07-03", "未弹珠机-002")
    eq(r2["up"], None, "没有对应机台就没有上币")
    eq(r2["status"], "今天还没填", "没填读数")


# ================================================================= 七、冻结
def t_freeze():
    sid = mk_shop("测试店-冻结")
    uid = mk_unit(sid, "申弹珠机-001", brand="申")
    put_reading(sid, uid, "2026-08-01", in_r=100, p_tokens=1000)
    put_reading(sid, uid, "2026-08-02", in_r=300, p_tokens=1200)
    S.recon_freeze(sid, "2026-08-02")
    before, d1 = row_of(sid, "2026-08-02", "申弹珠机-001")
    eq(before["meter_in"], 200, "冻结时的数")
    assert d1["frozen"], "应带 frozen 标记"
    # 改读数：冻结后核对结果不能变
    put_reading(sid, uid, "2026-08-01", in_r=50, p_tokens=1000)
    after, _ = row_of(sid, "2026-08-02", "申弹珠机-001")
    eq(after["meter_in"], 200, "冻结后改读数不影响核对")
    S.recon_unfreeze(sid, "2026-08-02")
    live, _ = row_of(sid, "2026-08-02", "申弹珠机-001")
    eq(live["meter_in"], 250, "解冻后回到实时")


# ================================================================= 八、权限
def t_store_isolation():
    sid_a = mk_shop("测试店-甲店")
    sid_b = mk_shop("测试店-乙店")
    S.ACTOR.me = {"id": "ST01", "name": "测试店-甲店",
                  "type": "store", "shop": sid_a}
    try:
        try:
            S.guard_shop(sid_b)
            eq(True, False, "越权访问应拒绝")
        except S.AuthError:
            pass
        eq(S.guard_shop(sid_a), sid_a, "自己店能访问")
        eq(S.guard_shop(""), sid_a, "不带 shop_id 时默认自己店")
    finally:
        S.ACTOR.me = None
    # 完全没绑定的账号要拒绝
    S.ACTOR.me = {"id": "ST99", "name": "没绑店", "type": "store", "shop": None}
    try:
        try:
            S.guard_shop(sid_a)
            eq(True, False, "未绑定的账号应拒绝")
        except S.AuthError:
            pass
    finally:
        S.ACTOR.me = None


def t_comm_not_stolen():
    """同号卡头出现在两家店的后台清单里时，同步不能把别家店的卡头抢过来。"""
    a = mk_shop("测试店-抢卡头甲")
    b = mk_shop("测试店-抢卡头乙")
    S.save_unit({"shop_id": a, "name": "甲机", "kind": "pusher", "comms": "901234"})
    res = S.save_unit({"shop_id": b, "name": "乙机", "kind": "pusher", "comms": "901234"})
    eq(res.get("skipped"), ["901234"], "重复卡头被跳过")
    with S.conn() as c:
        own = c.execute("SELECT u.shop_id FROM comms m JOIN units u ON u.id=m.unit_id "
                        "WHERE m.comm_id='901234'").fetchone()[0]
    eq(own, a, "卡头仍归第一家")


def t_store_cannot_set_provider():
    sid = mk_shop("测试店-越权")
    S.ACTOR.me = {"id": "ST02", "name": "x", "type": "store", "shop": sid}
    try:
        try:
            S.api_shop_provider({"shop_id": sid, "provider": "manual"})
            eq(True, False, "店长不该改得了取数方式")
        except S.AuthError:
            pass
    finally:
        S.ACTOR.me = None
    S.api_shop_provider({"shop_id": sid, "provider": "manual"})
    eq(S.shop_row(sid)["provider"], "manual", "总部能改")


def t_provider_validation():
    sid = mk_shop("测试店-非法值")
    try:
        S.api_shop_provider({"shop_id": sid, "provider": "whatever"})
        eq(True, False, "非法值应报错")
    except RuntimeError:
        pass


# ================================================================= 九、其他
def t_qr():
    try:
        import qrcode  # noqa: F401
    except ImportError:
        raise Skip("本机没装 qrcode（服务器上有）")
    png = S.qr_label_png("https://mancado.cn/cwsj/meter/#u=abc", "测试弹珠机-001")
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "应返回 PNG"
    assert len(png) > 2000, "二维码图片太小，可能没画上"


def t_entry_payload():
    sid = mk_shop("测试店-录入")
    uid = mk_unit(sid, "酉弹珠机-001", brand="酉")
    put_reading(sid, uid, "2026-09-01", in_r=10, p_tokens=100)
    e = S.api_entry({"shop_id": [sid], "day": ["2026-09-01"], "kind": ["pachinko"]})
    eq(len(e["rows"]), 1, "录入行数")
    eq(e["rows"][0]["unit_id"], uid, "机台 id")


def t_summary_counts():
    sid = mk_shop("测试店-汇总")
    u1 = mk_unit(sid, "戌弹珠机-001", brand="戌")
    u2 = mk_unit(sid, "戌弹珠机-002", brand="戌")
    u3 = mk_unit(sid, "戌弹珠机-003", brand="戌")
    for i, u in enumerate((u1, u2, u3), 1):
        attach_comm(sid, u, "C90%d" % i)
        card(sid, "2026-10-02", "C90%d" % i, up_native=10)
    for u, v in ((u1, 100), (u2, 200), (u3, 300)):
        put_reading(sid, u, "2026-10-01", in_r=v, p_tokens=1000)
        put_reading(sid, u, "2026-10-02", in_r=v + 10, p_tokens=1100)
    _, d = row_of(sid, "2026-10-02", "戌弹珠机-001")
    eq(d["summary"]["total"], 3, "总台数")
    eq(d["summary"]["ok"], 3, "正常台数")


def t_store_can_edit():
    sid = mk_shop("测试店-可编辑")
    uid = mk_unit(sid, "亥弹珠机-001", brand="亥")
    S.ACTOR.me = {"id": "ST03", "name": "测试店-可编辑", "type": "store", "shop": sid}
    try:
        import datetime as _dt
        today = _dt.date.today().isoformat()
        yday = S.edit_day()
        eq(S.can_edit(today), True, "今天可以填（晚上关店当天填）")
        eq(S.can_edit(yday), True, "昨天可以填")
        eq(S.can_edit("2020-01-01"), False, "前天及更早不能填")
        try:
            S.save_readings(sid, "2020-01-01", [{"unit_id": uid, "in_r": 1}])
            eq(True, False, "门店填老日期应被拒")
        except RuntimeError:
            pass
    finally:
        S.ACTOR.me = None
    eq(S.can_edit("2020-01-01"), True, "总部不受限制")


def t_baodian_routing():
    """宝点门店照样走宝点，不被鲸舰逻辑影响。"""
    calls = []

    class FakeBd:
        def shops(self):
            return [{"shopName": "测试店-宝点取数", "id": "BD-Shop-1"}]

        def fetch_up(self, shop, day):
            calls.append("up")
            return [{"commId": "C800", "actualNativeCoinNum": 100, "actualGiveCoinNum": 20,
                     "giveCoinA": 0, "giveCoinB": 0, "deviceName": "宝点机-001"}]

        def fetch_down(self, shop, day):
            calls.append("down")
            return []

    orig = S.baodian
    S.baodian = lambda sid="": FakeBd()
    try:
        sid = mk_shop("测试店-宝点取数", provider="baodian")
        S.fetch_day(sid, "2026-07-04")
        eq(calls, ["up", "down"], "宝点门店要调上币+下币")
        with S.conn() as c:
            row = c.execute("SELECT up_native,up_give FROM card_daily WHERE shop_id=? AND comm_id='C800'",
                            (sid,)).fetchone()
        eq(row["up_native"], 100, "上币落库")
        eq(row["up_give"], 20, "赠送落库")
    finally:
        S.baodian = orig


def t_recon_range():
    sid = mk_shop("测试店-区间")
    uid = mk_unit(sid, "亥弹珠机-002", brand="亥")
    attach_comm(sid, uid, "C910")
    for d, v in (("2026-11-01", 100), ("2026-11-02", 130), ("2026-11-03", 160)):
        put_reading(sid, uid, d, in_r=v, p_tokens=1000)
        card(sid, d, "C910", up_native=30)
    r = S.api_recon_range({"shop_id": [sid], "kind": ["pachinko"],
                          "from": ["2026-11-01"], "to": ["2026-11-03"]})
    days = sorted({x["day"] for x in r.get("rows", [])})
    eq(days, ["2026-11-01", "2026-11-02", "2026-11-03"], "区间应覆盖 3 天")
    # 导出要跟核对页同列：区间行必须带上玩法字段和珠/卡指标
    row = [x for x in r["rows"] if x["day"] == "2026-11-02"][0]
    eq(row["kind"], "pachinko", "区间行带机型")
    assert row["fields"], "区间行要带玩法字段"
    assert row["p"] is not None, "区间行要带珠/卡指标"
    eq(row["p"]["tokens"], 0, "投珠两天一样 → 增量 0")
    eq(row["status"], "正常", "有卡头有读数 → 正常")


CASES = [
    ("门店编号与机台编码自动分配", t_shops),
    ("加机台后老 P 位不变", t_pos_stable),
    ("同机型 P 位连续", t_brand_grouping),
    ("新卡头当天生效", t_comm_new_same_day),
    ("卡头搬迁次日起效", t_comm_move_next_day),
    ("历史那天用当时归属", t_comm_history_backfill),
    ("首次填：状态第一次填", t_first_fill),
    ("正常两天：增量正确", t_normal_delta),
    ("读数倒退要报出来", t_backwards),
    ("只抄珠卡也能算增量（回归）", t_bead_only_delta),
    ("缺字段只影响该字段", t_partial_field),
    ("归零当天按读数本身算", t_reset),
    ("机台上机日之前不出现", t_unit_from_day),
    ("停用当天在、次日起不在", t_unit_retire),
    ("停用当天没填数就不占位", t_unit_retired_today_hidden),
    ("拆分后核对按卡头分开", t_split_owner),
    ("合并后核对并成一台", t_merge_owner),
    ("四字段玩法算中奖出珠", t_four_fields_win),
    ("三字段玩法不算中奖出珠", t_three_fields_no_win),
    ("改一币几珠不影响历史", t_snapshot_per_coin),
    ("只计中奖的机器中奖=总出珠", t_out_only_win),
    ("出卡率与成本率", t_card_and_cost_rate),
    ("两率缺值留空", t_rate_blank_when_missing),
    ("纯手工门店不取数", t_manual_skip),
    ("鲸舰取数落机台日汇总", t_jingjian_fetch),
    ("鲸舰按机台名对上上币", t_jingjian_match),
    ("冻结后改读数不影响核对", t_freeze),
    ("店长只能碰自己店", t_store_isolation),
    ("同号卡头不被别家店抢走", t_comm_not_stolen),
    ("店长不能改取数方式", t_store_cannot_set_provider),
    ("取数方式非法值被拒", t_provider_validation),
    ("二维码输出 PNG", t_qr),
    ("录入接口返回机台行", t_entry_payload),
    ("核对汇总台数正确", t_summary_counts),
    ("门店可填今天和昨天", t_store_can_edit),
    ("宝点门店仍走宝点", t_baodian_routing),
    ("区间核对覆盖每一天", t_recon_range),
]


def main():
    print("临时库:", S.DB)
    for name, fn in CASES:
        with Case(name):
            fn()
    passed = sum(1 for _, st, _ in RESULTS if st == "PASS")
    skipped = sum(1 for _, st, _ in RESULTS if st == "SKIP")
    failed = [(n, m) for n, st, m in RESULTS if st == "FAIL"]
    print("\n" + "=" * 60)
    print("通过 %d / %d（跳过 %d）" % (passed, len(RESULTS), skipped))
    if failed:
        print("\n失败明细:")
        for n, m in failed:
            print("  -", n, "|", m)
    print("=" * 60)
    shutil.rmtree(TMP, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
