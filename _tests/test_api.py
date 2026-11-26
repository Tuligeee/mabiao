# -*- coding: utf-8 -*-
"""码表核对 · 管理与接口层单元测试（补充 test_all.py 没覆盖的路由）

临时库，不碰正式数据。
"""
import os
import sys
import json
import shutil
import tempfile
import traceback
import uuid
from pathlib import Path

APP = os.environ.get("METER_APP") or str(Path(__file__).resolve().parent.parent / "app")
sys.path.insert(0, APP)
TMP = tempfile.mkdtemp(prefix="meter_api_")

import server as S                                    # noqa: E402

S.DB = Path(TMP) / "test.db"
S.init_db()

RESULTS = []
VERBOSE = "-v" in sys.argv


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
            return True
        if et is None:
            RESULTS.append((self.name, "PASS", ""))
            if VERBOSE:
                print("  PASS", self.name)
        else:
            RESULTS.append((self.name, "FAIL", "%s: %s" % (type(ev).__name__, str(ev)[:120])))
            print("  FAIL", self.name, "->", "%s: %s" % (type(ev).__name__, str(ev)[:120]))
            if VERBOSE:
                traceback.print_exc()
        return True


ALLOW_SHOPS = []


class FakeBd:
    def shops(self):
        return [{"shopName": n, "id": "BD-" + n, "customerId": "C0", "customerName": "测试集团"}
                for n in ALLOW_SHOPS]

    def fetch_up(self, shop, day):
        return []

    def fetch_down(self, shop, day):
        return []


def with_fake_bd():
    orig = S.baodian
    S.baodian = lambda sid="": FakeBd()
    return orig


def eq(a, b, why=""):
    assert a == b, "%s 期望 %r 实际 %r" % (why or "值不符", b, a)


def truth(v, why=""):
    assert v, why or "应为真"


def mk_shop(name, provider="baodian"):
    sid = str(uuid.uuid4())
    with S.conn() as c:
        c.execute("INSERT INTO shops(id,name,created_at,provider) VALUES(?,?,?,?)",
                  (sid, name, S.now(), provider))
    S.ensure_codes()
    return sid


def mk_unit(shop_id, name, kind="pachinko", brand="甲", comm="C%s" % uuid.uuid4().hex[:6]):
    with S.conn() as c:
        uid = c.execute(
            "INSERT INTO units(shop_id,name,kind,brand,from_day,active) "
            "VALUES(?,?,?,?,'1970-01-01',1)", (shop_id, name, kind, brand)).lastrowid
        c.execute("INSERT INTO comms(comm_id,unit_id,updated_at) VALUES(?,?,?)",
                  (comm, uid, S.now()))
        S.comm_owner_set(c, comm, uid, shop_id, "1970-01-01")
    S.ensure_codes()
    return uid


# ------------------------------------------------------------------ 门店管理
def t_save_shop():
    # 宝点里没有这家店 → 必须拒绝（不能凭空建门店）
    try:
        S.save_shop({"name": "接口店-宝点没有"})
        eq(True, False, "宝点里没有的门店应被拒")
    except RuntimeError:
        pass
    ALLOW_SHOPS.append("接口店-新建")
    orig = with_fake_bd()
    try:
        r = S.save_shop({"name": "接口店-新建"})
        truth(r.get("ok"), "应返回 ok")
        S.ensure_codes()
        row = S.shop_row_by_name("接口店-新建")
        truth(row, "门店应建出来")
        truth(row["code"].startswith("S"), "应自动分配编号")
        S.save_shop({"name": "接口店-新建"})
        with S.conn() as c:
            n2 = c.execute("SELECT COUNT(*) n FROM shops WHERE name='接口店-新建'").fetchone()["n"]
        eq(n2, 1, "同名不应重复建")
    finally:
        S.baodian = orig


def t_save_shop_values():
    sid = mk_shop("接口店-珠卡价值")
    S.save_shop_values({"shop_id": sid, "bead_value": 0.5, "card_value": 2, "beads_per_coin": 20})
    row = S.shop_row(sid)
    eq(row["bead_value"], 0.5, "珠值")
    eq(row["card_value"], 2, "卡值")
    eq(row["beads_per_coin"], 20, "一币几珠")


