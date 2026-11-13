# -*- coding: utf-8 -*-
"""码表核对 · 本地服务

只做三件事：
  1. 收码表读数（人工填）
  2. 从宝点拉上币/下币（两个接口）
  3. 按机器算收币误差

依赖：Python 标准库 + requests（宝点签名用 hashlib，不需要额外包）
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
import uuid
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)
DB = DATA / "meter.db"

# 宝点会话模块：本机和服务器各一份，按存在性选
_SHARED_CANDIDATES = [
    os.environ.get("BD_SHARED") or "",
    r"C:\Users\Tuilge\Desktop\自动化合集\_shared",
    "/opt/日报自动化_服务器部署包/_shared",
]
SHARED = next((x for x in _SHARED_CANDIDATES if x and os.path.isdir(x)), _SHARED_CANDIDATES[1])

# 机台类型映射：优先用同目录那份，部署时一起上传
TYPE_MAP = str(BASE / "machine_type_map.json")
if not os.path.exists(TYPE_MAP):
    TYPE_MAP = r"C:\Users\Tuilge\WorkBuddy\2026-11-09-09-07-51\machine_type_map.json"

EXCEL = r"C:\Users\Tuilge\Desktop\资产核对表-鄂州店.xlsx"

API = "https://api.degrade.xhyun.vip"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

DEFAULT_SHOP = "纯玩世界鄂州吾悦店"
HIDDEN_GROUPS = {"宝点集团"}      # 内部测试店，不进界面

# 任务管理平台（cwsj）：账号体系共用它的，不另建一套
CWSJ_BASE = os.environ.get("CWSJ_BASE", "http://47.114.33.192/cwsj")
ACTOR = threading.local()          # 当前请求的身份
_ME_CACHE: dict[str, tuple] = {}   # token -> (expire_ts, me)


class AuthError(Exception):
    pass


def cwsj_login(account: str, pwd: str) -> dict:
    """用任务平台的账号登录，换回它的 JWT。"""
    import requests as _rq
    try:
        r = _rq.post(CWSJ_BASE + "/api/login",
                     json={"account": account, "pwd": pwd}, timeout=20)
    except Exception as e:
        raise AuthError("连不上任务平台：%s" % e)
    if r.status_code != 200:
        raise AuthError("账号或密码不对")
    data = r.json() or {}
    tok = data.get("token")
    if not tok:
        raise AuthError("任务平台没返回 token")
    return {"token": tok}


def cwsj_me(token: str) -> dict:
    """拿 token 去任务平台换身份，60 秒缓存。"""
    import requests as _rq
    now_ts = time.time()
    hit = _ME_CACHE.get(token)
    if hit and hit[0] > now_ts:
        return hit[1]
    try:
        r = _rq.get(CWSJ_BASE + "/api/bootstrap",
                    headers={"Authorization": "Bearer " + token}, timeout=20)
    except Exception as e:
        raise AuthError("连不上任务平台：%s" % e)
    if r.status_code != 200:
        raise AuthError("登录已过期，请重新登录")
    me = (r.json() or {}).get("me") or {}
    if not me:
        raise AuthError("任务平台没返回身份")
    _ME_CACHE[token] = (now_ts + 60, me)
    return me


def ensure_codes():
    """给门店和每块码表补稳定编号（活码用），并补位置号 P1..Pn。"""
    with conn() as c:
        scols = [r["name"] for r in c.execute("PRAGMA table_info(shops)")]
        if "code" not in scols:
            c.execute("ALTER TABLE shops ADD COLUMN code TEXT")
        ucols = [r["name"] for r in c.execute("PRAGMA table_info(units)")]
        if "code" not in ucols:
            c.execute("ALTER TABLE units ADD COLUMN code TEXT")
        if "pos" not in ucols:
            c.execute("ALTER TABLE units ADD COLUMN pos INTEGER")

        # 门店编号 S01..S16
        todo = [dict(r) for r in c.execute(
            "SELECT id FROM shops WHERE code IS NULL OR code='' ORDER BY name")]
        used = {r["code"] for r in c.execute("SELECT code FROM shops WHERE code<>''") if r["code"]}
        k = 1
        for r in todo:
            while ("S%02d" % k) in used:
                k += 1
            c.execute("UPDATE shops SET code=? WHERE id=?", ("S%02d" % k, r["id"]))
            used.add("S%02d" % k)

        # 码表编号 {门店编号}-{序号}：从这家店已有的最大序号往后接，
        # 否则新增/拆分出来的机台会拿到已经被占用的编号（曾撞过一次）
        shops = {r["id"]: r["code"] for r in c.execute("SELECT id, code FROM shops")}
        seq = {}
        for r in c.execute("SELECT shop_id, code FROM units WHERE code IS NOT NULL AND code<>''"):
            m = re.match(r".*-(\d+)$", r["code"] or "")
            if m:
                seq[r["shop_id"]] = max(seq.get(r["shop_id"], 0), int(m.group(1)))
        rows = [dict(r) for r in c.execute(
            "SELECT id, shop_id FROM units WHERE code IS NULL OR code='' ORDER BY shop_id, id")]
        for r in rows:
            k2 = seq.get(r["shop_id"], 0) + 1
            seq[r["shop_id"]] = k2
            c.execute("UPDATE units SET code=? WHERE id=?",
                      ("%s-%03d" % (shops.get(r["shop_id"]) or "X", k2), r["id"]))

        # 位置号：同一台机下按顺序 P1..Pn
        rows = [dict(r) for r in c.execute(
            "SELECT id, shop_id, name FROM units WHERE pos IS NULL ORDER BY shop_id, name, id")]
        groups = {}
        for r in rows:
            base = re.sub(r"\s+\d{5,}$", "", r["name"]).strip() or r["name"]
            groups.setdefault((r["shop_id"], base), []).append(r["id"])
        for _key, ids in groups.items():
            for i, uid in enumerate(ids, 1):
                c.execute("UPDATE units SET pos=? WHERE id=?", (i, uid))


def machine_of(uid: int) -> str:
    """码表 → 它所属机台的基础名"""
    with conn() as c:
        r = c.execute("SELECT name FROM units WHERE id=?", (uid,)).fetchone()
    if not r:
        return ""
    return re.sub(r"\s+\d{5,}$", "", r["name"]).strip() or r["name"]


def norm_store(s: str) -> str:
    """门店名归一化：去集团前缀、空格、连字符、店字尾。"""
    t = str(s or "").strip()
    for pre in ("乐掌门", "慢车道", "纯玩世界", "夹机同学", "宝点集团"):
        t = t.replace(pre, "")
    t = re.sub(r"[\s\-—·()（）]", "", t)
    if t.endswith("店") and len(t) > 2:
        t = t[:-1]
    return t


def resolve_shop(me: dict) -> str | None:
    """把任务平台的门店身份映射到我们的 shops 行。

    先查手工绑定，再按名字精确匹配，最后按包含匹配（唯一才算）。
    """
    key = str(me.get("id") or me.get("name") or "")
    with conn() as c:
        row = c.execute("SELECT shop_id FROM shop_map WHERE cwsj_key=?", (key,)).fetchone()
        if row:
            return row["shop_id"]

        marks = ",".join("?" * len(HIDDEN_GROUPS))
        shops = [dict(r) for r in c.execute(
            "SELECT id,name FROM shops WHERE COALESCE(group_name,'') NOT IN (%s)" % marks,
            tuple(HIDDEN_GROUPS))]
        name = (me.get("name") or "").strip()
        n = norm_store(name)
        if not n:
            return None

        cands = [s for s in shops if norm_store(s["name"]) == n]
        if not cands:
            # 包含匹配：「鄂州」↔「鄂州吾悦」
            cands = [s for s in shops
                     if n in norm_store(s["name"]) or norm_store(s["name"]) in n]

        if len(cands) == 1:
            c.execute("INSERT OR REPLACE INTO shop_map(cwsj_key,shop_id) VALUES(?,?)",
                      (key, cands[0]["id"]))
            print("[bind] %r -> %s" % (name, cands[0]["name"]), flush=True)
            return cands[0]["id"]

        print("[bind] 未匹配 cwsj=%r key=%r 候选=%s | 本地门店=%s"
              % (name, key, [x["name"] for x in cands] or "无",
                 " / ".join(s["name"] for s in shops)), flush=True)
    return None


def actor() -> dict | None:
    return getattr(ACTOR, "me", None)


def is_store() -> bool:
    a = actor()
    return bool(a and a.get("type") == "store")


def guard_shop(shop_id: str) -> str:
    """门店身份只能碰自己的店；总部放行。"""
    a = actor()
    if not a or a.get("type") != "store":
        return shop_id
    own = resolve_shop(a)
    if not own:
        raise AuthError("门店未绑定：任务平台里是「%s」" % (a.get("name") or "?") + "，请联系总部")
    return own


def require_hq():
    a = actor()
    if a and a.get("type") == "store":
        raise AuthError("只有总部能操作")
SCOPE_CLASS = "推币机"          # 参与码表核对的机台大类
# 对外地址：二维码要编这个，而不是反代回源时的 IP
PUBLIC_BASE = (os.environ.get("METER_PUBLIC_BASE") or "").rstrip("/")

SHARED_TEAM = "屏幕机"          # 该子类型是四联机共用一个码表
RATE = 10.0                     # 彩票 → 币 换算率


# ---------------------------------------------------------------- 类型映射
def load_type_map() -> dict:
    try:
        with open(TYPE_MAP, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"map": {}, "class_order": []}


TYPES = load_type_map()


KINDS = [("pusher", "推币机"), ("pachinko", "弹珠机")]


def kind_of(team_name: str) -> str | None:
    """码表机台分两类：名字带「弹珠」的算弹珠机，其余推币机大类算推币机。"""
    t = team_name or ""
    if "弹珠" in t:
        return "pachinko"
    if classify(t) == SCOPE_CLASS:
        return "pusher"
    return None


def kind_name(k: str) -> str:
    return dict(KINDS).get(k, k)


def classify(team_name: str) -> str:
    mp = TYPES.get("map") or {}
    if team_name in mp:
        return mp[team_name]
    for k, v in mp.items():
        if k and k in (team_name or ""):
            return v
    return "未分类"


# ---------------------------------------------------------------- 宝点
class Baodian:
    def __init__(self):
        if SHARED not in sys.path:
            sys.path.insert(0, SHARED)
        import baodian_session as S            # noqa: E402
        self.S = S
        self.d = S.as_login_dict()

    @property
    def token(self) -> str:
        return self.d["token"]

    def shops(self) -> list:
        return self.d.get("shops") or []

    def _headers(self, parts: list) -> dict:
        ts = str(int(time.time() * 1000))
        nc = str(uuid.uuid4())
        sign = hashlib.md5("&".join(sorted(parts) + [nc, ts, self.token]).encode()).hexdigest()
        return {
            "Authorization": "Bearer " + self.token,
            "Content-Type": "application/json;charset=UTF-8",
            "XH-NONCE": nc, "XH-TIMESTAMP": ts, "XH-SIGN": sign,
            "XH-TERMINAL": "backend",
            "Origin": "https://md.xhyun.vip", "Referer": "https://md.xhyun.vip/",
            "User-Agent": UA,
        }

    def post(self, path: str, body: dict, shop: dict) -> dict:
        import requests
        bs = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        h = self._headers(["jsonbody=" + bs])
        h["XH-CUSTOMERID"] = shop["customerId"]
        h["XH-SHOPID"] = shop["id"]
        h["XH-TYPE"] = "11"
        s = requests.Session()
        s.trust_env = False
        r = s.post(API + path, data=bs.encode(), headers=h,
                   timeout=(8, 90), proxies={"http": None, "https": None})
        return r.json()

    # --- 两个口径 ---
    def fetch_up(self, shop: dict, day: str) -> list:
        """机台消费币汇总 → 上币"""
        b = self.post("/reports/report/shop/coinoutplay/list", {
            "paging": {"page": 0, "limit": 1000, "sort": "asc", "sortField": "CreateTime",
                       "filters": [{"field": "CreateTime", "value": f"{day} ~ {day}",
                                    "operator": 0, "relation": 0, "fieldType": 1}]},
            "memberLevelIds": []}, shop)
        return b.get("listData") or []

    def fetch_down(self, shop: dict, day: str) -> list:
        """机台获分记录-按设备汇总 → 下币"""
        b = self.post("/terminals/backend/sport/get/sport_integral_summary/list", {
            "page": 0, "limit": 1000, "sortField": "CreateTime", "sort": "desc",
            "filters": [{"field": "CreateTime", "value": f"{day} ~ {day}",
                         "operator": 0, "relation": 0, "fieldType": 1}]}, shop)
        return b.get("listData") or []


_BD = None
_BD_ERR = None


def baodian() -> Baodian:
    global _BD, _BD_ERR
    if _BD is None:
        try:
            _BD = Baodian()
            _BD_ERR = None
        except Exception as e:                      # 登录态失效
            _BD_ERR = str(e)[:300]
            raise RuntimeError(_BD_ERR)
    return _BD


def reset_baodian():
    global _BD
    _BD = None


# ---------------------------------------------------------------- 数据库
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    return c


SCHEMA = """
CREATE TABLE IF NOT EXISTS shops(
  id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
  customer_id TEXT, shop_id TEXT, created_at TEXT,
  units_checked TEXT, units_confirmed TEXT, group_name TEXT);