def t_remove_shop():
    sid = mk_shop("接口店-待删")
    uid = mk_unit(sid, "甲弹珠机-001")
    S.save_readings(sid, "2026-02-01", [{"unit_id": uid, "in_r": 10}])
    S.remove_shop(sid)
    assert S.shop_row(sid) is None, "门店应删掉"
    with S.conn() as c:
        eq(c.execute("SELECT COUNT(*) n FROM units WHERE shop_id=?", (sid,)).fetchone()["n"], 0,
           "机台应一起删")
        eq(c.execute("SELECT COUNT(*) n FROM readings WHERE shop_id=?", (sid,)).fetchone()["n"], 0,
           "读数应一起删")


# ------------------------------------------------------------------ 机台管理
def t_save_unit_new():
    sid = mk_shop("接口店-建机台")
    r = S.save_unit({"shop_id": sid, "name": "乙弹珠机-001", "comms": "M1001",
                     "kind": "pachinko", "brand": "乙"})
    truth(r.get("ok"), "应成功")
    with S.conn() as c:
        u = c.execute("SELECT * FROM units WHERE shop_id=? AND name='乙弹珠机-001'",
                      (sid,)).fetchone()
    S.ensure_codes()
    with S.conn() as c:
        u = c.execute("SELECT * FROM units WHERE shop_id=? AND name='乙弹珠机-001'",
                      (sid,)).fetchone()
    truth(u, "机台应建出来")
    truth(u["code"], "应有编号")
    import datetime as _dt
    eq(S.comms_on(u["id"], _dt.date.today().isoformat()), ["M1001"], "卡头归属应写进去")


def t_save_unit_needs_comm():
    sid = mk_shop("接口店-空卡头")
    try:
        S.save_unit({"shop_id": sid, "name": "丙弹珠机-001", "comms": "", "kind": "pachinko"})
        eq(True, False, "没卡头应该被拒")
    except RuntimeError:
        pass


def t_save_unit_move_comm():
    """换卡头：旧卡头当天还在，次日起归新机。"""
    sid = mk_shop("接口店-换卡头")
    u1 = mk_unit(sid, "丁弹珠机-001", comm="M2001")
    u2 = mk_unit(sid, "丁弹珠机-002", comm="M2002")
    S.save_unit({"shop_id": sid, "name": "丁弹珠机-002", "comms": "M2002,M2001",
                 "kind": "pachinko", "brand": "丁", "id": u2})
    today = __import__("datetime").date.today().isoformat()
    assert "M2001" in S.comms_on(u1, today), "搬迁当天旧机台还在"
    tom = (__import__("datetime").date.today() + __import__("datetime").timedelta(days=1)).isoformat()
    assert "M2001" not in S.comms_on(u1, tom), "次日起旧机台不再有"
    assert "M2001" in S.comms_on(u2, tom), "次日起归新机台"


def t_save_unit_rename_keeps_code():
    sid = mk_shop("接口店-改名")
    uid = mk_unit(sid, "戊弹珠机-001", comm="M3001")
    with S.conn() as c:
        code = c.execute("SELECT code FROM units WHERE id=?", (uid,)).fetchone()["code"]
    S.save_unit({"shop_id": sid, "name": "戊弹珠机-改名后", "comms": "M3001",
                 "kind": "pachinko", "brand": "戊", "id": uid})
    with S.conn() as c:
        row = c.execute("SELECT name,code FROM units WHERE id=?", (uid,)).fetchone()
    eq(row["name"], "戊弹珠机-改名后", "名字改了")
    eq(row["code"], code, "编号不能变（二维码贴纸已经贴出去了）")


def t_split_unit():
    sid = mk_shop("接口店-拆分")
    uid = mk_unit(sid, "己弹珠机-001", comm="M4001")
    with S.conn() as c:
        c.execute("INSERT INTO comms(comm_id,unit_id,updated_at) VALUES(?,?,?)",
                  ("M4002", uid, S.now()))
        S.comm_owner_set(c, "M4002", uid, sid, "1970-01-01")
    r = S.split_unit(uid)
    truth(r.get("ok"), "拆分应成功")
    with S.conn() as c:
        n = c.execute("SELECT COUNT(*) n FROM units WHERE shop_id=? AND active=1",
                      (sid,)).fetchone()["n"]
    eq(n, 2, "一块码表拆成两台")