CREATE TABLE IF NOT EXISTS units(
  id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id TEXT NOT NULL, name TEXT NOT NULL,
  team TEXT, shared INTEGER DEFAULT 0, active INTEGER DEFAULT 1, sort INTEGER DEFAULT 0,
  locked INTEGER DEFAULT 0, kind TEXT DEFAULT 'pusher', UNIQUE(shop_id, name));
CREATE TABLE IF NOT EXISTS comms(
  comm_id TEXT PRIMARY KEY, unit_id INTEGER NOT NULL, console_no TEXT, team TEXT,
  device TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS readings(
  shop_id TEXT, unit_id INTEGER, day TEXT, in_r REAL, out_r REAL,
  note TEXT, updated_at TEXT, PRIMARY KEY(shop_id, unit_id, day));
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, shop_id TEXT, day TEXT,
  unit_id INTEGER, field TEXT, old_v REAL, new_v REAL, note TEXT);
CREATE TABLE IF NOT EXISTS card_daily(
  shop_id TEXT, day TEXT, comm_id TEXT, up_native REAL, up_give REAL,
  up_a REAL, up_b REAL, down_qty REAL, fetched_at TEXT,
  PRIMARY KEY(shop_id, day, comm_id));
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def init_db():
    with conn() as c:
        c.executescript(SCHEMA)
        # 老库补列
        cols = [r["name"] for r in c.execute("PRAGMA table_info(shops)")]
        if "units_checked" not in cols:
            c.execute("ALTER TABLE shops ADD COLUMN units_checked TEXT")
        if "units_confirmed" not in cols:
            c.execute("ALTER TABLE shops ADD COLUMN units_confirmed TEXT")
        if "group_name" not in cols:
            c.execute("ALTER TABLE shops ADD COLUMN group_name TEXT")
        c.execute("CREATE TABLE IF NOT EXISTS shop_map("
                  "cwsj_key TEXT PRIMARY KEY, shop_id TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS recon_notes("
                  "shop_id TEXT, day TEXT, unit_id INTEGER, reason TEXT, note TEXT, "
                  "updated_at TEXT, updated_by TEXT, PRIMARY KEY(shop_id,day,unit_id))")
        ensure_codes()
        ucols = [r["name"] for r in c.execute("PRAGMA table_info(units)")]
        if "locked" not in ucols:
            c.execute("ALTER TABLE units ADD COLUMN locked INTEGER DEFAULT 0")
        if "kind" not in ucols:
            c.execute("ALTER TABLE units ADD COLUMN kind TEXT DEFAULT 'pusher'")
        c.execute("UPDATE units SET kind='pachinko' WHERE team LIKE '%弹珠%' AND "
                  "(kind IS NULL OR kind='pusher')")
        rcols = [r["name"] for r in c.execute("PRAGMA table_info(readings)")]
        for col in ("p_tokens", "p_out", "p_cards"):
            if col not in rcols:
                c.execute("ALTER TABLE readings ADD COLUMN %s REAL" % col)
        scols = [r["name"] for r in c.execute("PRAGMA table_info(shops)")]
        for col in ("bead_value", "card_value"):
            if col not in scols:
                c.execute("ALTER TABLE shops ADD COLUMN %s REAL" % col)


def meta_get(k, default=None):
    with conn() as c:
        r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def meta_set(k, v):
    with conn() as c:
        c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                  (k, str(v)))


# ---------------------------------------------------------------- 门店 / 机台
def shop_row(shop_id: str):
    with conn() as c:
        r = c.execute("SELECT * FROM shops WHERE id=?", (shop_id,)).fetchone()
    return dict(r) if r else None


def shop_row_by_name(name: str):
    with conn() as c:
        r = c.execute("SELECT * FROM shops WHERE name=?", (name,)).fetchone()
    return dict(r) if r else None


def shop_of(d: dict):
    """库里存的门店 → 宝点会话里的门店对象"""
    bd = baodian()
    for s in bd.shops():
        if s["shopName"] == d["name"]:
            return s
    raise RuntimeError(f"宝点账号里找不到门店 {d['name']}")


def ensure_shop(name: str) -> str:
    bd = baodian()
    s = next((x for x in bd.shops() if x["shopName"] == name), None)
    if not s:
        raise RuntimeError(f"宝点账号里没有门店 {name}")
    sid = s["id"]
    with conn() as c:
        c.execute("INSERT INTO shops(id,name,customer_id,shop_id,created_at,group_name) "
                  "VALUES(?,?,?,?,?,?) "
                  "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
                  "customer_id=excluded.customer_id, shop_id=excluded.shop_id, "
                  "group_name=excluded.group_name",
                  (sid, name, s["customerId"], s["id"], now(), s.get("customerName")))
    return sid


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def sync_units(shop_id: str, devices: list) -> int:
    """按宝点设备清单推导码表单元。

    默认一个卡头（通讯ID）一个码表：每个卡头的读数在宝点是分开记的。
    子类型是「屏幕机」的，四联机共用一块码表，合成一个单元装 4 个通讯ID。
    其余共用码表的机器，由店长在设置里手工合并。
    """
    with conn() as c:
        locked = {r["comm_id"] for r in c.execute(
            "SELECT m.comm_id FROM comms m JOIN units u ON u.id = m.unit_id "
            "WHERE u.locked = 1")}
    scope = [d for d in devices
             if kind_of(d.get("projectTeamName"))
             and str(d.get("commId")) not in locked]
    groups: dict[str, list] = {}
    for d in scope:
        groups.setdefault(d.get("deviceName") or "未命名", []).append(d)

    made = 0
    with conn() as c:
        for name, ds in groups.items():
            team = ds[0].get("projectTeamName")
            kind = kind_of(team) or "pusher"
            if team == SHARED_TEAM:
                c.execute("INSERT INTO units(shop_id,name,team,shared,kind) VALUES(?,?,?,1,?) "
                          "ON CONFLICT(shop_id,name) DO UPDATE SET team=excluded.team, "
                          "shared=1, active=1, kind=excluded.kind", (shop_id, name, team, kind))
                u = c.execute("SELECT id FROM units WHERE shop_id=? AND name=?",
                              (shop_id, name)).fetchone()["id"]
                for d in ds:
                    put_comm(c, d, u)
                made += 1
            else:
                for d in ds:
                    label = "%s %s" % (name, d.get("commId"))
                    c.execute("INSERT INTO units(shop_id,name,team,shared,kind) VALUES(?,?,?,0,?) "
                              "ON CONFLICT(shop_id,name) DO UPDATE SET team=excluded.team, "
                              "shared=0, active=1, kind=excluded.kind", (shop_id, label, team, kind))
                    u = c.execute("SELECT id FROM units WHERE shop_id=? AND name=?",
                                  (shop_id, label)).fetchone()["id"]
                    put_comm(c, d, u)
                    made += 1
    return made


def sync_units_best(shop_id: str) -> dict:
    """取最近 3 天里「推币机」设备最多的那天来推导机台。

    不用当天：设备当天可能没全上报，会少几台。
    """
    bd = baodian()
    shop = shop_of(shop_row(shop_id))
    best, best_n, best_day = None, -1, None
    for i in range(1, 4):
        day = (date.today() - timedelta(days=i)).isoformat()
        try:
            devs = bd.fetch_up(shop, day)
        except Exception:
            continue
        n = len([d for d in devs if kind_of(d.get("projectTeamName"))])
        if n > best_n:
            best, best_n, best_day = devs, n, day
        time.sleep(0.5)
    if best:
        sync_units(shop_id, best)
    with conn() as c:
        c.execute("UPDATE shops SET units_checked=? WHERE id=?", (now(), shop_id))
    return {"devices": best_n, "day": best_day}


def put_comm(c, d, unit_id):
    c.execute("INSERT INTO comms(comm_id,unit_id,console_no,team,device,updated_at) "
              "VALUES(?,?,?,?,?,?) ON CONFLICT(comm_id) DO UPDATE SET "
              "unit_id=excluded.unit_id, console_no=excluded.console_no, "
              "team=excluded.team, device=excluded.device, updated_at=excluded.updated_at",
              (str(d.get("commId")), unit_id, d.get("consoleNo"),
               d.get("projectTeamName"), d.get("deviceName"), now()))


def units_of(shop_id: str, active_only: bool = True, kind: str | None = None) -> list:
    where = "AND active=1" if active_only else ""
    args = [shop_id]
    if kind:
        where += " AND COALESCE(kind,'pusher')=?"
        args.append(kind)
    with conn() as c:
        us = [dict(r) for r in c.execute(
            f"SELECT * FROM units WHERE shop_id=? {where} ORDER BY active DESC, sort, id",
            tuple(args))]
        for u in us:
            u["comms"] = [dict(r) for r in c.execute(
                "SELECT * FROM comms WHERE unit_id=? ORDER BY console_no", (u["id"],))]
    return us


# ---------------------------------------------------------------- 取数
def fetch_day(shop_id: str, day: str) -> dict:
    """拉一天的上币 + 下币，落 card_daily。"""
    d = shop_row(shop_id)
    shop = shop_of(d)
    bd = baodian()
    up = bd.fetch_up(shop, day)
    time.sleep(0.6)
    down = bd.fetch_down(shop, day)

    U = {}
    for r in up:
        cid = str(r.get("commId") or "")
        if not cid:
            continue
        U[cid] = (r.get("actualNativeCoinNum") or 0, r.get("actualGiveCoinNum") or 0,
                  r.get("giveCoinA") or 0, r.get("giveCoinB") or 0)
    D = {}
    for r in down:
        cid = str(r.get("commId") or "")
        if not cid:
            continue
        D[cid] = D.get(cid, 0) + (r.get("integralQuantity") or 0)

    ts = now()
    with conn() as c:
        for cid in set(U) | set(D):
            u = U.get(cid, (0, 0, 0, 0))
            c.execute("INSERT INTO card_daily(shop_id,day,comm_id,up_native,up_give,up_a,up_b,"
                      "down_qty,fetched_at) VALUES(?,?,?,?,?,?,?,?,?) "
                      "ON CONFLICT(shop_id,day,comm_id) DO UPDATE SET "
                      "up_native=excluded.up_native, up_give=excluded.up_give, "
                      "up_a=excluded.up_a, up_b=excluded.up_b, "
                      "down_qty=excluded.down_qty, fetched_at=excluded.fetched_at",
                      (shop_id, day, cid, u[0], u[1], u[2], u[3], D.get(cid, 0), ts))
        # 顺带补 consoleNo / 设备名（下币接口有 consoleNo）
        for r in down:
            cid = str(r.get("commId") or "")
            if not cid:
                continue
            row = c.execute("SELECT unit_id FROM comms WHERE comm_id=?", (cid,)).fetchone()
            if row:
                c.execute("UPDATE comms SET console_no=COALESCE(?,console_no), updated_at=? "
                          "WHERE comm_id=?", (r.get("consoleNo"), ts, cid))
    return {"up_rows": len(U), "down_rows": len(D)}


# 抄表在次日早上做：码表读数是当天日终值，第二天来填。
# 所以门店只能填「昨天」，过了就锁死；总部不限。
EDIT_REASONS = ["换码表或归零重计", "机器故障没走系统", "未下完分断电",
                "人为抄错已改", "其他"]


def edit_day() -> str:
    """门店今天该填的日期。"""
    return (date.today() - timedelta(days=1)).isoformat()


def can_edit(day: str) -> bool:
    if not is_store():
        return True
    return day == edit_day()


def recon_note(shop_id: str, day: str, unit_id: int, reason: str, note: str) -> dict:
    with conn() as c:
        c.execute("INSERT INTO recon_notes(shop_id,day,unit_id,reason,note,updated_at,updated_by) "
                  "VALUES(?,?,?,?,?,?,?) ON CONFLICT(shop_id,day,unit_id) DO UPDATE SET "
                  "reason=excluded.reason, note=excluded.note, "
                  "updated_at=excluded.updated_at, updated_by=excluded.updated_by",
                  (shop_id, day, unit_id, reason, note, now(),
                   (actor() or {}).get("name") or "?"))
    return {"ok": True}


def prev_reading(shop_id: str, unit_id: int, day: str):
    with conn() as c:
        r = c.execute("SELECT day,in_r,out_r,p_tokens,p_out,p_cards FROM readings "
                      "WHERE shop_id=? AND unit_id=? AND day<? AND in_r IS NOT NULL "
                      "ORDER BY day DESC LIMIT 1",
                      (shop_id, unit_id, day)).fetchone()
    return dict(r) if r else None


def recon(shop_id: str, day: str, kind: str | None = None) -> dict:
    units = units_of(shop_id, kind=kind)
    with conn() as c:
        cards = {}
        for r in c.execute("SELECT * FROM card_daily WHERE shop_id=? AND day=?", (shop_id, day)):
            cards[r["comm_id"]] = dict(r)
        reads = {}
        for r in c.execute("SELECT * FROM readings WHERE shop_id=? AND day=?", (shop_id, day)):
            reads[r["unit_id"]] = dict(r)
        notes = {}
        for r in c.execute("SELECT * FROM recon_notes WHERE shop_id=? AND day=?", (shop_id, day)):
            notes[r["unit_id"]] = dict(r)

    rows = []
    for u in units:
        comms = [str(x["comm_id"]) for x in u["comms"]]
        got = [cards[c] for c in comms if c in cards]
        up = sum((g["up_native"] or 0) + (g["up_give"] or 0) for g in got) if got else None
        down = sum((g["down_qty"] or 0) for g in got) / RATE if got else None
        rd = reads.get(u["id"])
        pv = prev_reading(shop_id, u["id"], day) if rd else None

        is_p = (u.get("kind") == "pachinko")
        tin = tout = None
        if rd and pv:
            tin = (rd["in_r"] or 0) - (pv["in_r"] or 0)
            tout = (rd["out_r"] or 0) - (pv["out_r"] or 0)

        p_metrics = None
        if is_p:
            d_tokens = d_out = d_cards = None
            if rd and pv:
                d_tokens = (rd["p_tokens"] or 0) - (pv["p_tokens"] or 0)
                d_out = (rd["p_out"] or 0) - (pv["p_out"] or 0)
                d_cards = (rd["p_cards"] or 0) - (pv["p_cards"] or 0)
            win = (d_out - tin) if (d_out is not None and tin is not None) else None
            keep = (d_tokens - win) if (d_tokens is not None and win is not None) else None
            rate_p = (win / d_tokens) if (win is not None and d_tokens) else None
            row_shop = shop_row(shop_id) or {}
            bead = row_shop.get("bead_value")
            cardv = row_shop.get("card_value")
            profit = None
            if keep is not None and d_cards is not None and bead and cardv:
                profit = keep * bead - d_cards * cardv
            p_metrics = {
                "tokens": d_tokens, "out": d_out, "cards": d_cards,
                "win": win, "keep": keep, "rate": rate_p, "profit": profit,
            }

        head = None
        if rd and got:
            head = (up or 0) - (0 if is_p else (down or 0))
        meter = tin if is_p else ((tin - tout) if (tin is not None and tout is not None) else None)

        if not rd:
            status = "今天还没填"
        elif not got:
            status = "宝点今天没有数据"
        elif pv is None:
            status = "第一次填"
        elif (rd["in_r"] or 0) < (pv["in_r"] or 0) or (rd["out_r"] or 0) < (pv["out_r"] or 0):
            status = "读数比昨天小"
        else:
            status = "正常"

        diff = (head - meter) if (head is not None and meter is not None) else None
        rate = (diff / meter) if (diff is not None and meter) else None

        rows.append({
            "kind": u.get("kind") or "pusher",
            "p": p_metrics,
            "unit_id": u["id"], "name": u["name"], "team": u["team"],
            "shared": bool(u["shared"]), "comm_count": len(comms),
            "up": up, "down": down, "head": head, "meter_in": tin, "meter_out": tout,
            "meter": meter, "diff": diff, "rate": rate, "status": status,
            "in_r": rd["in_r"] if rd else None, "out_r": rd["out_r"] if rd else None,
            "prev_in": pv["in_r"] if pv else None, "prev_out": pv["out_r"] if pv else None,
            "note": rd["note"] if rd else None,
            "reason": (notes.get(u["id"]) or {}).get("reason"),
            "reason_note": (notes.get(u["id"]) or {}).get("note"),
        })

    ok = [r for r in rows if r["status"] == "正常"]
    return {
        "day": day,
        "rows": rows,
        "summary": {
            "total": len(rows),
            "ok": len(ok),
            "diff_all": round(sum(r["diff"] for r in rows if r["diff"] is not None), 1),
            "diff_ok": round(sum(r["diff"] for r in ok if r["diff"] is not None), 1),
            "warn": len([r for r in rows if r["status"] in ("读数比昨天小", "第一次填", "今天还没填", "宝点今天没有数据")]),
            "big": len([r for r in rows if r["diff"] is not None and abs(r["diff"]) >= 1000]),
        },
    }