def t_merge_units():
    sid = mk_shop("接口店-合并")
    u1 = mk_unit(sid, "庚弹珠机-001", comm="M5001")
    u2 = mk_unit(sid, "庚弹珠机-002", comm="M5002")
    S.save_readings(sid, "2026-02-02", [{"unit_id": u2, "in_r": 99}])
    r = S.merge_units(sid, [u1, u2], "庚弹珠机-合并后")
    truth(r.get("ok"), "合并应成功")
    with S.conn() as c:
        n = c.execute("SELECT COUNT(*) n FROM units WHERE shop_id=? AND active=1",
                      (sid,)).fetchone()["n"]
        kept = c.execute("SELECT id FROM units WHERE shop_id=? AND active=1", (sid,)).fetchone()["id"]
        rd = c.execute("SELECT in_r FROM readings WHERE unit_id=?",
                       (kept,)).fetchall()
    eq(n, 1, "合并后只剩一台")
    truth(len(rd) >= 1, "读数要带过去，不能丢")


def t_remove_unit():
    sid = mk_shop("接口店-删机台")
    uid = mk_unit(sid, "辛弹珠机-001", comm="M6001")
    S.save_readings(sid, "2026-02-03", [{"unit_id": uid, "in_r": 5}])
    S.remove_unit(uid)
    with S.conn() as c:
        eq(c.execute("SELECT COUNT(*) n FROM units WHERE id=?", (uid,)).fetchone()["n"], 0, "机台删了")
        eq(c.execute("SELECT COUNT(*) n FROM readings WHERE unit_id=?", (uid,)).fetchone()["n"], 1,
           "读数要留档（只标记不删除）")


def t_save_machine():
    sid = mk_shop("接口店-改机台")
    uid = mk_unit(sid, "壬弹珠机-001", comm="M7001")
    r = S.save_machine({"shop_id": sid, "ids": [uid], "name": "壬弹珠机-001",
                        "comms": "M7001"})
    truth(r.get("ok"), "改机台应成功")
    with S.conn() as c:
        row = c.execute("SELECT name FROM units WHERE id=?", (uid,)).fetchone()
    truth(row["name"].startswith("壬弹珠机-001"), "名字要保留原名")
    truth("M7001" in row["name"], "名字后面带上通讯ID（与宝点一致）")


# ------------------------------------------------------------------ 核对附加
def t_recon_note():
    sid = mk_shop("接口店-备注")
    uid = mk_unit(sid, "癸弹珠机-001", comm="M8001")
    S.save_readings(sid, "2026-02-04", [{"unit_id": uid, "in_r": 100, "p_tokens": 500}])
    S.save_readings(sid, "2026-02-05", [{"unit_id": uid, "in_r": 300, "p_tokens": 900}])
    with S.conn() as c:
        c.execute("INSERT INTO card_daily(shop_id,day,comm_id,up_native,up_give,down_qty,fetched_at)"
                  " VALUES(?,?,?,?,0,0,?)", (sid, "2026-02-05", "M8001", 200, S.now()))
    d = S.recon(sid, "2026-02-05", "pachinko")
    row = d["rows"][0]
    S.api_recon_note({"shop_id": sid, "day": "2026-02-05", "unit_id": uid,
                      "reason": "机台故障", "note": "卡住了半小时"})
    d2 = S.recon(sid, "2026-02-05", "pachinko")
    eq(d2["rows"][0]["reason"], "机台故障", "原因应存下来")
    eq(d2["rows"][0]["reason_note"], "卡住了半小时", "备注应存下来")


def t_audit():
    sid = mk_shop("接口店-留痕")
    uid = mk_unit(sid, "子推币机-001", comm="M9001", kind="pusher")
    S.save_readings(sid, "2026-02-06", [{"unit_id": uid, "in_r": 100}])
    S.save_readings(sid, "2026-02-06", [{"unit_id": uid, "in_r": 150}])
    a = S.api_audit({"shop_id": [sid]})
    truth(len(a["rows"]) >= 1, "改过就该有痕迹")
    eq(a["rows"][0]["old_v"], 100, "记下旧值")
    eq(a["rows"][0]["new_v"], 150, "记下新值")


def t_confirm_units():
    sid = mk_shop("接口店-确认清单")
    u1 = mk_unit(sid, "丑弹珠机-001", comm="MA001")
    u2 = mk_unit(sid, "丑弹珠机-002", comm="MA002")
    S.confirm_units(sid, [u1])
    with S.conn() as c:
        a = c.execute("SELECT active FROM units WHERE id=?", (u1,)).fetchone()["active"]
        b = c.execute("SELECT active FROM units WHERE id=?", (u2,)).fetchone()["active"]
        t = c.execute("SELECT units_confirmed FROM shops WHERE id=?", (sid,)).fetchone()["units_confirmed"]
    eq(a, 1, "勾上的启用")
    eq(b, 0, "没勾的停用")
    truth(t, "要记确认时间")


# ------------------------------------------------------------------ 查询/概览
def t_api_kinds():
    sid = mk_shop("接口店-分类")
    mk_unit(sid, "寅弹珠机-001", comm="MB001")
    mk_unit(sid, "寅推币机-001", comm="MB002", kind="pusher")
    d = S.api_kinds({"shop_id": [sid]})
    by = {x["kind"]: x for x in d["kinds"]}
    eq(by["pachinko"]["units"], 1, "弹珠机 1 台")
    eq(by["pusher"]["units"], 1, "推币机 1 台")


def t_api_unit_by_code():
    sid = mk_shop("接口店-扫码")
    uid = mk_unit(sid, "卯弹珠机-001", comm="MC001")
    with S.conn() as c:
        code = c.execute("SELECT code FROM units WHERE id=?", (uid,)).fetchone()["code"]
    d = S.api_unit({"c": [code]})
    truth(d.get("machine"), "扫码应找到机台")
    eq(d["machine"]["code"], code, "找对机台编号")
    truth(d.get("rows"), "应带出这块码表的行")


def t_api_overview():
    sid = mk_shop("接口店-全链")
    uid = mk_unit(sid, "辰弹珠机-001", comm="MD001")
    S.save_readings(sid, "2026-02-08", [{"unit_id": uid, "in_r": 100, "p_tokens": 1000}])
    S.save_readings(sid, "2026-02-09", [{"unit_id": uid, "in_r": 200, "p_tokens": 1200}])
    d = S.api_overview("2026-02-09")
    eq(d["day"], "2026-02-09", "日期")
    mine = [r for r in d["rows"] if r["shop"] == "接口店-全链"]
    truth(mine, "全链里应有这家店")
    eq(mine[0]["units"], 1, "台数 1")


def t_store_cannot_overview():
    sid = mk_shop("接口店-门店越权")
    S.ACTOR.me = {"id": "ST77", "name": "接口店-门店越权", "type": "store", "shop": sid}
    try:
        try:
            S.api_overview("2026-02-09")
            eq(True, False, "门店不该看全链")
        except S.AuthError:
            pass
    finally:
        S.ACTOR.me = None


def t_bootstrap_shape():
    sid = mk_shop("接口店-引导")
    mk_unit(sid, "巳弹珠机-001", comm="ME001")
    S.meta_set("shop_id", sid)
    b = S.api_bootstrap()
    for k in ("shops", "units", "edit_day", "max_day", "today", "shop_id"):
        truth(k in b, "bootstrap 缺字段 " + k)
    eq(b["max_day"], __import__("datetime").date.today().isoformat(), "最晚可选到今天")
    prov = {x["provider"] for x in b["shops"] if x["id"] == sid}
    truth(prov, "门店要带 provider")


def t_switch_shop():
    ALLOW_SHOPS.append("接口店-A")
    orig = with_fake_bd()
    try:
        sid = S.ensure_shop("接口店-A")
        S.meta_set("shop_id", sid)
        eq(S.meta_get("shop_id"), sid, "切店写进 meta")
    finally:
        S.baodian = orig


# ------------------------------------------------------------------ 服务/杂项
def t_setup_shape():
    d = S.api_setup()
    for k in ("shops", "units", "step1_done", "step2_done"):
        truth(k in d, "setup 缺字段 " + k)