def save_readings(shop_id: str, day: str, items: list) -> dict:
    """items: [{unit_id, in_r, out_r, note}] → 保存 + 留痕 + 返回读数下降告警"""
    if not can_edit(day):
        raise RuntimeError("门店只能填 %s 的码表，%s 已经锁了" % (edit_day(), day))
    warns = []
    ts = now()
    with conn() as c:
        for it in items:
            uid = int(it["unit_id"])
            new_in = it.get("in_r")
            new_out = it.get("out_r")
            old = c.execute("SELECT in_r,out_r FROM readings WHERE shop_id=? AND unit_id=? AND day=?",
                            (shop_id, uid, day)).fetchone()
            if old:
                if old["in_r"] != new_in:
                    c.execute("INSERT INTO audit(ts,shop_id,day,unit_id,field,old_v,new_v,note) "
                              "VALUES(?,?,?,?,?,?,?,?)",
                              (ts, shop_id, day, uid, "in_r", old["in_r"], new_in,
                               it.get("reason") or "编辑"))
                if old["out_r"] != new_out:
                    c.execute("INSERT INTO audit(ts,shop_id,day,unit_id,field,old_v,new_v,note) "
                              "VALUES(?,?,?,?,?,?,?,?)",
                              (ts, shop_id, day, uid, "out_r", old["out_r"], new_out,
                               it.get("reason") or "编辑"))
            c.execute("INSERT INTO readings(shop_id,unit_id,day,in_r,out_r,note,updated_at,"
                      "p_tokens,p_out,p_cards) VALUES(?,?,?,?,?,?,?,?,?,?) "
                      "ON CONFLICT(shop_id,unit_id,day) DO UPDATE SET "
                      "in_r=excluded.in_r, out_r=excluded.out_r, note=excluded.note, "
                      "updated_at=excluded.updated_at, p_tokens=excluded.p_tokens, "
                      "p_out=excluded.p_out, p_cards=excluded.p_cards",
                      (shop_id, uid, day, new_in, new_out, it.get("note"), ts,
                       it.get("p_tokens"), it.get("p_out"), it.get("p_cards")))

            pv = prev_reading(shop_id, uid, day)
            if pv and new_in is not None and new_in < (pv["in_r"] or 0):
                warns.append({"unit_id": uid, "field": "in_r", "prev": pv["in_r"], "now": new_in})
            if pv and new_out is not None and new_out < (pv["out_r"] or 0):
                warns.append({"unit_id": uid, "field": "out_r", "prev": pv["out_r"], "now": new_out})
    return {"saved": len(items), "warns": warns}


def entry_view(shop_id: str, day: str, kind: str | None = None) -> dict:
    shop = shop_row(shop_id) or {}
    units = units_of(shop_id, active_only=False, kind=kind)
    with conn() as c:
        reads = {r["unit_id"]: dict(r) for r in
                 c.execute("SELECT * FROM readings WHERE shop_id=? AND day=?", (shop_id, day))}
        have_card = {r["comm_id"] for r in
                     c.execute("SELECT comm_id FROM card_daily WHERE shop_id=? AND day=?",
                               (shop_id, day))}
    rows = []
    for u in units:
        rd = reads.get(u["id"])
        pv = prev_reading(shop_id, u["id"], day)
        comms = [str(x["comm_id"]) for x in u["comms"]]
        rows.append({
            "unit_id": u["id"], "name": u["name"], "team": u["team"], "shared": bool(u["shared"]),
            "active": bool(u["active"]),
            "kind": u.get("kind") or "pusher",
            "pos": u.get("pos"), "code": u.get("code"),
            "comms": comms, "has_card": any(c in have_card for c in comms),
            "prev_in": pv["in_r"] if pv else None,
            "prev_out": pv["out_r"] if pv else None,
            "prev_day": pv["day"] if pv else None,
            "in_r": rd["in_r"] if rd else None,
            "out_r": rd["out_r"] if rd else None,
            "p_tokens": rd["p_tokens"] if rd else None,
            "p_out": rd["p_out"] if rd else None,
            "p_cards": rd["p_cards"] if rd else None,
            "prev_tokens": pv["p_tokens"] if pv else None,
            "prev_out_total": pv["p_out"] if pv else None,
            "prev_cards": pv["p_cards"] if pv else None,
            "note": rd["note"] if rd else None,
        })
    return {"day": day, "rows": rows,
            "confirmed": bool(shop.get("units_confirmed")),
            "confirmed_at": shop.get("units_confirmed")}


def dates_with_data(shop_id: str) -> list:
    with conn() as c:
        rs = c.execute("SELECT day, COUNT(*) n FROM readings WHERE shop_id=? GROUP BY day "
                       "ORDER BY day DESC LIMIT 400", (shop_id,)).fetchall()
    return [{"day": r["day"], "n": r["n"]} for r in rs]


# ---------------------------------------------------------------- 后台任务
JOB = {"running": False, "kind": "", "done": 0, "total": 0, "msg": "", "err": "", "log": []}


def run_job(kind: str, items: list, fn, sleep=1.0):
    def _run():
        JOB.update(running=True, kind=kind, done=0, total=len(items), msg="", err="", log=[])
        for i, it in enumerate(items, 1):
            try:
                fn(it)
                JOB["log"].append("%s ok" % it)
            except Exception as e:
                JOB["err"] = "%s: %s" % (it, str(e)[:120])
                JOB["log"].append("%s fail" % it)
            JOB["done"] = i
            JOB["msg"] = str(it)
            time.sleep(sleep)
        JOB["running"] = False
        JOB["msg"] = "结束"
    threading.Thread(target=_run, daemon=True).start()


# ---------------------------------------------------------------- 历史回填
BACKFILL = {"running": False, "done": 0, "total": 0, "msg": "", "err": ""}


def seed_from_excel(shop_id: str) -> dict:
    """把 Excel 的码表读数（D/E）灌进 readings。

    用「卡头数据」里每天的 通讯ID↔P位 做映射：Excel 一行(机台,P位) → 通讯ID → 码表单元。
    """
    from openpyxl import load_workbook
    wb = load_workbook(EXCEL, data_only=True)
    s0, s1 = wb["推币机收币误差表"], wb["卡头数据"]

    def dt(v):
        return v.strftime("%Y-%m-%d") if hasattr(v, "strftime") else None

    # 每天 (机台, P位) -> 通讯ID
    per_day = {}
    for r in range(2, s1.max_row + 1):
        d = dt(s1.cell(row=r, column=1).value)
        cid = s1.cell(row=r, column=9).value
        mach = s1.cell(row=r, column=8).value
        p = s1.cell(row=r, column=10).value
        if not d or not cid or not mach or not p:
            continue
        per_day.setdefault(d, {})[(mach, p)] = str(cid)

    comm2unit = {}
    with conn() as c:
        for r in c.execute("SELECT comm_id, unit_id FROM comms"):
            comm2unit[r["comm_id"]] = r["unit_id"]
        unit_name = {r["id"]: r["name"] for r in c.execute(
            "SELECT id,name FROM units WHERE shop_id=?", (shop_id,))}

    n = 0
    ts = now()
    with conn() as c:
        for r in range(2, s0.max_row + 1):
            day = dt(s0.cell(row=r, column=1).value)
            if not day:
                continue
            machine = s0.cell(row=r, column=2).value
            p = s0.cell(row=r, column=3).value
            din = s0.cell(row=r, column=4).value
            dout = s0.cell(row=r, column=5).value
            if not isinstance(din, (int, float)) and not isinstance(dout, (int, float)):
                continue
            cid = (per_day.get(day) or {}).get((machine, p))
            if not cid:
                continue
            uid = comm2unit.get(cid)
            if uid is None:
                continue
            c.execute("INSERT INTO readings(shop_id,unit_id,day,in_r,out_r,updated_at) "
                      "VALUES(?,?,?,?,?,?) ON CONFLICT(shop_id,unit_id,day) DO UPDATE SET "
                      "in_r=excluded.in_r, out_r=excluded.out_r",
                      (shop_id, uid, day,
                       din if isinstance(din, (int, float)) else None,
                       dout if isinstance(dout, (int, float)) else None, ts))
            n += 1
    return {"readings": n, "units": len(unit_name)}