def t_photos_empty():
    sid = mk_shop("接口店-照片")
    d = S.api_photos({"shop_id": [sid], "day": ["2026-02-10"], "unit_id": ["0"]})
    eq(d.get("photos"), [], "没拍过就是空列表")


def t_photo_save():
    import base64
    sid = mk_shop("接口店-拍照")
    uid = mk_unit(sid, "午弹珠机-001", comm="MF001")
    jpg = base64.b64encode(b"\xff\xd8\xff\xe0" + b"0" * 4000).decode()
    r = S.save_photo(sid, uid, "2026-02-11", jpg, 100, 50)
    truth(r.get("id"), "应返回照片 id")
    truth(r.get("bytes", 0) > 1024, "字节数应记下来")
    d = S.api_photos({"shop_id": [sid], "day": ["2026-02-11"], "unit_id": [str(uid)]})
    eq(len(d["photos"]), 1, "应能查到 1 张")


def t_welcome_done():
    sid = mk_shop("接口店-引导完成")
    r = S.api_welcome_done({"shop_id": sid})
    truth(r is not None, "打卡接口要有返回")


def t_baodian_reset():
    r = S.reset_baodian()
    truth(r is None or isinstance(r, dict), "重置宝点会话不应炸")


def t_overview_day_bounds():
    """区间核对超过 62 天要拒绝。"""
    sid = mk_shop("接口店-区间上限")
    try:
        S.api_recon_range({"shop_id": [sid], "kind": ["pachinko"],
                           "from": ["2026-01-01"], "to": ["2026-06-01"]})
        eq(True, False, "超过 62 天应被拒")
    except RuntimeError:
        pass


def t_recon_frozen_readonly():
    """冻结后，区间导出也拿冻结值。"""
    sid = mk_shop("接口店-冻结区间")
    uid = mk_unit(sid, "未弹珠机-001", comm="MG001")
    S.save_readings(sid, "2026-02-12", [{"unit_id": uid, "in_r": 100, "p_tokens": 900}])
    S.save_readings(sid, "2026-02-13", [{"unit_id": uid, "in_r": 300, "p_tokens": 1200}])
    S.recon_freeze(sid, "2026-02-13")
    S.save_readings(sid, "2026-02-12", [{"unit_id": uid, "in_r": 50, "p_tokens": 900}])
    d = S.api_recon_range({"shop_id": [sid], "kind": ["pachinko"],
                          "from": ["2026-02-13"], "to": ["2026-02-13"]})
    row = [r for r in d["rows"] if r["day"] == "2026-02-13"]
    truth(row, "应有这一天的行")
    eq(row[0]["meter_in"], 200, "冻结后导出仍是冻结时的数")


CASES = [
    ("门店新建与重名去重", t_save_shop),
    ("门店珠值/卡值/一币几珠", t_save_shop_values),
    ("删门店连带机台读数", t_remove_shop),
    ("新建机台写卡头归属", t_save_unit_new),
    ("机台没卡头要拒绝", t_save_unit_needs_comm),
    ("换卡头次日起归新机", t_save_unit_move_comm),
    ("改机台名不动编号", t_save_unit_rename_keeps_code),
    ("拆分机台", t_split_unit),
    ("合并机台带读数", t_merge_units),
    ("删机台连带读数", t_remove_unit),
    ("改机台接口", t_save_machine),
    ("核对原因与备注", t_recon_note),
    ("改读数留痕可查", t_audit),
    ("机台清单确认", t_confirm_units),
    ("分类统计", t_api_kinds),
    ("按编号扫码查机台", t_api_unit_by_code),
    ("全链概览", t_api_overview),
    ("门店不能看全链", t_store_cannot_overview),
    ("引导数据结构", t_bootstrap_shape),
    ("切店写 meta", t_switch_shop),
    ("初始化进度结构", t_setup_shape),
    ("照片列表为空", t_photos_empty),
    ("存照片能查到", t_photo_save),
    ("引导确认接口", t_welcome_done),
    ("重置宝点会话", t_baodian_reset),
    ("区间超过 62 天被拒", t_overview_day_bounds),
    ("冻结后区间导出不变", t_recon_frozen_readonly),
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
    os.environ.setdefault("METER_QUIET", "1")
    sys.exit(main())