def backfill_cards(shop_id: str, days: list):
    def run():
        BACKFILL.update(running=True, done=0, total=len(days), msg="", err="")
        for i, day in enumerate(days, 1):
            try:
                fetch_day(shop_id, day)
                BACKFILL["msg"] = f"{day} 完成"
            except Exception as e:
                BACKFILL["err"] = f"{day}: {str(e)[:120]}"
            BACKFILL["done"] = i
            time.sleep(1.2)
        BACKFILL["running"] = False
        BACKFILL["msg"] = "回填结束"
    threading.Thread(target=run, daemon=True).start()


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "MeterCheck/1.0"

    def log_message(self, fmt, *args):
        pass

    # --- 工具 ---
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _actor(self):
        auth = self.headers.get("Authorization") or ""
        tok = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if not tok:
            return None
        try:
            return cwsj_me(tok)
        except AuthError:
            return None

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _static(self, path: str):
        rel = path.lstrip("/") or "index.html"
        f = (BASE / rel).resolve()
        if not str(f).startswith(str(BASE)) or not f.is_file():
            return self._send(404, {"error": "not found"})
        ctype = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "application/javascript; charset=utf-8",
                 ".svg": "image/svg+xml"}.get(f.suffix, "application/octet-stream")
        self._send(200, f.read_bytes(), ctype)

    # --- GET ---
    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        p = u.path
        ACTOR.me = self._actor()
        if p.startswith("/api/") and p != "/api/login" and actor() is None:
            return self._send(401, {"error": "未登录"})
        try:
            if p.endswith("/qr/img"):
                from urllib.parse import parse_qs as _pqs
                code = (q.get("c") or [""])[0]
                with conn() as c:
                    u = c.execute("SELECT * FROM units WHERE code=?", (code,)).fetchone()
                if not u:
                    return self._send(404, {"error": "no such code"})
                guard_shop(u["shop_id"])
                m = next((x for x in _machine_groups(u["shop_id"]) if x["code"] == code), None)
                if not m:
                    return self._send(404, {"error": "no such machine"})
                sub = " / ".join(
                    "P%s %s" % (v.get("pos") or i + 1, "/".join(v.get("comms") or []))
                    for i, v in enumerate(m["units"]))
                png = qr_label_png(code, "%s/?c=%s" % (_qr_origin(), code), m["name"], sub)
                body = png
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=600")
                self.end_headers()
                self.wfile.write(body)
                return None

            if p.endswith("/qr/zip"):
                shop_id = guard_shop((q.get("shop_id") or [""])[0])
                codes = [x for x in (q.get("codes") or [""])[0].split(",") if x]
                body = qr_zip(shop_id, codes)
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition",
                                 "attachment; filename=mabiao-qr.zip")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return None

            if p.endswith("/qr"):
                pre = (self.headers.get("X-Forwarded-Prefix") or "").rstrip("/")
                if PUBLIC_BASE:
                    origin = PUBLIC_BASE
                else:
                    proto = self.headers.get("X-Forwarded-Proto") or "https"
                    origin = proto + "://" + (self.headers.get("Host") or "127.0.0.1:8899") + pre
                html = qr_page((q.get("shop_id") or [""])[0], origin, pre)
                return self._send(200, html, "text/html; charset=utf-8")
            if p == "/api/unit":
                return self._send(200, api_unit(q))
            if p == "/api/kinds":
                return self._send(200, api_kinds(q))
            if p == "/api/me":
                return self._send(200, api_me())
            if p == "/api/bootstrap":
                return self._send(200, api_bootstrap())
            if p == "/api/entry":
                return self._send(200, api_entry(q))
            if p == "/api/recon/range":
                return self._send(200, api_recon_range(q))
            if p == "/api/recon":
                return self._send(200, api_recon(q))
            if p == "/api/units":
                return self._send(200, {"units": units_of(
                    (q.get("shop_id") or [meta_get("shop_id")])[0], active_only=False)})
            if p == "/api/backfill/status":
                return self._send(200, BACKFILL)
            if p == "/api/manage/shops":
                return self._send(200, {"rows": manage_shops()})
            if p == "/api/manage/machines":
                return self._send(200, {"rows": manage_machines(
                    (q.get("shop_id") or [""])[0])})
            if p == "/api/manage/units":
                return self._send(200, {"rows": manage_units(
                    (q.get("shop_id") or [""])[0])})
            if p == "/api/setup":
                return self._send(200, api_setup())
            if p == "/api/overview":
                return self._send(200, api_overview(
                    (q.get("day") or [date.today().isoformat()])[0]))
            if p == "/api/job":
                return self._send(200, JOB)
            if p == "/api/audit":
                return self._send(200, api_audit(q))
            if p.startswith("/api/"):
                return self._send(404, {"error": "no such api"})
            return self._static(p)
        except Exception as e:
            return self._send(500, {"error": str(e)[:400]})

    # --- POST ---
    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        b = self._body()
        ACTOR.me = self._actor()
        if p.startswith("/api/") and p != "/api/login" and actor() is None:
            return self._send(401, {"error": "未登录"})
        try:
            if p == "/api/recon/note":
                return self._send(200, api_recon_note(b))
            if p == "/api/entry":
                return self._send(200, save_readings(b["shop_id"], b["day"], b.get("items") or []))
            if p == "/api/fetch":
                sid, day = b["shop_id"], b["day"]
                devices = baodian().fetch_up(shop_of(shop_row(sid)), day)
                sync_units(sid, devices)
                r = fetch_day(sid, day)
                return self._send(200, {"ok": True, **r})
            if p == "/api/backfill":
                sid = b["shop_id"]
                days = b.get("days") or []
                seed = seed_from_excel(sid)
                if days:
                    backfill_cards(sid, days)
                return self._send(200, {"ok": True, "seed": seed, "total": len(days)})
            if p == "/api/shops":
                name = b.get("name")
                sid = ensure_shop(name)
                return self._send(200, {"ok": True, "shop_id": sid})
            if p == "/api/units":
                sid = b["shop_id"]
                with conn() as c:
                    c.execute("UPDATE units SET shared=?, active=? WHERE id=?",
                              (1 if b.get("shared") else 0, 0 if b.get("hide") else 1,
                               int(b["unit_id"])))
                return self._send(200, {"ok": True})
            if p == "/api/shop/welcome":
                return self._send(200, api_welcome_done(b))
            if p == "/api/login":
                r = cwsj_login((b.get("account") or "").strip(), b.get("pwd") or "")
                return self._send(200, r)
            if p == "/api/manage/shop/values":
                return self._send(200, save_shop_values(b))
            if p == "/api/manage/shop/save":
                return self._send(200, save_shop(b))
            if p == "/api/manage/shop/remove":
                return self._send(200, remove_shop(b["id"]))
            if p == "/api/manage/unit/save":
                r = save_unit(b)
                ensure_codes()
                return self._send(200, r)
            if p == "/api/manage/unit/split":
                r = split_unit(int(b["id"]))
                ensure_codes()
                return self._send(200, r)
            if p == "/api/manage/machine/save":
                return self._send(200, save_machine(b))
            if p == "/api/manage/unit/merge":
                r = merge_units(b["shop_id"], b.get("ids") or [], b.get("name") or "")
                ensure_codes()
                return self._send(200, r)
            if p == "/api/manage/unit/remove":
                r = remove_unit(int(b["id"]))
                ensure_codes()
                return self._send(200, r)
            if p == "/api/setup/shops":
                r = sync_all_shops()
                return self._send(200, {"ok": True, **r})
            if p == "/api/setup/units":
                todo = [r["name"] for r in conn().execute(
                    "SELECT name FROM shops WHERE units_checked IS NULL ORDER BY name")]
                run_job("units", todo,
                        lambda name: sync_units_best(shop_row_by_name(name)["id"]), sleep=0.8)
                return self._send(200, {"ok": True, "task": len(todo)})
            if p == "/api/units/confirm":
                return self._send(200, confirm_units(b["shop_id"], b.get("unit_ids") or []))
            if p == "/api/shops/sync":
                r = sync_all_shops()
                todo = [x for x in shops_with_units()]
                run_job("units", [x["name"] for x in todo],
                        lambda name: sync_units_best(shop_row_by_name(name)["id"]), sleep=0.8)
                return self._send(200, {"ok": True, **r, "task": len(todo)})
            if p == "/api/fetch-all":
                day = b["day"]
                names = [x["name"] for x in shops_with_units() if x["units"]]
                run_job("fetch", names,
                        lambda name: fetch_day(shop_row_by_name(name)["id"], day), sleep=1.2)
                return self._send(200, {"ok": True, "task": len(names), "day": day})
            if p == "/api/baodian/reset":
                reset_baodian()
                return self._send(200, {"ok": True})
            if p.startswith("/api/"):
                return self._send(404, {"error": "no such api"})
            return self._send(404, {"error": "not found"})
        except AuthError as e:
            msg = str(e)
            code = 403 if ("只有总部" in msg or "绑定" in msg) else 401
            return self._send(code, {"error": msg})
        except Exception as e:
            return self._send(500, {"error": str(e)[:400]})


# ---------------------------------------------------------------- API 实现
def sync_all_shops() -> dict:
    """把宝点账号里所有门店建进库（不拉机台，机台由任务逐个拉）"""
    bd = baodian()
    n = 0
    for s in bd.shops():
        try:
            ensure_shop(s["shopName"])
            n += 1
        except Exception:
            pass
    return {"shops": n}


def shops_with_units() -> list:
    with conn() as c:
        rs = [dict(r) for r in c.execute(
            "SELECT s.id, s.name, COUNT(u.id) AS units FROM shops s "
            "LEFT JOIN units u ON u.shop_id = s.id AND u.active = 1 "
            "WHERE COALESCE(s.group_name,'') NOT IN (%s) "
            "GROUP BY s.id ORDER BY s.name" % ",".join("?" * len(HIDDEN_GROUPS)),
            tuple(HIDDEN_GROUPS))]
    return rs


def api_setup() -> dict:
    """三步准备：门店 → 推币机 → 可以填"""
    with conn() as c:
        n_shops = c.execute("SELECT COUNT(*) n FROM shops").fetchone()["n"]
        n_units = c.execute("SELECT COUNT(*) n FROM units WHERE active=1").fetchone()["n"]
        n_ready = c.execute(
            "SELECT COUNT(DISTINCT shop_id) n FROM units WHERE active=1").fetchone()["n"]
        n_no_unit = c.execute(
            "SELECT COUNT(*) n FROM shops WHERE units_checked IS NULL").fetchone()["n"]
    return {
        "shops": n_shops,
        "shops_with_units": n_ready,
        "shops_without_units": n_no_unit,
        "units": n_units,
        "step1_done": n_shops > 0,
        "step2_done": n_units > 0,
    }


def api_overview(day: str) -> dict:
    require_hq()
    rows = []
    for sh in shops_with_units():
        if not sh["units"]:
            continue
        r = recon(sh["id"], day)
        s = r["summary"]
        rows.append({
            "shop_id": sh["id"], "shop": sh["name"], "units": sh["units"],
            "ok": s["ok"], "diff_ok": s["diff_ok"], "diff_all": s["diff_all"],
            "big": s["big"], "warn": s["warn"],
        })
    tot = {
        "shops": len(rows),
        "units": sum(x["units"] for x in rows),
        "ok": sum(x["ok"] for x in rows),
        "diff_ok": round(sum(x["diff_ok"] for x in rows), 1),
        "diff_all": round(sum(x["diff_all"] for x in rows), 1),
        "big": sum(x["big"] for x in rows),
    }
    return {"day": day, "rows": rows, "total": tot}


def fill_group_names():
    """老库里没有集团名，用宝点会话补一次"""
    try:
        bd = baodian()
        m = {s["shopName"]: s.get("customerName") for s in bd.shops()}
    except Exception:
        return
    with conn() as c:
        for name, grp in m.items():
            if grp:
                c.execute("UPDATE shops SET group_name=? WHERE name=? AND "
                          "(group_name IS NULL OR group_name='')", (grp, name))


def _machine_groups(shop_id: str) -> list:
    """按机台归组：一台机一个码，组里是它的各块码表（P1..Pn）"""
    with conn() as c:
        units = [dict(r) for r in c.execute(
            "SELECT id,name,kind,pos,code FROM units WHERE shop_id=? AND active=1 "
            "ORDER BY COALESCE(kind,'pusher'), name, COALESCE(pos,99), id", (shop_id,))]
        comms = {}
        for r in c.execute(
                "SELECT m.unit_id, m.comm_id FROM comms m JOIN units u ON u.id=m.unit_id "
                "WHERE u.shop_id=? ORDER BY m.comm_id", (shop_id,)):
            comms.setdefault(r["unit_id"], []).append(str(r["comm_id"]))
    groups: dict[tuple, list] = {}
    for u in units:
        base = re.sub(r"\s+\d{5,}$", "", u["name"]).strip() or u["name"]
        u["comms"] = comms.get(u["id"], [])
        groups.setdefault((u["kind"] or "pusher", base), []).append(u)
    out = []
    for (kind, base), us in groups.items():
        out.append({"kind": kind, "name": base, "units": us,
                    "code": us[0]["code"], "count": len(us)})
    return out


_QR_FONT_CACHE = {}


def _qr_font(size: int, bold: bool = False):
    """找一个中文字体，找不到退回默认。"""
    key = (size, bold)
    if key in _QR_FONT_CACHE:
        return _QR_FONT_CACHE[key]
    from PIL import ImageFont
    import glob
    pats = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-%s.ttc" % ("Bold" if bold else "Regular"),
        "/usr/share/fonts/opentype/noto/NotoSerifCJK-%s.ttc" % ("Bold" if bold else "Regular"),
        "/usr/share/fonts/truetype/noto/NotoSansCJK-%s.ttc" % ("Bold" if bold else "Regular"),
    ]
    cand = []
    for p0 in pats:
        if os.path.exists(p0):
            cand.append(p0)
    if not cand:
        cand = glob.glob("/usr/share/fonts/**/*CJK*", recursive=True)
    f = None
    for p0 in cand:
        try:
            f = ImageFont.truetype(p0, size)
            break
        except Exception:
            continue
    if f is None:
        f = ImageFont.load_default()
    _QR_FONT_CACHE[key] = f
    return f


def qr_label_png(code: str, url: str, title: str, sub: str) -> bytes:
    """一张贴纸：上面机台名，中间二维码，下面 P 位和通讯ID。"""
    import qrcode
    from PIL import Image, ImageDraw
    qr = qrcode.QRCode(border=1, box_size=9,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    qimg = qr.make_image(fill_color="black", back_color="white").convert("RGB")

    W = max(640, qimg.width + 80)
    top, bottom = 78, 96
    H = top + qimg.height + bottom
    img = Image.new("RGB", (W, H), "white")
    img.paste(qimg, ((W - qimg.width) // 2, top))
    d = ImageDraw.Draw(img)
    f1 = _qr_font(38, bold=True)
    f2 = _qr_font(26)
    tw = d.textlength(title, font=f1)
    d.text(((W - tw) / 2, 22), title, fill="black", font=f1)
    if sub:
        sw = d.textlength(sub, font=f2)
        d.text(((W - sw) / 2, top + qimg.height + 26), sub, fill="#3f3f46", font=f2)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _safe_name(s: str) -> str:
    return re.sub(r'[\\/:*?"<>|\s]+', "_", s).strip("_")


def qr_zip(shop_id: str, codes: list) -> bytes:
    """把选中的机台二维码打成 zip。"""
    import zipfile
    machines = _machine_groups(shop_id)
    by_code = {m["code"]: m for m in machines if m.get("code")}
    origin = _qr_origin()
    buf = io.BytesIO()
    used = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for c in codes:
            m = by_code.get(c)
            if not m:
                continue
            url = "%s/?c=%s" % (origin, c)
            sub = " / ".join(
                "P%s %s" % (u.get("pos") or i + 1, "/".join(u.get("comms") or []))
                for i, u in enumerate(m["units"]))
            png = qr_label_png(c, url, m["name"], sub)
            base = "%s_%s" % (c, _safe_name(m["name"]))
            name = base + ".png"
            k = 2
            while name in used:
                name = "%s_%d.png" % (base, k)
                k += 1
            used.add(name)
            z.writestr(name, png)
    return buf.getvalue()


def _qr_origin() -> str:
    """二维码里编的对外地址。"""
    if PUBLIC_BASE:
        return PUBLIC_BASE
    return "http://127.0.0.1:8899"


def qr_page(shop_id: str, origin: str, pre: str = "") -> str:
    """二维码页：一台机一张图，可勾选，下载成 zip。"""
    shop_id = guard_shop(shop_id)
    with conn() as c:
        shop = c.execute("SELECT id,name,code FROM shops WHERE id=?", (shop_id,)).fetchone()
    machines = _machine_groups(shop_id)
    cards = []
    for m in machines:
        sub = " / ".join(
            "P%s %s" % (u.get("pos") or i + 1, "/".join(u.get("comms") or []))
            for i, u in enumerate(m["units"]))
        cards.append(
            '<label class="qrc">'
            '<input type="checkbox" class="qrc__cb" value="%s">'
            '<img src="qr/img?c=%s" alt="%s" loading="lazy">'
            '<span class="qrc__t">%s</span>'
            '<span class="qrc__s">%s</span>'
            '</label>' % (m["code"], m["code"], m["name"], m["name"], sub))
    return """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>机台二维码 · %(shop)s</title>
<style>
 body{margin:0;padding:24px 24px 96px;font:14px/1.45 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#101012;background:#f6f7f9}
 h1{font-size:20px;margin:0 0 4px}
 p.hint{color:#6B6B73;margin:0 0 16px;font-size:13px}
 .bar{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:14px;
      padding:10px 0 12px;background:#f6f7f9}
 .bar label{display:inline-flex;align-items:center;gap:6px;font-size:13px;color:#3f3f46;cursor:pointer}
 .bar .n{font-size:13px;color:#6B6B73}
 .bar button{margin-left:auto;font:inherit;font-size:13px;font-weight:600;color:#fff;
      background:#1d4ed8;border:0;border-radius:8px;padding:9px 16px;cursor:pointer}
 .bar button:disabled{background:#c7c9ce;cursor:not-allowed}
 .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:14px}
 .qrc{position:relative;display:block;padding:10px;background:#fff;border:1px solid #e5e7eb;
      border-radius:10px;text-align:center;cursor:pointer}
 .qrc:has(input:checked){border-color:#1d4ed8;box-shadow:0 0 0 1px #1d4ed8}
 .qrc__cb{position:absolute;top:8px;left:8px;inline-size:16px;block-size:16px}
 .qrc img{display:block;width:100%%;height:auto;border-radius:6px}
 .qrc__t{display:block;margin-top:6px;font-size:13px;font-weight:600}
 .qrc__s{display:block;margin-top:2px;font-size:11px;color:#6B6B73;
      font-family:ui-monospace,Consolas,monospace;overflow-wrap:anywhere}
 @media print{body{padding:0}.bar{display:none}.grid{grid-template-columns:repeat(3,1fr)}}
</style></head><body>
<h1>%(shop)s · 机台二维码</h1>
<p class="hint">共 %(n)d 台。勾选要用的，下载成压缩包；二维码里编的是机台编号，改名字换卡头都不用重贴。</p>
<div class="bar">
  <label><input type="checkbox" id="all"> 全选</label>
  <span class="n" id="cnt">已选 0 台</span>
  <button type="button" id="dl" disabled>下载选中</button>
</div>
<div class="grid">%(cards)s</div>
<script>
(function(){
  var cbs = [].slice.call(document.querySelectorAll('.qrc__cb'));
  var all = document.getElementById('all');
  var cnt = document.getElementById('cnt');
  var dl = document.getElementById('dl');
  function sync(){
    var n = cbs.filter(function(x){return x.checked;}).length;
    cnt.textContent = '已选 ' + n + ' 台';
    dl.disabled = n === 0;
    all.checked = n === cbs.length && n > 0;
    all.indeterminate = n > 0 && n < cbs.length;
  }
  cbs.forEach(function(x){ x.addEventListener('change', sync); });
  all.addEventListener('change', function(){
    cbs.forEach(function(x){ x.checked = all.checked; });
    sync();
  });
  dl.addEventListener('click', function(){
    var codes = cbs.filter(function(x){return x.checked;}).map(function(x){return x.value;});
    if (!codes.length) return;
    location.href = 'qr/zip?shop_id=%(sid)s&codes=' + encodeURIComponent(codes.join(','));
  });
  sync();
})();
</script>
</body></html>""" % {"shop": shop["name"], "n": len(machines), "cards": "".join(cards),
                       "pre": pre, "sid": shop_id}



def api_unit(q) -> dict:
    """扫码直达：按编号 c= 或 id= 找到机台，返回它的全部码表。"""
    day = (q.get("day") or [date.today().isoformat()])[0]
    code = (q.get("c") or [""])[0].strip()
    uid = (q.get("id") or [""])[0]
    with conn() as c:
        if code:
            row = c.execute("SELECT * FROM units WHERE code=?", (code,)).fetchone()
        elif uid:
            row = c.execute("SELECT * FROM units WHERE id=?", (int(uid),)).fetchone()
        else:
            raise RuntimeError("缺少机台参数")
        if not row:
            raise RuntimeError("这个码没有对应的机台，可能已停用")
        guard_shop(row["shop_id"])
        shop = c.execute("SELECT id,name,code FROM shops WHERE id=?", (row["shop_id"],)).fetchone()

    base = re.sub(r"\s+\d{5,}$", "", row["name"]).strip() or row["name"]
    kind = row["kind"] or "pusher"
    ev = entry_view(row["shop_id"], day, kind)
    rows = [r for r in ev["rows"]
            if (re.sub(r"\s+\d{5,}$", "", r["name"]).strip() or r["name"]) == base
            and r.get("active") is not False]
    rows.sort(key=lambda r: r.get("pos") or 99)
    # 下一台：同店同类里，按编号排在后面那台
    machines = [m for m in _machine_groups(row["shop_id"]) if m["kind"] == kind]
    machines.sort(key=lambda m: m["code"] or "")
    codes = [m["code"] for m in machines]
    nxt = None
    if row["code"] in codes:
        i = codes.index(row["code"])
        if i + 1 < len(codes):
            nxt = {"code": codes[i + 1], "name": machines[i + 1]["name"]}
    return {"day": day, "kind": kind, "shop": dict(shop),
            "machine": {"name": base, "code": row["code"]},
            "next": nxt, "rows": rows}


def api_kinds(q) -> dict:
    """导航页：两类机台各多少台、今天填了几台、误差多少。"""
    sid = guard_shop((q.get("shop_id") or [meta_get("shop_id")])[0])
    day = (q.get("day") or [date.today().isoformat()])[0]
    out = []
    for kind, name in KINDS:
        ev = entry_view(sid, day, kind)
        rows = [r for r in ev["rows"] if r.get("active") is not False]
        filled = sum(1 for r in rows if r["in_r"] is not None or r["out_r"] is not None)
        rc = recon(sid, day, kind)
        s = rc.get("summary") or {}
        out.append({
            "kind": kind, "name": name,
            "units": len(rows), "filled": filled,
            "ok": s.get("ok", 0), "diff_ok": s.get("diff_ok", 0),
        })
    return {"day": day, "kinds": out}


def api_welcome_done(b: dict) -> dict:
    """第一次进来的引导弹窗，看过就记下来（按门店记）。"""
    shop_id = guard_shop(b["shop_id"])
    with conn() as c:
        c.execute("UPDATE shops SET units_confirmed=COALESCE(units_confirmed,?) WHERE id=?",
                  (now(), shop_id))
    return {"ok": True}


def api_me() -> dict:
    a = actor()
    if not a:
        raise AuthError("未登录")
    own = resolve_shop(a) if a.get("type") == "store" else None
    shop = None
    if own:
        with conn() as c:
            r = c.execute("SELECT id,name FROM shops WHERE id=?", (own,)).fetchone()
            shop = dict(r) if r else None
    return {
        "type": a.get("type"),
        "name": a.get("name"),
        "cwsj_name": a.get("name"),
        "shop": shop,
        "bound": bool(own) if a.get("type") == "store" else True,
    }


def api_bootstrap():
    init_db()
    sid = meta_get("shop_id")
    if not sid:
        try:
            sid = ensure_shop(DEFAULT_SHOP)
            meta_set("shop_id", sid)
        except Exception:
            sid = None
    fill_group_names()
    shops = [dict(r) for r in conn().execute(
        "SELECT * FROM shops WHERE COALESCE(group_name,'') NOT IN (%s) "
        "ORDER BY group_name, name" % ",".join("?" * len(HIDDEN_GROUPS)),
        tuple(HIDDEN_GROUPS))]
    # 门店身份：只看得见自己这一家
    if is_store():
        own = resolve_shop(actor())
        if own:
            shops = [s for s in shops if s["id"] == own]
            sid = own
        else:
            shops = []
    units = units_of(sid) if sid else []
    bd_ok, bd_msg = True, ""
    try:
        baodian()
    except Exception as e:
        bd_ok, bd_msg = False, str(e)[:200]

    # 第一次进来：还没有机台清单就从宝点拉一份（按推币机大类推导）
    if sid and bd_ok and not units:
        try:
            info = sync_units_best(sid)
            units = units_of(sid)
            meta_set("units_src_day", info.get("day") or "")
        except Exception as e:
            bd_msg = f"拉机台清单失败: {str(e)[:160]}"

    bd_shops = []
    if bd_ok:
        try:
            bd_shops = [s["shopName"] for s in baodian().shops()]
        except Exception:
            bd_shops = []

    return {
        "shop_id": sid,
        "shops": shops,
        "bd_shops": bd_shops,
        "units": units,
        "dates": dates_with_data(sid) if sid else [],
        "baodian_ok": bd_ok,
        "baodian_msg": bd_msg,
        "today": date.today().isoformat(),
        "edit_day": edit_day(),
        "scope_class": SCOPE_CLASS,
        "rate": RATE,
        "units_confirmed": (shop_row(sid) or {}).get("units_confirmed") if sid else None,
    }


# ---------------------------------------------------------------- 基础资料维护
def manage_shops() -> list:
    require_hq()
    with conn() as c:
        rs = [dict(r) for r in c.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM units u WHERE u.shop_id=s.id AND u.active=1) AS units "
            "FROM shops s WHERE COALESCE(s.group_name,'') NOT IN (%s) "
            "ORDER BY s.group_name, s.name" % ",".join("?" * len(HIDDEN_GROUPS)),
            tuple(HIDDEN_GROUPS))]
    return rs


def manage_units(shop_id: str) -> list:
    with conn() as c:
        us = [dict(r) for r in c.execute(
            "SELECT * FROM units WHERE shop_id=? ORDER BY active DESC, name", (shop_id,))]
        for u in us:
            u["comms"] = [dict(r) for r in c.execute(
                "SELECT comm_id, console_no FROM comms WHERE unit_id=? ORDER BY comm_id", (u["id"],))]
    return us


def manage_machines(shop_id: str) -> list:
    """按机台名归组：一台机一个框，框里是它的卡头（通讯ID）"""
    import re as _re
    shop_id = guard_shop(shop_id)
    us = [u for u in manage_units(shop_id) if u.get("active")]
    groups: dict[str, list] = {}
    for u in us:
        base = _re.sub(r"\s+\d{5,}$", "", u["name"]).strip() or u["name"]
        groups.setdefault(base, []).append(u)
    out = []
    for base, units in groups.items():
        comms = [c["comm_id"] for u in units for c in u["comms"]]
        out.append({
            "key": base,
            "kind": (units[0].get("kind") or "pusher"),
            "units": units,
            "comms": comms,
            "heads": len(comms),
            "merged": len(units) == 1 and len(comms) > 1,
            "shared": any(bool(u["shared"]) for u in units),
            "active": any(bool(u["active"]) for u in units),
        })
    order = {k: i for i, (k, _) in enumerate(KINDS)}
    out.sort(key=lambda g: (order.get(g["kind"], 9), not g["active"], g["key"]))
    return out


def save_shop_values(b: dict) -> dict:
    """弹珠机的珠值 / 卡片价值，按店配置。"""
    shop_id = guard_shop(b["shop_id"])
    bead = b.get("bead_value")
    card = b.get("card_value")
    with conn() as c:
        c.execute("UPDATE shops SET bead_value=?, card_value=? WHERE id=?",
                  (bead, card, shop_id))
    return {"ok": True}


def save_shop(b: dict) -> dict:
    require_hq()
    sid = b.get("id")
    name = (b.get("name") or "").strip()
    if not name:
        raise RuntimeError("门店名不能为空")
    with conn() as c:
        if sid:
            c.execute("UPDATE shops SET name=? WHERE id=?", (name, sid))
        else:
            bd_name = (b.get("baodian_name") or name).strip()
            bd = baodian()
            hit = next((x for x in bd.shops() if x["shopName"] == bd_name), None)
            if not hit:
                raise RuntimeError("宝点里找不到门店 %s" % bd_name)
            sid = hit["id"]
            c.execute("INSERT INTO shops(id,name,customer_id,shop_id,created_at) VALUES(?,?,?,?,?) "
                      "ON CONFLICT(id) DO UPDATE SET name=excluded.name",
                      (sid, name, hit["customerId"], hit["id"], now()))
    return {"ok": True, "id": sid}


def remove_shop(shop_id: str) -> dict:
    require_hq()
    with conn() as c:
        c.execute("DELETE FROM comms WHERE unit_id IN (SELECT id FROM units WHERE shop_id=?)",
                  (shop_id,))
        c.execute("DELETE FROM units WHERE shop_id=?", (shop_id,))
        c.execute("DELETE FROM readings WHERE shop_id=?", (shop_id,))
        c.execute("DELETE FROM card_daily WHERE shop_id=?", (shop_id,))
        c.execute("DELETE FROM shops WHERE id=?", (shop_id,))
    return {"ok": True}


def save_unit(b: dict) -> dict:
    shop_id = guard_shop(b["shop_id"])
    name = (b.get("name") or "").strip()
    if not name:
        raise RuntimeError("机台名不能为空")
    shared = 1 if b.get("shared") else 0
    kind = b.get("kind") if b.get("kind") in ("pusher", "pachinko") else "pusher"
    raw = b.get("comms") or ""
    if isinstance(raw, list):
        comms = [str(x).strip() for x in raw if str(x).strip()]
    else:
        comms = [x.strip() for x in str(raw).replace("，", ",").split(",") if x.strip()]
    if not comms:
        raise RuntimeError("至少要填一个通讯ID")

    with conn() as c:
        uid = b.get("id")
        if uid:
            c.execute("UPDATE units SET name=?, shared=?, locked=1, kind=? WHERE id=?",
                      (name, shared, kind, uid))
            uid = int(uid)
        else:
            c.execute("INSERT INTO units(shop_id,name,team,shared,active,locked,kind) "
                      "VALUES(?,?,?,?,1,1,?) "
                      "ON CONFLICT(shop_id,name) DO UPDATE SET shared=excluded.shared, "
                      "active=1, locked=1, kind=excluded.kind",
                      (shop_id, name, b.get("team"), shared, kind))
            uid = c.execute("SELECT id FROM units WHERE shop_id=? AND name=?",
                            (shop_id, name)).fetchone()["id"]
        # 先解绑不在新清单里的通讯ID
        keep = set(comms)
        for r in c.execute("SELECT comm_id FROM comms WHERE unit_id=?", (uid,)):
            if r["comm_id"] not in keep:
                c.execute("DELETE FROM comms WHERE comm_id=?", (r["comm_id"],))
        for cid in comms:
            c.execute("INSERT INTO comms(comm_id,unit_id,console_no,team,device,updated_at) "
                      "VALUES(?,?,NULL,NULL,NULL,?) ON CONFLICT(comm_id) DO UPDATE SET "
                      "unit_id=excluded.unit_id, updated_at=excluded.updated_at",
                      (cid, uid, now()))
    return {"ok": True, "id": uid}


def merge_units(shop_id: str, ids: list, name: str = "", lock: bool = True) -> dict:
    """把多台合成一台：通讯ID 合并，码表改成共用，其余单元删掉。"""
    import re as _re
    shop_id = guard_shop(shop_id)
    ids = [int(x) for x in (ids or [])]
    if len(ids) < 2:
        raise RuntimeError("至少选两台")
    marks = ",".join("?" * len(ids))
    with conn() as c:
        units = [dict(r) for r in c.execute(
            "SELECT * FROM units WHERE shop_id=? AND id IN (%s) ORDER BY id" % marks,
            (shop_id, *ids))]
        if len(units) < 2:
            raise RuntimeError("这些机台不属于这家店")
        keep = units[0]
        comms = [r["comm_id"] for r in c.execute(
            "SELECT comm_id FROM comms WHERE unit_id IN (%s)" % marks, tuple(ids))]
        new_name = (name or "").strip() or _re.sub(r"\s+\d{5,}$", "", keep["name"])
        c.execute("UPDATE units SET name=?, shared=1, locked=? WHERE id=?",
                  (new_name, 1 if lock else 0, keep["id"]))
        for cid in comms:
            c.execute("UPDATE comms SET unit_id=? WHERE comm_id=?", (keep["id"], cid))
        # 历史读数迁到保留的那台（同一天已有时跳过）
        for u in units[1:]:
            for r in c.execute("SELECT day,in_r,out_r,note FROM readings WHERE unit_id=?",
                               (u["id"],)).fetchall():
                exists = c.execute("SELECT 1 FROM readings WHERE shop_id=? AND unit_id=? AND day=?",
                                   (shop_id, keep["id"], r["day"])).fetchone()
                if not exists:
                    c.execute("INSERT INTO readings(shop_id,unit_id,day,in_r,out_r,note,updated_at) "
                              "VALUES(?,?,?,?,?,?,?)",
                              (shop_id, keep["id"], r["day"], r["in_r"], r["out_r"], r["note"], now()))
            c.execute("DELETE FROM readings WHERE unit_id=?", (u["id"],))
            c.execute("DELETE FROM units WHERE id=?", (u["id"],))
    return {"ok": True, "id": keep["id"], "name": new_name, "comms": len(comms)}


def save_machine(b: dict) -> dict:
    """改一台机：名称 + 卡头清单。

    不改合并状态：原来是分开的就还是分开（每个卡头一个单元），
    原来是一块的还是一块。要合并/拆开用专门的按钮。
    """
    import re as _re
    shop_id = guard_shop(b["shop_id"])
    ids = [int(x) for x in (b.get("ids") or [])]
    name = (b.get("name") or "").strip()
    if not name:
        raise RuntimeError("机台名不能为空")
    raw = b.get("comms") or ""
    comms = [x.strip() for x in str(raw).replace("，", ",").split(",") if x.strip()]
    if not comms:
        raise RuntimeError("至少要填一个通讯ID")
    if not ids:
        raise RuntimeError("没有指定机台")

    marks = ",".join("?" * len(ids))
    with conn() as c:
        units = [dict(r) for r in c.execute(
            "SELECT * FROM units WHERE shop_id=? AND id IN (%s) ORDER BY id" % marks,
            (shop_id, *ids))]
        if not units:
            raise RuntimeError("找不到这台机")
        for u in units:
            u["comms"] = [r["comm_id"] for r in c.execute(
                "SELECT comm_id FROM comms WHERE unit_id=? ORDER BY comm_id", (u["id"],))]
        merged_before = len(units) == 1 and len(units[0]["comms"]) > 1

        if merged_before:
            keep = units[0]["id"]
            c.execute("UPDATE units SET name=?, shared=?, locked=1, active=1 WHERE id=?",
                      (name, 1 if len(comms) > 1 else 0, keep))
            wanted = set(comms)
            for r in c.execute("SELECT comm_id FROM comms WHERE unit_id=?", (keep,)).fetchall():
                if r["comm_id"] not in wanted:
                    c.execute("DELETE FROM comms WHERE comm_id=?", (r["comm_id"],))
            for cid in comms:
                c.execute("INSERT INTO comms(comm_id,unit_id,console_no,team,device,updated_at) "
                          "VALUES(?,?,NULL,NULL,NULL,?) ON CONFLICT(comm_id) DO UPDATE SET "
                          "unit_id=excluded.unit_id, updated_at=excluded.updated_at",
                          (cid, keep, now()))
            return {"ok": True, "id": keep, "merged": True}

        # 保持分散：按通讯ID 复用单元，缺的补，多的删
        by_comm = {}
        for u in units:
            for cid in u["comms"]:
                by_comm[cid] = u["id"]
        for cid in comms:
            label = "%s %s" % (name, cid)
            uid = by_comm.get(cid)
            if uid:
                c.execute("UPDATE units SET name=?, shared=0, locked=1, active=1 WHERE id=?",
                          (label, uid))
            else:
                c.execute("INSERT INTO units(shop_id,name,team,shared,active,locked) "
                          "VALUES(?,?,?,0,1,1) ON CONFLICT(shop_id,name) DO UPDATE SET "
                          "active=1, locked=1", (shop_id, label, None))
                uid = c.execute("SELECT id FROM units WHERE shop_id=? AND name=?",
                                (shop_id, label)).fetchone()["id"]
            c.execute("INSERT INTO comms(comm_id,unit_id,console_no,team,device,updated_at) "
                      "VALUES(?,?,NULL,NULL,NULL,?) ON CONFLICT(comm_id) DO UPDATE SET "
                      "unit_id=excluded.unit_id, updated_at=excluded.updated_at",
                      (cid, uid, now()))
        wanted = set(comms)
        for u in units:
            if not u["comms"]:
                c.execute("DELETE FROM units WHERE id=?", (u["id"],))
            elif not set(u["comms"]) & wanted:
                c.execute("DELETE FROM readings WHERE unit_id=?", (u["id"],))
                c.execute("DELETE FROM comms WHERE unit_id=?", (u["id"],))
                c.execute("DELETE FROM units WHERE id=?", (u["id"],))
    return {"ok": True, "merged": False}


def split_unit(uid: int) -> dict:
    """把一个共用码表的单元拆回每卡头一个单元。"""
    import re as _re
    with conn() as c:
        u = c.execute("SELECT * FROM units WHERE id=?", (uid,)).fetchone()
        if not u:
            raise RuntimeError("找不到这台机")
        guard_shop(u["shop_id"])
        comms = [r["comm_id"] for r in c.execute(
            "SELECT comm_id FROM comms WHERE unit_id=? ORDER BY comm_id", (uid,))]
        if len(comms) < 2:
            raise RuntimeError("只有一个卡头，不用拆")
        base = _re.sub(r"\s+\d{5,}$", "", u["name"]).strip() or u["name"]
        first = "%s %s" % (base, comms[0])
        c.execute("UPDATE units SET name=?, shared=0, locked=0 WHERE id=?", (first, uid))
        for cid in comms[1:]:
            label = "%s %s" % (base, cid)
            c.execute("INSERT INTO units(shop_id,name,team,shared,active) VALUES(?,?,?,0,1) "
                      "ON CONFLICT(shop_id,name) DO UPDATE SET active=1, shared=0",
                      (u["shop_id"], label, u["team"]))
            nid = c.execute("SELECT id FROM units WHERE shop_id=? AND name=?",
                            (u["shop_id"], label)).fetchone()["id"]
            c.execute("UPDATE comms SET unit_id=? WHERE comm_id=?", (nid, cid))
    return {"ok": True, "units": len(comms)}


def remove_unit(uid: int) -> dict:
    with conn() as c:
        row = c.execute("SELECT shop_id FROM units WHERE id=?", (uid,)).fetchone()
        if row:
            guard_shop(row["shop_id"])
        c.execute("DELETE FROM comms WHERE unit_id=?", (uid,))
        c.execute("DELETE FROM units WHERE id=?", (uid,))
    return {"ok": True}


def confirm_units(shop_id: str, unit_ids: list) -> dict:
    """店长核对机台清单：勾上的启用，没勾的停用，并记确认时间"""
    shop_id = guard_shop(shop_id)
    keep = set(int(x) for x in (unit_ids or []))
    with conn() as c:
        ids = [r["id"] for r in c.execute(
            "SELECT id FROM units WHERE shop_id=?", (shop_id,))]
        for uid in ids:
            c.execute("UPDATE units SET active=? WHERE id=?",
                      (1 if uid in keep else 0, uid))
        c.execute("UPDATE shops SET units_confirmed=? WHERE id=?", (now(), shop_id))
        n = c.execute("SELECT COUNT(*) n FROM units WHERE shop_id=? AND active=1",
                      (shop_id,)).fetchone()["n"]
    return {"ok": True, "active": n}


def api_entry(q):
    sid = guard_shop((q.get("shop_id") or [meta_get("shop_id")])[0])
    day = (q.get("day") or [edit_day()])[0]
    kind = (q.get("kind") or ["pusher"])[0]
    out = entry_view(sid, day, kind)
    out["can_edit"] = can_edit(day)
    out["edit_day"] = edit_day()
    return out


def api_recon_note(b) -> dict:
    shop_id = guard_shop(b["shop_id"])
    return recon_note(shop_id, b["day"], int(b["unit_id"]),
                      (b.get("reason") or "").strip(), (b.get("note") or "").strip())


def api_recon_range(q) -> dict:
    """区间核对：一次取多天的行，给导出用。最多 62 天。"""
    sid = guard_shop((q.get("shop_id") or [meta_get("shop_id")])[0])
    kind = (q.get("kind") or ["pusher"])[0]
    start = (q.get("from") or [edit_day()])[0]
    end = (q.get("to") or [start])[0]
    d0 = date.fromisoformat(start)
    d1 = date.fromisoformat(end)
    if d1 < d0:
        d0, d1 = d1, d0
    if (d1 - d0).days > 62:
        raise RuntimeError("一次最多导 62 天")
    rows = []
    d = d0
    while d <= d1:
        day = d.isoformat()
        r = recon(sid, day, kind)
        for row in r["rows"]:
            rows.append({
                "day": day,
                "name": row["name"],
                "meter_in": row["meter_in"],
                "meter_out": row["meter_out"],
                "up": row["up"],
                "down": row["down"],
                "head": row["head"],
                "meter": row["meter"],
                "diff": row["diff"],
                "rate": row["rate"],
                "status": row["status"],
                "reason": row.get("reason"),
                "reason_note": row.get("reason_note"),
            })
        d += timedelta(days=1)
    return {"from": d0.isoformat(), "to": d1.isoformat(), "kind": kind, "rows": rows}


def api_recon(q):
    sid = guard_shop((q.get("shop_id") or [meta_get("shop_id")])[0])
    day = (q.get("day") or [date.today().isoformat()])[0]
    kind = (q.get("kind") or ["pusher"])[0]
    return recon(sid, day, kind)


def api_audit(q):
    sid = guard_shop((q.get("shop_id") or [meta_get("shop_id")])[0])
    with conn() as c:
        rs = [dict(r) for r in c.execute(
            "SELECT a.*, u.name FROM audit a LEFT JOIN units u ON u.id=a.unit_id "
            "WHERE a.shop_id=? ORDER BY a.id DESC LIMIT 200", (sid,))]
    return {"rows": rs}


def main():
    port = int(os.environ.get("PORT") or 8765)
    init_db()
    host = os.environ.get("METER_HOST", "0.0.0.0")
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"码表核对 · http://127.0.0.1:{port}")
    if host == "0.0.0.0":
        import socket as _sk
        try:
            _s = _sk.socket(_sk.AF_INET, _sk.SOCK_DGRAM)
            _s.connect(("8.8.8.8", 80))
            lan = _s.getsockname()[0]
            _s.close()
            print(f"手机访问 · http://{lan}:{port}/")
        except Exception:
            pass
    srv.serve_forever()


if __name__ == "__main__":
    main()
