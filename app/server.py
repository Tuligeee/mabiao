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
        if "provider" not in scols:
            c.execute("ALTER TABLE shops ADD COLUMN provider TEXT DEFAULT 'baodian'")
        if "jj_base" not in scols:
            c.execute("ALTER TABLE shops ADD COLUMN jj_base TEXT")
        if "jj_shop_id" not in scols:
            c.execute("ALTER TABLE shops ADD COLUMN jj_shop_id TEXT")
        if "jj_chain_id" not in scols:
            c.execute("ALTER TABLE shops ADD COLUMN jj_chain_id TEXT")
        if "code" not in scols:
            c.execute("ALTER TABLE shops ADD COLUMN code TEXT")
        ucols = [r["name"] for r in c.execute("PRAGMA table_info(units)")]
        if "from_day" not in ucols:
            c.execute("ALTER TABLE units ADD COLUMN from_day TEXT")
        if "to_day" not in ucols:
            c.execute("ALTER TABLE units ADD COLUMN to_day TEXT")
        if "code" not in ucols:
            c.execute("ALTER TABLE units ADD COLUMN code TEXT")
        if "pos" not in ucols:
            c.execute("ALTER TABLE units ADD COLUMN pos INTEGER")
        if "beads_per_coin" not in scols:
            c.execute("ALTER TABLE shops ADD COLUMN beads_per_coin REAL")
        if "brand" not in ucols:
            c.execute("ALTER TABLE units ADD COLUMN brand TEXT")

        # 门店编号：和任务平台对齐，直接用它的门店 ID（ST01..）；没对齐的才临时编 S01..
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

        # 机台品牌：没填过的按机台名自动补一次，之后以库里存的为准
        for r in c.execute("SELECT id, name FROM units "
                           "WHERE (brand IS NULL OR brand='') AND kind='pachinko'"):
            c.execute("UPDATE units SET brand=? WHERE id=?",
                      (unit_brand(r["name"]), r["id"]))

        # 位置号：同一台机下按通讯ID顺序重排 P1..Pn。
        # 每次都全量重算 —— 只补 NULL 的话，删掉重建的机台会停在 P1（可乐风暴撞过一次）。
        rows = [dict(r) for r in c.execute(
            "SELECT u.id, u.shop_id, u.name, "
            "  (SELECT MIN(comm_id) FROM comms WHERE unit_id=u.id) AS first_comm "
            "FROM units u ORDER BY u.shop_id, u.name, u.id")]
        groups = {}
        for r in rows:
            # 「塔兰弹珠机-001 817879」和「萌力双胞胎弹珠机-001」都要归到同一组
            base = re.sub(r"[\s\-_]+\d+$", "", r["name"]).strip() or r["name"]
            groups.setdefault((r["shop_id"], base), []).append(r)
        for _key, items in groups.items():
            used = {it["pos"] for it in items if it.get("pos")}
            nxt = 1
            # 已分配过的 P 位不再重排：贴纸印了 P1..Pn，换卡头不该让它变
            for it in sorted(items, key=lambda x: (x["first_comm"] or "999999", x["id"])):
                if it.get("pos"):
                    continue
                while nxt in used:
                    nxt += 1
                c.execute("UPDATE units SET pos=? WHERE id=?", (nxt, it["id"]))
                used.add(nxt)


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
        # 任务平台的 name 是「杭州店」这种简称，brand 才是和码表一致的全名，
        # 所以先拿 brand 匹配，再退回 name（2026-11-26 修：ST16 杭州店绑不上）
        tries = [x for x in ((me.get("brand") or "").strip(), (me.get("name") or "").strip()) if x]
        name = tries[-1] if tries else ""
        cands = []
        for cand_name in tries:
            n = norm_store(cand_name)
            if not n:
                continue
            cands = [s for s in shops if norm_store(s["name"]) == n]
            if not cands:
                # 包含匹配：「鄂州」↔「鄂州吾悦」
                cands = [s for s in shops
                         if n in norm_store(s["name"]) or norm_store(s["name"]) in n]
            if len(cands) == 1:
                name = cand_name
                break

        if len(cands) == 1:
            c.execute("INSERT OR REPLACE INTO shop_map(cwsj_key,shop_id) VALUES(?,?)",
                      (key, cands[0]["id"]))
            if not os.environ.get("METER_QUIET"):
                print("[bind] %r -> %s" % (name, cands[0]["name"]), flush=True)
            return cands[0]["id"]
        if not os.environ.get("METER_QUIET"):
            print("[bind] 未匹配 cwsj=%r key=%r 候选=%s | 本地门店=%s"
                  % (name, key, [x["name"] for x in cands] or "无",
                     " / ".join(s["name"] for s in shops)), flush=True)
    return None


def actor() -> dict | None:
    return getattr(ACTOR, "me", None)


def is_store() -> bool:
    a = actor()
    return bool(a and a.get("type") == "store")


def one(v) -> str:
    """参数取值：可能是字符串，也可能是列表（query 是列表，JSON body 是字符串）。

    以前统一写 `(x or [""])[0]`，遇到字符串就只取第一个字符 ——
    鄂州店扫码后存成了 `meter-3.json` 而不是 `meter-3be437fd-….json`，
    于是「扫完还是宝点登录」（2026-11-26 踩过）。
    """
    if isinstance(v, (list, tuple)):
        return str(v[0]) if v else ""
    return str(v or "")


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


# ---------------------------------------------------------------- 宝点扫码登录
BD_LOGIN_BASE = (os.environ.get("BD_LOGIN_BASE") or "http://127.0.0.1:8770").rstrip("/")
BD_KEY_FILE = os.environ.get("BD_LINK_KEY_FILE") or "/opt/bd-login/link_key.txt"
# bd-login 按 key 另存的那份，和日报/套餐共用的 session.json 分开
METER_BD_DIR = os.environ.get("METER_BD_DIR") or "/opt/taocan/.cache/sessions"
# 全局那份（总部扫的），所有店兜底用
SHARED_BD_FILE = os.environ.get("BD_SESSION_FILE") or "/opt/taocan/.cache/session.json"


def meter_bd_file(shop_id: str = "") -> str:
    """每家店自己那份登录态；没给门店就是旧的单份。"""
    if not shop_id:
        return os.path.join(METER_BD_DIR, "meter.json")
    return os.path.join(METER_BD_DIR, "meter-%s.json" % shop_id)


def bd_link_key() -> str:
    try:
        with open(BD_KEY_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _bd_read(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def bd_alive(shop_id: str = "") -> dict:
    """这家店的宝点登录态行不行；本店那份优先，没有再退回总部那份。"""
    for src, path in (("shop", meter_bd_file(shop_id)),
                      ("shared", SHARED_BD_FILE)):
        if src == "shop" and not shop_id:
            path = os.path.join(METER_BD_DIR, "meter.json")
        d = _bd_read(path)
        exp = float(d.get("exp") or 0) or 0
        if not exp:
            continue
        left = (exp - time.time()) / 3600
        if left > 0:
            return {"alive": True, "source": src,
                    "exp_text": time.strftime("%Y-%m-%d %H:%M", time.localtime(exp)),
                    "left_hours": round(left, 2),
                    "account": ((d.get("ctx") or {}).get("user") or {}).get("name") or ""}
    return {"alive": False, "source": "", "exp_text": "", "left_hours": -1, "account": ""}


BD_AUTO_LOGIN = os.environ.get("METER_BD_AUTO", "1") == "1"
BD_VENV_PY = os.environ.get("METER_BD_VENV_PY") or "/opt/日报自动化_服务器部署包/.venv/bin/python"
BD_LOGIN_PY = os.path.join(str(BASE), "baodian_login.py")
# 统一入口：定时器用的同一个脚本（同一套阈值 + 同一把锁），没有就退回老的 --ensure
BD_REFRESH_PY = os.environ.get("METER_BD_REFRESH_PY") or os.path.join(str(BASE), "bin", "bd_refresh.py")
_BD_AUTO_TS = [0.0]


def bd_auto_login(force: bool = False) -> dict:
    """登录态过期就用账密自动重登（走日报 venv 的 python，那边有 ddddocr）。

    成功会重写 /opt/taocan/.cache/session.json，所有工具一起受益。
    有 bin/bd_refresh.py 就走它——跟 30 分钟一次的定时器同一套阈值和同一把锁，
    避免两边同时过验证码。
    """
    if not BD_AUTO_LOGIN:
        return {"ok": False, "skipped": "自动登录已关闭"}
    if not (os.path.exists(BD_VENV_PY) and os.path.exists(BD_LOGIN_PY)):
        return {"ok": False, "skipped": "本机没有登录脚本/依赖环境（只在服务器上生效）"}
    now = time.time()
    if not force and now - _BD_AUTO_TS[0] < 300:      # 五分钟内只试一次，别把账号打到风控
        return {"ok": False, "skipped": "刚试过，等一会儿"}
    _BD_AUTO_TS[0] = now
    import subprocess
    cmd = ([BD_VENV_PY, BD_REFRESH_PY, "--if-needed"]
           if os.path.exists(BD_REFRESH_PY) else [BD_VENV_PY, BD_LOGIN_PY, "--ensure"])
    try:
        pr = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, timeout=300)
        out = pr.stdout.decode("utf-8", "replace").strip().splitlines()
        data = {}
        for line in reversed(out):
            line = line.strip()
            if line.startswith("{"):
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                break
        if pr.returncode != 0:
            return {"ok": False, "error": (data.get("error") or "登录失败")[:200]}
        return data or {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:200]}


def bd_call(path: str, body: dict, timeout=90) -> dict:
    import requests
    k = bd_link_key()
    url = BD_LOGIN_BASE + path + (("?k=" + k) if k else "")
    r = requests.post(url, json=body, timeout=timeout, proxies={"http": None, "https": None})
    return r.json()


# ---------------------------------------------------------------- 宝点
class Baodian:
    def __init__(self, shop_id: str = ""):
        if SHARED not in sys.path:
            sys.path.insert(0, SHARED)
        import baodian_session as S            # noqa: E402
        self.S = S
        # 本店扫码登录那份优先；没有/过期再退回总部那份
        old_file = getattr(S, "SESSION_FILE", None)
        try:
            own = meter_bd_file(shop_id)
            if shop_id and os.path.exists(own):
                d = _bd_read(own)
                exp = float(d.get("exp") or 0) or 0
                if exp > time.time():
                    S.SESSION_FILE = own
            self.d = S.as_login_dict()
        finally:
            if old_file is not None:
                S.SESSION_FILE = old_file

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


_BD_BY_SHOP: dict = {}


def baodian(shop_id: str = "") -> Baodian:
    global _BD, _BD_ERR
    key = shop_id or ""
    if key and key in _BD_BY_SHOP:
        return _BD_BY_SHOP[key]
    if not key and _BD is not None:
        return _BD
    try:
        bd = Baodian(key)
    except Exception as e:                      # 登录态失效
        _BD_ERR = str(e)[:300]
        raise RuntimeError(_BD_ERR)
    _BD_ERR = None
    if key:
        _BD_BY_SHOP[key] = bd
    else:
        _BD = bd
    return bd


def reset_baodian():
    global _BD
    _BD = None
    _BD_BY_SHOP.clear()


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
-- 核对结果冻结：总部锁定后这一天不再随数据变化
CREATE TABLE IF NOT EXISTS recon_frozen(
  shop_id TEXT, day TEXT, unit_id INTEGER, payload TEXT, frozen_at TEXT, frozen_by TEXT,
  PRIMARY KEY(shop_id, day, unit_id));
-- 鲸舰机台日汇总：机器维度，没有卡头
CREATE TABLE IF NOT EXISTS machine_daily(
  shop_id TEXT, day TEXT, machine_name TEXT,
  coins REAL, play_count REAL, raw TEXT, fetched_at TEXT,
  PRIMARY KEY(shop_id, day, machine_name));
-- 码表归零：这天起读数从 0 算，不再减前一天
CREATE TABLE IF NOT EXISTS unit_resets(
  unit_id INTEGER, day TEXT, by TEXT, at TEXT, PRIMARY KEY(unit_id, day));
-- 卡头归属历史：一个卡头哪天起归哪台机，换机台时旧封口、新行开张
CREATE TABLE IF NOT EXISTS comm_owner(
  comm_id TEXT, unit_id INTEGER, shop_id TEXT,
  from_day TEXT, to_day TEXT, PRIMARY KEY(comm_id, from_day));
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
-- 码表照片：店长抄表时拍的凭证，只留 45 天
CREATE TABLE IF NOT EXISTS photos(
  id TEXT PRIMARY KEY, shop_id TEXT, unit_id INTEGER, day TEXT,
  path TEXT, bytes INTEGER, w INTEGER, h INTEGER, at TEXT, by TEXT,
  expired INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def init_db():
    with conn() as c:
        c.executescript(SCHEMA)
        # 老库：把当前 comms 归属补成历史（从 EPOCH 起一直归这台机）
        if not c.execute("SELECT 1 FROM comm_owner LIMIT 1").fetchone():
            for r in c.execute("SELECT m.comm_id, m.unit_id, u.shop_id FROM comms m "
                               "JOIN units u ON u.id=m.unit_id"):
                c.execute("INSERT OR IGNORE INTO comm_owner(comm_id,unit_id,shop_id,from_day,to_day) "
                          "VALUES(?,?,?,?,NULL)", (r["comm_id"], r["unit_id"], r["shop_id"], EPOCH_DAY))
            c.commit()          # 先放锁，ensure_codes 会另开连接
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
        c.execute("CREATE TABLE IF NOT EXISTS brand_alias("
                  "key TEXT PRIMARY KEY, label TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS brand_flags("
                  "brand TEXT PRIMARY KEY, flags TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS recon_notes("
                  "shop_id TEXT, day TEXT, unit_id INTEGER, reason TEXT, note TEXT, "
                  "updated_at TEXT, updated_by TEXT, PRIMARY KEY(shop_id,day,unit_id))")
        ensure_codes()
        # 老机台补登记日（列在 ensure_codes 里才建出来，必须放它后面）
        c.execute("UPDATE units SET from_day = COALESCE((SELECT MIN(r.day) FROM readings r "
                  "WHERE r.unit_id = units.id), ?) WHERE from_day IS NULL", (EPOCH_DAY,))
        ucols = [r["name"] for r in c.execute("PRAGMA table_info(units)")]
        if "locked" not in ucols:
            c.execute("ALTER TABLE units ADD COLUMN locked INTEGER DEFAULT 0")
        if "kind" not in ucols:
            c.execute("ALTER TABLE units ADD COLUMN kind TEXT DEFAULT 'pusher'")
        c.execute("UPDATE units SET kind='pachinko' WHERE team LIKE '%弹珠%' AND "
                  "(kind IS NULL OR kind='pusher')")
        rcols = [r["name"] for r in c.execute("PRAGMA table_info(readings)")]
        if "unit_snap" not in rcols:
            c.execute("ALTER TABLE readings ADD COLUMN unit_snap TEXT")
        if "per_coin_snap" not in rcols:
            c.execute("ALTER TABLE readings ADD COLUMN per_coin_snap REAL")
        # 总出珠计法快照：1=含首出珠，0=只计中奖；改了品牌开关不改写历史
        if "out_incl_snap" not in rcols:
            c.execute("ALTER TABLE readings ADD COLUMN out_incl_snap INTEGER")
        for col in ("p_tokens", "p_out", "p_cards"):
            if col not in rcols:
                c.execute("ALTER TABLE readings ADD COLUMN %s REAL" % col)
        scols = [r["name"] for r in c.execute("PRAGMA table_info(shops)")]
        for col in ("bead_value", "card_value"):
            if col not in scols:
                c.execute("ALTER TABLE shops ADD COLUMN %s REAL" % col)
        pcols = [r["name"] for r in c.execute("PRAGMA table_info(photos)")]
        if "expired" not in pcols:
            c.execute("ALTER TABLE photos ADD COLUMN expired INTEGER DEFAULT 0")
    migrate_machines()


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
    bd = baodian(d.get("id") or "")
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


def unique_unit_name(c, shop_id: str, base: str, skip_id=None) -> str:
    """重名时才加 (2)(3)，绝不往机台名后面拼通讯ID（2026-11-26 要求）。"""
    base = (base or "").strip() or "未命名"
    for i in range(1, 60):
        cand = base if i == 1 else "%s (%d)" % (base, i)
        row = c.execute("SELECT id FROM units WHERE shop_id=? AND name=?",
                        (shop_id, cand)).fetchone()
        if not row or (skip_id is not None and row["id"] == skip_id):
            return cand
    return base

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
            mid = ensure_machine(c, shop_id, name, kind)
            if team == SHARED_TEAM:
                c.execute("UPDATE machines SET shared=1, active=1 WHERE id=?", (mid,))
            else:
                c.execute("UPDATE machines SET active=1 WHERE id=?", (mid,))
            base = c.execute("SELECT name FROM machines WHERE id=?", (mid,)).fetchone()["name"]
            for i, d in enumerate(ds, start=1):
                cid = str(d.get("commId") or "").strip()
                if not cid:
                    continue
                nm = base if i == 1 else "%s P%d" % (base, i)
                c.execute("INSERT INTO units(shop_id,name,team,shared,active,kind,pos,machine_id,"
                          "from_day) VALUES(?,?,?,0,1,?,?,?,?) "
                          "ON CONFLICT(shop_id,name) DO UPDATE SET team=excluded.team, active=1, "
                          "kind=excluded.kind, pos=excluded.pos, machine_id=excluded.machine_id",
                          (shop_id, nm, team, kind, i, mid, date.today().isoformat()))
                u = c.execute("SELECT id FROM units WHERE shop_id=? AND name=?",
                              (shop_id, nm)).fetchone()["id"]
                put_comm(c, d, u)
                made += 1
    return made


def sync_units_best(shop_id: str) -> dict:
    """取最近 3 天里「推币机」设备最多的那天来推导机台。

    不用当天：设备当天可能没全上报，会少几台。
    """
    bd = baodian(shop_id)
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


EPOCH_DAY = "1970-01-01"


def _tomorrow(day: str | None = None) -> str:
    d = date.fromisoformat(day) if day else date.today()
    return (d + timedelta(days=1)).isoformat()


def comm_owner_set(c, comm_id: str, unit_id: int, shop_id: str, day: str):
    """把卡头归属改到 unit_id。

    新装的卡头从 day 生效；从一个机台挪到另一个机台则从次日生效——
    否则当天同一个卡头的流水会被两台机各算一遍，或者旧机当天那部分直接丢。
    """
    row = c.execute("SELECT unit_id, shop_id, from_day, to_day FROM comm_owner "
                    "WHERE comm_id=? ORDER BY from_day DESC LIMIT 1", (comm_id,)).fetchone()
    if row and row["unit_id"] == unit_id and (row["to_day"] is None or row["to_day"] > day):
        return                                     # 没变，别重复记
    moved = bool(row) and row["unit_id"] != unit_id
    eff = _tomorrow(day) if moved else day
    if row:
        c.execute("UPDATE comm_owner SET to_day=? WHERE comm_id=? AND from_day=? "
                  "AND (to_day IS NULL OR to_day>?)",
                  (eff, comm_id, row["from_day"], eff))
    # 回到原机台：把封口线接上，不新开一行
    back = c.execute("UPDATE comm_owner SET to_day=NULL WHERE comm_id=? AND unit_id=? "
                     "AND to_day=?", (comm_id, unit_id, eff))
    if back.rowcount:
        return
    c.execute("INSERT OR REPLACE INTO comm_owner(comm_id, unit_id, shop_id, from_day, to_day) "
              "VALUES(?,?,?,?,NULL)", (comm_id, unit_id, shop_id, eff))


def comm_owner_move(c, comm_id: str, unit_id: int, shop_id: str, day: str):
    """从 day 起把卡头改归 unit_id：别的机台当天封口，这台机当天开张。

    合并 / 拆分改的是「这块表归哪台机」，属于配置，当天生效；
    物理换机台才走 comm_owner_set 的次日生效，避免一天的数据被两台机各算一次。
    """
    c.execute("UPDATE comm_owner SET to_day=? WHERE comm_id=? AND unit_id<>? "
              "AND (to_day IS NULL OR to_day>?)", (day, comm_id, unit_id, day))
    cur = c.execute("SELECT 1 FROM comm_owner WHERE comm_id=? AND unit_id=? "
                    "AND from_day<=? AND (to_day IS NULL OR to_day>?) LIMIT 1",
                    (comm_id, unit_id, day, day)).fetchone()
    if not cur:
        c.execute("INSERT OR REPLACE INTO comm_owner(comm_id,unit_id,shop_id,from_day,to_day) "
                  "VALUES(?,?,?,?,NULL)", (comm_id, unit_id, shop_id, day))


def comms_on(unit_id: int, day: str) -> list:
    """this unit 在 day 这天挂哪些卡头（历史归属口径）。"""
    with conn() as c:
        return [str(r["comm_id"]) for r in c.execute(
            "SELECT DISTINCT comm_id FROM comm_owner WHERE unit_id=? AND from_day<=? "
            "AND (to_day IS NULL OR to_day>?) ORDER BY comm_id", (unit_id, day, day))]


# ---------------------------------------------------------------- 机台（一台机多个 P 位）
SHARED_DEFAULT = {"凤凰城", "法老也疯狂"}   # 4 个卡头共用 1 块码表的机型


def machine_base(name: str) -> str:
    """「可乐风暴 733586」「可乐风暴 P5」→「可乐风暴」"""
    s = re.sub(r"\s+\d{5,}$", "", str(name or ""))
    s = re.sub(r"\s*\(\d+\)\s*$", "", s)
    s = re.sub(r"\s+P\d+$", "", s)
    return s.strip() or str(name or "")


def machine_of(unit_row) -> dict:
    """取这台机所属的机台；没有就返回空。"""
    mid = unit_row["machine_id"] if "machine_id" in unit_row.keys() else None
    if not mid:
        return {}
    with conn() as c:
        r = c.execute("SELECT * FROM machines WHERE id=?", (mid,)).fetchone()
    return dict(r) if r else {}


def machine_units(machine_id: int) -> list:
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM units WHERE machine_id=? ORDER BY pos, id", (machine_id,))]


def machine_comms(machine_id: int, day: str | None = None) -> list:
    """机台下的全部卡头（按 P 位排）；给 day 就是那天归属的。"""
    out, seen = [], set()
    units = machine_units(machine_id)
    with conn() as c:
        for u in units:
            if day:
                cs = comms_on(u["id"], day)
            else:
                cs = [str(r["comm_id"]) for r in c.execute(
                    "SELECT comm_id FROM comms WHERE unit_id=? ORDER BY comm_id", (u["id"],))]
            for cid in cs:
                if cid not in seen:
                    seen.add(cid)
                    out.append(cid)
    return out


def migrate_machines() -> int:
    """老库补「机台」这一层：按名字归组、每台一个 units（P 位），幂等。"""
    made = 0
    with conn() as c:
        c.execute("CREATE TABLE IF NOT EXISTS machines("
                  "id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id TEXT NOT NULL, "
                  "name TEXT NOT NULL, kind TEXT DEFAULT 'pusher', shared INTEGER DEFAULT 0, "
                  "active INTEGER DEFAULT 1, code TEXT, sort INTEGER DEFAULT 0, brand TEXT, "
                  "UNIQUE(shop_id, name))")
        # 只做一次性搬迁：已经有机器表就不再动（新机台由 save_unit / sync 自己建）
        if c.execute("SELECT COUNT(*) FROM machines").fetchone()[0]:
            return 0
        ucols = [r["name"] for r in c.execute("PRAGMA table_info(units)")]
        if "machine_id" not in ucols:
            c.execute("ALTER TABLE units ADD COLUMN machine_id INTEGER")
        rows = [dict(r) for r in c.execute("SELECT * FROM units WHERE machine_id IS NULL")]
        groups: dict = {}
        for u in rows:
            groups.setdefault((u["shop_id"], machine_base(u["name"])), []).append(u)
        for (shop_id, base), us in groups.items():
            kind = (us[0].get("kind") or "pusher")
            shared = 1 if base in SHARED_DEFAULT else 0
            c.execute("INSERT OR IGNORE INTO machines(shop_id,name,kind,shared,active,code) "
                      "VALUES(?,?,?,?,1,?)",
                      (shop_id, base, kind, shared, us[0].get("code")))
            m = c.execute("SELECT id FROM machines WHERE shop_id=? AND name=?",
                          (shop_id, base)).fetchone()["id"]
            ids = [u["id"] for u in us]
            keep = us[0]
            keep_id = keep["id"]
            # 先把整组卡头挂到要保留的单元上，再删掉多余单元（comms.unit_id 不能为空）
            qs = ",".join("?" * len(ids))
            c.execute("UPDATE comms SET unit_id=? WHERE unit_id IN (%s)" % qs,
                      [keep_id] + ids)
            for u in us[1:]:
                c.execute("DELETE FROM units WHERE id=?", (u["id"],))
            c.execute("UPDATE units SET machine_id=?, name=?, pos=1 WHERE id=?",
                      (m, base, keep_id))
            comms = [str(r["comm_id"]) for r in c.execute(
                "SELECT comm_id FROM comms WHERE unit_id=? ORDER BY comm_id", (keep_id,))]
            # 一个卡头一个「码表位」；共用码表的机台在核对时再合并
            for i, cid in enumerate(comms[1:], start=1):
                c.execute("INSERT INTO units(shop_id,name,team,shared,active,sort,kind,code,pos,"
                          "machine_id) VALUES(?,?,?,0,1,0,?,NULL,?,?)",
                          (shop_id, "%s P%d" % (base, i + 1), keep.get("team"),
                           keep.get("kind") or "pusher", i + 1, m))
                nid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
                c.execute("UPDATE comms SET unit_id=? WHERE comm_id=?", (nid, cid))
            made += 1
        # 归属历史按新结构重建（读数已清空，不需要保历史）
        if made:
            c.execute("DELETE FROM comm_owner")
            c.execute("INSERT INTO comm_owner(comm_id,unit_id,shop_id,from_day,to_day) "
                      "SELECT m.comm_id, m.unit_id, u.shop_id, '1970-01-01', NULL "
                      "FROM comms m JOIN units u ON u.id = m.unit_id WHERE m.unit_id IS NOT NULL")
    return made


def put_comm(c, d, unit_id):
    cid = str(d.get("commId"))
    c.execute("INSERT INTO comms(comm_id,unit_id,console_no,team,device,updated_at) "
              "VALUES(?,?,?,?,?,?) ON CONFLICT(comm_id) DO UPDATE SET "
              "unit_id=excluded.unit_id, console_no=excluded.console_no, "
              "team=excluded.team, device=excluded.device, updated_at=excluded.updated_at",
              (cid, unit_id, d.get("consoleNo"),
               d.get("projectTeamName"), d.get("deviceName"), now()))
    u = c.execute("SELECT shop_id FROM units WHERE id=?", (unit_id,)).fetchone()
    if u:
        comm_owner_set(c, cid, unit_id, u["shop_id"], date.today().isoformat())


def units_of(shop_id: str, active_only: bool = True, kind: str | None = None,
             day: str | None = None) -> list:
    if day:
        # 按「这一天在不在用」取：卖掉的老机台仍能出现在历史那天
        where = ("AND (from_day IS NULL OR from_day<=?) "
                 "AND (to_day IS NULL OR to_day>?)")
    else:
        where = "AND active=1" if active_only else ""
    args = [shop_id] if not day else [shop_id, day, day]
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
def fetch_jingjian_day(shop_id: str, day: str) -> dict:
    """鲸舰店：拉机台投币额，落 machine_daily。"""
    import jingjian
    d = shop_row(shop_id) or {}
    base = d.get("jj_base") or jingjian.DEFAULT_BASE
    # 按 P 位取：一台机多个 P 就是多条，第 2 个起名字带 (2)(3)，和拆开码表的命名一致
    rows = jingjian.day_consoles(base, (d.get("jj_shop_id") or "").strip(), day,
                                 (d.get("jj_chain_id") or "").strip())
    ts = now()
    # 同一个 P 号可能有好几个出币口（鲸舰里 2P 重复出现），所以按顺序编号，别拿 P 号当唯一键
    seq: dict = {}
    for r in sorted(rows, key=lambda x: (x["machine"], x.get("pos") or 1, str(x.get("console_id") or ""))):
        i = seq.get(r["machine"], 0) + 1
        seq[r["machine"]] = i
        r["seq"] = i
    with conn() as c:
        c.execute("DELETE FROM machine_daily WHERE shop_id=? AND day=?", (shop_id, day))
        for r in rows:
            if not r["machine"]:
                continue
            name = r["machine"] if r["seq"] <= 1 else "%s P%d" % (r["machine"], r["seq"])
            c.execute("INSERT OR REPLACE INTO machine_daily"
                      "(shop_id,day,machine_name,coins,play_count,raw,fetched_at) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (shop_id, day, name, r["coins"], r["count"],
                       json.dumps(r, ensure_ascii=False), ts))
    return {"ok": True, "provider": "jingjian", "machines": len(rows)}


def sync_jingjian_comms(shop_id: str, days: int = 7) -> dict:
    """从鲸舰消费明细里把每台机的卡头补进码表：P位 + 通讯ID。

    鲸舰汇总接口只有机台级金额，明细接口才带 consoleNo / commId。只补卡头，
    不动机台名，也不新增机台（对不上名字的列出来给人看）。
    """
    require_hq()
    shop_id = guard_shop(shop_id)
    d = shop_row(shop_id) or {}
    if (d.get("provider") or "baodian") != "jingjian":
        raise RuntimeError("这家店不是鲸舰取数")
    import jingjian
    base = d.get("jj_base") or jingjian.DEFAULT_BASE
    shop_no = str(d.get("jj_shop_id") or "").strip()
    if not shop_no:
        raise RuntimeError("这家店没配鲸舰门店编号（jj_shop_id）")
    days = max(1, int(days or 7))
    day_to = date.today().isoformat()
    day_from = (date.today() - timedelta(days=days - 1)).isoformat()
    items = jingjian.machine_consoles(base, shop_no, day_from, day_to)
    added = 0
    updated = []
    missed = []
    with conn() as c:
        units = {r["name"]: dict(r) for r in c.execute(
            "SELECT id,name FROM units WHERE shop_id=? AND active=1", (shop_id,))}
        for it in items:
            u = units.get(it["machine"])
            if not u:
                missed.append(it["machine"])
                continue
            for cs in it["consoles"]:
                exists = c.execute("SELECT 1 FROM comms WHERE comm_id=?", (cs["comm"],)).fetchone()
                c.execute("INSERT INTO comms(comm_id,unit_id,console_no,updated_at) VALUES(?,?,?,?) "
                          "ON CONFLICT(comm_id) DO UPDATE SET unit_id=excluded.unit_id, "
                          "console_no=excluded.console_no, updated_at=excluded.updated_at",
                          (cs["comm"], u["id"], cs["p"], now()))
                comm_owner_set(c, cs["comm"], u["id"], shop_id, day_to)
                if not exists:
                    added += 1
            if len(it["consoles"]) > 1:
                c.execute("UPDATE units SET shared=1 WHERE id=?", (u["id"],))
            updated.append({"machine": it["machine"], "consoles": len(it["consoles"])})
    return {"ok": True, "machines": len(items), "comms_added": added,
            "updated": updated, "missed": missed, "from": day_from, "to": day_to}


def fetch_shiruan_day(shop_id: str, day: str) -> dict:
    """世软店：拉机台收币，落 machine_daily（世软也是机台维度）。"""
    import shiruan
    d = shop_row(shop_id) or {}
    store_id = (d.get("jj_shop_id") or "").strip()
    if not store_id:
        raise RuntimeError("这家店没配世软门店编号（jj_shop_id）")
    rows = shiruan.day_summary(day, store_id)
    ts = now()
    with conn() as c:
        c.execute("DELETE FROM machine_daily WHERE shop_id=? AND day=?", (shop_id, day))
        for r in rows:
            c.execute("INSERT OR REPLACE INTO machine_daily"
                      "(shop_id,day,machine_name,coins,play_count,raw,fetched_at) "
                      "VALUES(?,?,?,?,?,?,?)",
                      (shop_id, day, r["machine"], r["coins"], r["count"],
                       json.dumps(r, ensure_ascii=False), ts))
    return {"ok": True, "provider": "shiruan", "machines": len(rows)}


def fetch_day(shop_id: str, day: str) -> dict:
    """拉一天的上币 + 下币，落 card_daily（鲸舰店走机台汇总）。"""
    d = shop_row(shop_id) or {}
    prov = d.get("provider") or "baodian"
    if prov == "jingjian":
        return fetch_jingjian_day(shop_id, day)
    if prov == "shiruan":
        return fetch_shiruan_day(shop_id, day)
    if prov == "manual":
        return {"ok": True, "provider": "manual", "skipped": "只抄表，没有卡头数据"}
    # 宝点店：登录态过期就先拿账密自动登一次（成功后所有店一起恢复）
    if not bd_alive(shop_id).get("alive"):
        auto = bd_auto_login()
        if not auto.get("ok") and not auto.get("skipped"):
            raise RuntimeError("宝点登录已过期，自动登录也没成功：%s" % auto.get("error"))
    shop = shop_of(d)
    bd = baodian(shop_id)
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


def editable_days() -> tuple:
    """门店能改的日期：今天 + 昨天（有些店晚上 10 点关门当天就填）。"""
    t = date.today()
    return (t.isoformat(), (t - timedelta(days=1)).isoformat())


def can_edit(day: str) -> bool:
    if not is_store():
        return True
    return day in editable_days()


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
                      "WHERE shop_id=? AND unit_id=? AND day<? "
                      "AND (in_r IS NOT NULL OR out_r IS NOT NULL OR p_tokens IS NOT NULL "
                      "     OR p_out IS NOT NULL OR p_cards IS NOT NULL) "
                      "ORDER BY day DESC LIMIT 1",
                      (shop_id, unit_id, day)).fetchone()
    return dict(r) if r else None


def unit_reset_set(unit_id: int, day: str, on: bool = True) -> dict:
    with conn() as c:
        if on:
            c.execute("INSERT OR REPLACE INTO unit_resets(unit_id, day, by, at) VALUES(?,?,?,?)",
                      (unit_id, day, (actor() or {}).get("name") or "?", now()))
        else:
            c.execute("DELETE FROM unit_resets WHERE unit_id=? AND day=?", (unit_id, day))
    return {"ok": True}


def frozen_info(shop_id: str, day: str):
    with conn() as c:
        r = c.execute("SELECT frozen_at, frozen_by, COUNT(*) n FROM recon_frozen "
                      "WHERE shop_id=? AND day=?", (shop_id, day)).fetchone()
    if not r or not r["n"]:
        return None
    return {"at": r["frozen_at"], "by": r["frozen_by"], "n": r["n"]}


def recon_freeze(shop_id: str, day: str) -> dict:
    """把这一天算一遍存下来；以后都读这份。"""
    d = recon(shop_id, day, None)
    who = (actor() or {}).get("name") or "?"
    with conn() as c:
        c.execute("DELETE FROM recon_frozen WHERE shop_id=? AND day=?", (shop_id, day))
        for r in d["rows"]:
            c.execute("INSERT INTO recon_frozen(shop_id,day,unit_id,payload,frozen_at,frozen_by) "
                      "VALUES(?,?,?,?,?,?)",
                      (shop_id, day, r["unit_id"], json.dumps(r, ensure_ascii=False), now(), who))
    return {"ok": True, "rows": len(d["rows"]), "summary": d["summary"]}


def recon_unfreeze(shop_id: str, day: str) -> dict:
    with conn() as c:
        c.execute("DELETE FROM recon_frozen WHERE shop_id=? AND day=?", (shop_id, day))
    return {"ok": True}


def recon(shop_id: str, day: str, kind: str | None = None) -> dict:
    frz = frozen_info(shop_id, day)
    if frz:
        with conn() as c:
            rows = [json.loads(r["payload"]) for r in c.execute(
                "SELECT payload FROM recon_frozen WHERE shop_id=? AND day=? ORDER BY unit_id",
                (shop_id, day))]
        if kind:
            rows = [r for r in rows if (r.get("kind") or "pusher") == kind]
        ok = [r for r in rows if r["status"] == "正常"]
        return {
            "day": day, "rows": rows, "frozen": frz,
            "summary": {
                "total": len(rows), "ok": len(ok),
                "diff_all": round(sum(r["diff"] for r in rows if r["diff"] is not None), 1),
                "diff_ok": round(sum(r["diff"] for r in ok if r["diff"] is not None), 1),
                "warn": len([r for r in rows if r["status"] in
                             ("读数比昨天小", "第一次填", "今天还没填",
                              "卡头今天没有数据")]),
                "big": len([r for r in rows if r["diff"] is not None and abs(r["diff"]) >= 1000]),
            },
        }
    units = units_of(shop_id, kind=kind, day=day)
    prov = (shop_row(shop_id) or {}).get("provider") or "baodian"
    with conn() as c:
        cards = {}
        for r in c.execute("SELECT * FROM card_daily WHERE shop_id=? AND day=?", (shop_id, day)):
            cards[r["comm_id"]] = dict(r)
        mcards = {}
        for r in c.execute("SELECT * FROM machine_daily WHERE shop_id=? AND day=?", (shop_id, day)):
            mcards[r["machine_name"]] = dict(r)
        reads = {}
        for r in c.execute("SELECT * FROM readings WHERE shop_id=? AND day=?", (shop_id, day)):
            reads[r["unit_id"]] = dict(r)
        notes = {}
        for r in c.execute("SELECT * FROM recon_notes WHERE shop_id=? AND day=?", (shop_id, day)):
            notes[r["unit_id"]] = dict(r)
        resets = {r["unit_id"] for r in c.execute(
            "SELECT unit_id FROM unit_resets WHERE day=?", (day,))}
        # 卡头ID 按 P 位排（P1 在前），和墙上/Excel 的顺序对得上
        cno = {r["comm_id"]: (r["console_no"] or "") for r in c.execute(
            "SELECT comm_id, console_no FROM comms")}
        # 当天的照片张数（核对页要能点开看）
        ph = {}
        for r in c.execute("SELECT unit_id, COUNT(*) n, MAX(expired) ex FROM photos "
                           "WHERE shop_id=? AND day=? GROUP BY unit_id", (shop_id, day)):
            ph[r["unit_id"]] = {"n": r["n"], "expired": bool(r["ex"])}
        mach = {r["id"]: dict(r) for r in c.execute("SELECT * FROM machines")}

    # 共用一块码表的机台：只留一行，卡头算整台机的（按机台去重，不看 pos）
    seen_shared = set()
    shown = []
    for u in units:
        mu = mach.get(u.get("machine_id")) or {}
        if mu.get("shared"):
            if mu["id"] in seen_shared:
                continue
            seen_shared.add(mu["id"])
            u = dict(u)
            u["name"] = mu["name"]
            u["comms"] = [{"comm_id": x} for x in machine_comms(mu["id"], day)]
        shown.append(u)
    units = shown
    today = date.today().isoformat()
    rows = []
    seen_shared = set()
    for u in units:
        # 已停用的机台：当天没填过数就不再占核对页一格（停用当天填过的留最后一天）
        if not u["active"] and not reads.get(u["id"]) and day >= today:
            continue
        # 共用一块码表的机台只出一行
        mu = mach.get(u.get("machine_id")) or {}
        if mu.get("shared"):
            if mu["id"] in seen_shared:
                continue
            seen_shared.add(mu["id"])
        hist = machine_comms(mu["id"], day) if mu.get("shared") else comms_on(u["id"], day)
        comms = hist if hist else [str(x["comm_id"]) for x in u["comms"]]
        # 卡头ID 按 P 位排（P1 在前），和墙上/Excel 的顺序对得上
        comms_sorted = sorted(comms, key=lambda x: (cno.get(x, "~"), x))
        got = [cards[c] for c in comms if c in cards]
        up = sum((g["up_native"] or 0) + (g["up_give"] or 0) for g in got) if got else None
        # 机台维度（鲸舰 / 世软）：一台机多个卡头时要把各卡头的收币加起来
        if not got and mcards:
            hits = [mcards[c] for c in comms if c in mcards]
            if not hits:
                hit = mcards.get(u["name"])
                if hit:
                    hits = [hit]
            if hits:
                # 这类系统只有投币额，没有下币
                up = sum((h["coins"] or 0) for h in hits)
                got = [{"up_native": up, "up_give": 0, "down_qty": 0}]
        down = sum((g["down_qty"] or 0) for g in got) / RATE if got else None
        rd = reads.get(u["id"])
        pv = prev_reading(shop_id, u["id"], day) if rd else None
        # 这天码表归零了：增量直接取当天读数
        reset = (u["id"] in resets)

        is_p = (u.get("kind") == "pachinko")
        def delta(field):
            if not rd:
                return None
            if reset:
                return rd[field] or 0
            if not pv or rd[field] is None or pv[field] is None:
                return None
            return (rd[field] or 0) - (pv[field] or 0)

        tin = delta("in_r")
        tout = delta("out_r")

        p_metrics = None
        d_tokens = d_out = d_cards = None
        if is_p:
            d_tokens = delta("p_tokens")
            d_out = delta("p_out")
            d_cards = delta("p_cards")
            # 码表投币一律记币（不再有「记珠」）；一币几珠按门店参数，老数据读快照
            row_shop0 = shop_row(shop_id) or {}
            u_brand = brand_of(u)
            per_coin = ((rd["per_coin_snap"] if rd and rd["per_coin_snap"] else None)
                        or row_shop0.get("beads_per_coin") or brand_per_coin(u_brand))
            # 总出珠计法：含首出珠 → 中奖 = 总出珠 − 投币×一币几珠
            #            只计中奖 → 中奖 = 总出珠（不用一币几珠）
            snap_incl = rd["out_incl_snap"] if rd and rd["out_incl_snap"] is not None else None
            out_incl = brand_out_incl(u_brand) if snap_incl is None else bool(snap_incl)
            paid_beads = (tin * per_coin) if (out_incl and tin is not None and per_coin) else None
            if out_incl:
                win = (d_out - paid_beads) if (d_out is not None and paid_beads is not None) else None
            else:
                win = d_out
            keep = (d_tokens - win) if (d_tokens is not None and win is not None) else None
            rate_p = (win / d_tokens) if (win is not None and d_tokens) else None
            row_shop = shop_row(shop_id) or {}
            bead = row_shop.get("bead_value")
            cardv = row_shop.get("card_value")
            profit = None
            if keep is not None and d_cards is not None and bead and cardv:
                profit = keep * bead - d_cards * cardv
            # 出卡率 = 出卡 ÷ 中奖出珠；成本率 = 卡成本 ÷ 珠营收（珠营收 = 实际收珠 × 珠值）
            card_rate = (d_cards / win) if (d_cards is not None and win) else None
            cost_rate = None
            if d_cards is not None and cardv and bead and keep and keep > 0:
                cost_rate = (d_cards * cardv) / (keep * bead)
            p_metrics = {
                "coins": tin, "coins_norm": tin, "unit": DEFAULT_UNIT, "out_incl": out_incl,
                "per_coin": per_coin, "paid": paid_beads,
                "tokens": d_tokens, "out": d_out, "cards": d_cards,
                "win": win, "keep": keep, "rate": rate_p, "profit": profit,
                "card_rate": card_rate, "cost_rate": cost_rate,
            }

        head = None
        if rd and got:
            head = (up or 0) - (0 if is_p else (down or 0))
        if is_p:
            # 机器上就是「总投币数」，直接跟宝点上币比；不折珠
            meter = tin
        else:
            meter = (tin - tout) if (tin is not None and tout is not None) else None

        # 读数本身的毛病优先于「有没有卡头数据」：先报第一次填、再报倒退
        neg = any(v is not None and v < 0 for v in (tin, tout, d_tokens, d_out, d_cards))
        if not rd:
            status = "今天还没填"
        elif pv is None and not reset:
            status = "第一次填"
        elif neg and not reset:
            status = "读数比昨天小"
        elif not got and prov == "manual":
            status = "只抄表（无卡头）"
        elif not got:
            status = "卡头今天没有数据"
        else:
            status = "正常"

        diff = (head - meter) if (head is not None and meter is not None) else None
        rate = (diff / meter) if (diff is not None and meter) else None

        rows.append({
            "kind": u.get("kind") or "pusher",
            "brand": u_brand if is_p else None,
            "fields": MODE_FIELDS[brand_mode(u_brand)] if is_p else None,
            "p": p_metrics,
            "unit_id": u["id"], "name": u["name"], "team": u["team"],
            "shared": bool(u["shared"]), "comm_count": len(comms),
            "comms": comms_sorted,
            "poss": _poss_one(cno, comms_sorted),
            "up": up, "down": down, "head": head, "meter_in": tin, "meter_out": tout,
            "meter": meter, "diff": diff, "rate": rate, "status": status,
            "in_r": rd["in_r"] if rd else None, "out_r": rd["out_r"] if rd else None,
            "prev_in": pv["in_r"] if pv else None, "prev_out": pv["out_r"] if pv else None,
            "note": rd["note"] if rd else None,
            "reset": bool(reset),
            "photos": ph.get(u["id"], {}).get("n", 0),
            "photos_expired": bool(ph.get(u["id"], {}).get("expired")),
            "reason": (notes.get(u["id"]) or {}).get("reason"),
            "reason_note": (notes.get(u["id"]) or {}).get("note"),
        })

    ok = [r for r in rows if r["status"] == "正常"]
    return {
        "day": day,
        "rows": rows,
        "frozen": None,
        "summary": {
            "total": len(rows),
            "ok": len(ok),
            "diff_all": round(sum(r["diff"] for r in rows if r["diff"] is not None), 1),
            "diff_ok": round(sum(r["diff"] for r in ok if r["diff"] is not None), 1),
            "warn": len([r for r in rows if r["status"] in ("读数比昨天小", "第一次填", "今天还没填", "卡头今天没有数据")]),
            "big": len([r for r in rows if r["diff"] is not None and abs(r["diff"]) >= 1000]),
        },
    }


def save_readings(shop_id: str, day: str, items: list) -> dict:
    """items: [{unit_id, in_r, out_r, note}] → 保存 + 留痕 + 返回读数下降告警"""
    if not can_edit(day):
        raise RuntimeError("门店只能填今天和昨天的码表，%s 不能改了" % day)
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
            urow = c.execute("SELECT name, brand, kind FROM units WHERE id=?", (uid,)).fetchone()
            b_key = (urow["brand"] if urow and urow["brand"] else brand_of(dict(urow))) if urow else ""
            is_p = bool(urow and urow["kind"] == "pachinko")
            unit_snap = brand_unit(b_key) if is_p else None
            # 一币几珠现在按门店参数，抄数时连店里的值一起冻结进快照
            shop_v = shop_row(shop_id) or {}
            per_snap = ((shop_v.get("beads_per_coin") or brand_per_coin(b_key)) if is_p else None)
            incl_snap = (1 if brand_out_incl(b_key) else 0) if is_p else None
            c.execute("INSERT INTO readings(shop_id,unit_id,day,in_r,out_r,note,updated_at,"
                      "p_tokens,p_out,p_cards,unit_snap,per_coin_snap,out_incl_snap) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                      "ON CONFLICT(shop_id,unit_id,day) DO UPDATE SET "
                      "in_r=excluded.in_r, out_r=excluded.out_r, note=excluded.note, "
                      "updated_at=excluded.updated_at, p_tokens=excluded.p_tokens, "
                      "p_out=excluded.p_out, p_cards=excluded.p_cards, "
                      "unit_snap=COALESCE(readings.unit_snap, excluded.unit_snap), "
                      "per_coin_snap=COALESCE(readings.per_coin_snap, excluded.per_coin_snap), "
                      "out_incl_snap=COALESCE(readings.out_incl_snap, excluded.out_incl_snap)",
                      (shop_id, uid, day, new_in, new_out, it.get("note"), ts,
                       it.get("p_tokens"), it.get("p_out"), it.get("p_cards"),
                       unit_snap, per_snap, incl_snap))

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
        # 鲸舰 / 世软是机台维度：有数的是 machine_daily 里的机台名，不是卡头流水
        have_machine = {r["machine_name"] for r in
                        c.execute("SELECT machine_name FROM machine_daily WHERE shop_id=? AND day=?",
                                  (shop_id, day))}
        pics = {}
        for r in c.execute("SELECT id, unit_id, at, w, h, expired FROM photos "
                           "WHERE shop_id=? AND day=? ORDER BY at", (shop_id, day)):
            pics.setdefault(r["unit_id"], []).append(
                {"id": r["id"], "at": r["at"], "w": r["w"], "h": r["h"],
                 "expired": bool(r["expired"])})
        mach = {r["id"]: dict(r) for r in c.execute("SELECT * FROM machines")}
    rows = []
    seen_shared = set()
    for u in units:
        m = mach.get(u.get("machine_id")) or {}
        if m.get("shared"):
            if m["id"] in seen_shared:
                continue
            seen_shared.add(m["id"])
            u = dict(u)
            u["name"] = m["name"]
            u["comms"] = [{"comm_id": x} for x in machine_comms(m["id"], day)]
        rd = reads.get(u["id"])
        pv = prev_reading(shop_id, u["id"], day)
        comms = [str(x["comm_id"]) for x in u["comms"]]
        # 卡头清单：P 位取 comms.console_no（units.pos 是老字段，拆完都还是 1）
        pcomms = [{"p": (x.get("console_no") or "").strip(), "comm": str(x["comm_id"])}
                  for x in u["comms"] if x.get("comm_id")]
        rows.append({
            "unit_id": u["id"], "name": u["name"], "team": u["team"], "shared": bool(u["shared"]),
            "active": bool(u["active"]),
            "kind": u.get("kind") or "pusher",
            "brand": brand_of(u) if (u.get("kind") == "pachinko") else None,
            "fields": brand_flags(brand_of(u))["fields"] if (u.get("kind") == "pachinko") else None,
            "unit": brand_unit(brand_of(u)) if (u.get("kind") == "pachinko") else None,
            "pos": u.get("pos"), "code": u.get("code"),
            "comms": comms, "pcomms": pcomms,
            "has_card": (any(c in have_card or c in have_machine for c in comms)
                         or (u["name"] or "").strip() in have_machine),
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
            "photos": pics.get(u["id"]) or [],
        })
    return {"day": day, "rows": rows,
            "confirmed": bool(shop.get("units_confirmed")),
            "confirmed_at": shop.get("units_confirmed")}


# ---------------------------------------------------------------- 码表照片
PHOTO_DIR = DATA / "photos"
PHOTO_KEEP_DAYS = 30          # 按上传时间留 30 天
PHOTO_MAX_BYTES = 3 * 1024 * 1024


def photo_cutoff() -> str:
    return (datetime.now() - timedelta(days=PHOTO_KEEP_DAYS)).strftime("%Y-%m-%d %H:%M:%S")


def cleanup_photos() -> int:
    """上传满 30 天的照片：删文件、留一行记录，页面显示「照片已过期」。"""
    cut = photo_cutoff()
    n = 0
    with conn() as c:
        old = [dict(r) for r in c.execute(
            "SELECT id, path FROM photos WHERE expired=0 AND at < ?", (cut,))]
        for r in old:
            if r["path"]:
                try:
                    (PHOTO_DIR / r["path"]).unlink()
                except OSError:
                    pass
            c.execute("UPDATE photos SET path='', expired=1 WHERE id=?", (r["id"],))
            n += 1
    # 顺手清掉空目录
    try:
        for d in PHOTO_DIR.iterdir():
            if d.is_dir() and not any(d.iterdir()):
                d.rmdir()
    except OSError:
        pass
    return n


def photo_gc_loop():
    """每 6 小时扫一次，保证「30 天」不靠有人上传才执行。"""
    while True:
        time.sleep(6 * 3600)
        try:
            cleanup_photos()
        except Exception:
            pass


def save_photo(shop_id: str, unit_id: int, day: str, data_b64: str,
               w=None, h=None) -> dict:
    """一块码表一天可以拍多张，逐张追加。"""
    import base64
    sid = guard_shop(shop_id)
    with conn() as c:
        u = c.execute("SELECT id, shop_id, code FROM units WHERE id=?", (unit_id,)).fetchone()
    if not u or u["shop_id"] != sid:
        raise RuntimeError("机台不属于这家门店")
    try:
        raw = base64.b64decode(data_b64 or "", validate=False)
    except Exception:
        raise RuntimeError("照片数据坏了，重拍一张")
    if len(raw) < 1024:
        raise RuntimeError("照片太小，重拍一张")
    if len(raw) > PHOTO_MAX_BYTES:
        raise RuntimeError("照片太大，重拍一张")
    if raw[:2] != b"\xff\xd8":
        raise RuntimeError("只收 JPG")

    pid = uuid.uuid4().hex
    rel = "%s/%s_%s.jpg" % (day, u["code"] or u["id"], pid[:8])
    p = PHOTO_DIR / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(raw)

    ts = now()
    with conn() as c:
        c.execute("INSERT INTO photos(id,shop_id,unit_id,day,path,bytes,w,h,at,by,expired) "
                  "VALUES(?,?,?,?,?,?,?,?,?,?,0)",
                  (pid, sid, unit_id, day, rel, len(raw), w, h, ts,
                   (actor() or {}).get("name") or "?"))
        c.execute("INSERT INTO audit(ts,shop_id,day,unit_id,field,old_v,new_v,note) "
                  "VALUES(?,?,?,?,?,?,?,?)",
                  (ts, sid, day, unit_id, "photo", None, len(raw), "上传码表照片"))
    cleanup_photos()
    return {"id": pid, "bytes": len(raw), "at": ts}


def api_photos(q) -> dict:
    """某台机某天的全部照片（核对页点开看大图用）。"""
    sid = guard_shop((q.get("shop_id") or [""])[0])
    day = (q.get("day") or [""])[0]
    try:
        uid = int((q.get("unit_id") or ["0"])[0])
    except ValueError:
        uid = 0
    with conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, at, w, h, expired FROM photos "
            "WHERE shop_id=? AND unit_id=? AND day=? ORDER BY at", (sid, uid, day))]
    return {"photos": [{"id": r["id"], "at": r["at"],
                        "expired": bool(r["expired"])} for r in rows]}


def photo_bytes(photo_id: str) -> bytes:
    with conn() as c:
        r = c.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
    if not r:
        raise RuntimeError("照片不存在")
    guard_shop(r["shop_id"])
    if r["expired"] or not r["path"]:
        raise RuntimeError("照片已过期")
    p = PHOTO_DIR / r["path"]
    if not p.is_file():
        raise RuntimeError("照片已过期")
    return p.read_bytes()


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
                sub = " / ".join("%s %s" % (x["p"] or "未标", x["comm"]) for x in m["pcomms"])
                png = qr_label_png("%s/?c=%s" % (_qr_origin(), code), m["name"])
                body = png
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "public, max-age=86400, immutable")
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
            if p == "/api/photo":
                return self._send(200, photo_bytes((q.get("id") or [""])[0]), "image/jpeg")
            if p == "/api/photos":
                return self._send(200, api_photos(q))
            if p == "/api/bd/status":
                return self._send(200, api_bd_status(q))
            if p == "/api/manage/bd-sync/preview":
                sid = (q.get("shop_id") or [""])[0] or (resolve_shop(actor()) or "")
                days = int((q.get("days") or ["30"])[0])
                return self._send(200, bd_sync_preview(sid, days))
            if p == "/api/manage/devices":
                return self._send(200, api_manage_devices(q))
            if p == "/api/brand/list":
                return self._send(200, api_brand_list(q))
            if p == "/api/brand/flags":
                return self._send(200, api_brand_flags(q))
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
            if p == "/api/bd/login/start":
                return self._send(200, api_bd_start(b))
            if p == "/api/bd/login/poll":
                return self._send(200, api_bd_poll(b))
            if p == "/api/manage/brand/flags":
                return self._send(200, save_brand_flags(b))
            if p == "/api/manage/shop/provider":
                return self._send(200, api_shop_provider(b))
            if p == "/api/recon/reset":
                return self._send(200, api_recon_reset(b))
            if p == "/api/recon/freeze":
                return self._send(200, api_recon_freeze(b))
            if p == "/api/recon/note":
                return self._send(200, api_recon_note(b))
            if p == "/api/entry":
                return self._send(200, save_readings(b["shop_id"], b["day"], b.get("items") or []))
            if p == "/api/photo":
                return self._send(200, save_photo(b["shop_id"], int(b["unit_id"]), b["day"],
                                                  b.get("data") or "", b.get("w"), b.get("h")))
            if p == "/api/fetch":
                sid, day = b["shop_id"], b["day"]
                prov = (shop_row(sid) or {}).get("provider") or "baodian"
                if prov == "manual":
                    # 纯手工店没有卡头，不需要跟任何系统取数
                    return self._send(200, {"ok": True, "provider": "manual",
                                            "skipped": "纯手工门店，不用取数"})
                # 宝点上币只对宝点店做；鲸舰/世软走各自的机台汇总
                if prov not in ("jingjian", "shiruan"):
                    devices = baodian(sid).fetch_up(shop_of(shop_row(sid)), day)
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
                uid = int(b["unit_id"])
                with conn() as c:
                    c.execute("UPDATE units SET active=? WHERE id=?",
                              (0 if b.get("hide") else 1, uid))
                    if "shared" in b:
                        # 合并/拆分 = 改机台的码表数，不动卡头、不动机台
                        u = c.execute("SELECT machine_id FROM units WHERE id=?", (uid,)).fetchone()
                        if u and u["machine_id"]:
                            c.execute("UPDATE machines SET shared=? WHERE id=?",
                                      (1 if b.get("shared") else 0, u["machine_id"]))
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
            if p == "/api/manage/bd-sync/apply":
                sid = (b.get("shop_id") or "").strip() or (resolve_shop(actor()) or "")
                return self._send(200, bd_sync_apply(sid, b.get("items") or []))
            if p == "/api/manage/jj-comms/sync":
                sid = (b.get("shop_id") or "").strip() or (resolve_shop(actor()) or "")
                return self._send(200, sync_jingjian_comms(sid, int(b.get("days") or 7)))
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
            if p == "/api/manage/unit/retire":
                return self._send(200, retire_unit(int(b["id"]), b.get("day")))
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
            "SELECT s.id, s.name, COALESCE(s.provider,'baodian') AS provider, "
            "s.jj_base, s.jj_shop_id, COUNT(u.id) AS units FROM shops s "
            "LEFT JOIN units u ON u.shop_id = s.id AND u.active = 1 "
            "WHERE COALESCE(s.group_name,'') NOT IN (%s) OR u.id IS NOT NULL "
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


def _poss_from_map(pos: dict, comms) -> list:
    """单卡头的机器没 P 位时按 P1（2026-11-26）。

    """
    vals = [pos.get(c, "") for c in (comms or [])]
    if len(vals) == 1 and not vals[0]:
        return ["P1"]
    return vals

def _poss_one(cno: dict, comms_sorted) -> list:
    """单卡头的机器没录 P 位时按 P1 显示（2026-11-26 要求）。"""
    vals = [cno.get(x) or "" for x in (comms_sorted or [])]
    if len(vals) == 1 and not vals[0]:
        return ["P1"]
    return vals

def pcomms_of(all_comms) -> list:
    """把通讯ID列表变成 [{p: 'P1'..'Pn', comm}]。

    系统里填的位置（console_no）只用来排序，显示一律按顺序 P1..Pn：
    后台数据常年不改，出现三个 2P、或者一个都没标，直接照抄会乱（2026-11-26 用户口径）。
    """
    ids = [str(c) for c in (all_comms or [])]
    if not ids:
        return []
    with conn() as c:
        rows = c.execute("SELECT comm_id, console_no FROM comms WHERE comm_id IN (%s)"
                         % ",".join("?" * len(ids)), ids).fetchall()
    pos = {str(r["comm_id"]): (r["console_no"] or "") for r in rows}
    ordered = sorted(ids, key=lambda cid: (pos.get(cid) or "~", cid))
    return [{"p": "P%d" % (i + 1), "comm": cid} for i, cid in enumerate(ordered)]


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
        base = re.sub(r"\s*\(\d+\)\s*$", "", re.sub(r"\s+P\d+$", "", re.sub(r"\s+\d{5,}$", "", u["name"]))).strip() or u["name"]
        u["comms"] = comms.get(u["id"], [])
        groups.setdefault((u["kind"] or "pusher", base), []).append(u)
    out = []
    for (kind, base), us in groups.items():
        # P 位按卡头编：这台机的所有通讯ID排序后依次 P1..Pn。
        # 共用码表的机台（4 个卡头喂一块表）也是 P1..P4，只是读数只有一份。
        all_comms = sorted(cid for u in us for cid in (u.get("comms") or []))
        out.append({"kind": kind, "name": base, "units": us,
                    "code": us[0]["code"], "count": len(us),
                    "pcomms": pcomms_of(all_comms)})
    return out


_BD_POS_CACHE: dict = {}          # shop_id -> (取的时刻, {comm: P位})，缓存 6 小时


def bd_pos_matrix(shop_id: str, days: int = 30) -> dict:
    """从宝点「下币汇总」收集 卡头 → P 位（consoleNo）。

    只有下币记录里带 consoleNo，且只报当天有下币的设备，
    所以扫最近 N 天，取每个卡头第一次出现的值。结果缓存 6 小时（扫一次约 12 秒）。
    """
    hit = _BD_POS_CACHE.get(shop_id)
    if hit and time.time() - hit[0] < 6 * 3600:
        return dict(hit[1])
    shop = shop_of(shop_row(shop_id))
    bd = baodian(shop_id)
    pos: dict = {}
    for i in range(1, max(1, days) + 1):
        day = (date.today() - timedelta(days=i)).isoformat()
        try:
            rows = bd.fetch_down(shop, day)
        except Exception:
            continue
        for r in rows:
            cid = str(r.get("commId") or "")
            cn = (r.get("consoleNo") or "").strip()
            if cid and cn:
                pos.setdefault(cid, cn)
        time.sleep(0.2)
    _BD_POS_CACHE[shop_id] = (time.time(), dict(pos))
    return pos


def bd_pos_deep(shop_id: str, need: set, days: int = 180) -> dict:
    """近 30 天扫不到 P 位的卡头，再往前翻到 days 天补（机器停开一段时间很常见）。

    只在还有卡头没 P 位时调用；已经扫到的值不动。
    """
    hit = _BD_POS_CACHE.get(shop_id)
    pos = dict(hit[1]) if hit else {}
    missing = {c for c in (need or set()) if str(c) not in pos}
    if not missing:
        return pos
    shop = shop_of(shop_row(shop_id))
    bd = baodian(shop_id)
    for i in range(31, max(31, days) + 1):
        day = (date.today() - timedelta(days=i)).isoformat()
        try:
            rows = bd.fetch_down(shop, day)
        except Exception:
            continue
        for r in rows:
            cid = str(r.get("commId") or "")
            cn = (r.get("consoleNo") or "").strip()
            if cid and cn:
                pos.setdefault(cid, cn)
        if missing <= set(pos):
            break
        time.sleep(0.2)
    _BD_POS_CACHE[shop_id] = (time.time(), dict(pos))
    return pos


def sync_pos(shop_id: str, days: int = 30) -> dict:
    """只同步 P 位（老按钮保留）：宝点怎么设，这里就怎么设。"""
    pos = bd_pos_matrix(shop_id, days)
    updated = 0
    ts = now()
    with conn() as c:
        for cid, cn in pos.items():
            cur = c.execute("SELECT console_no FROM comms WHERE comm_id=?", (cid,)).fetchone()
            if not cur:
                continue
            if (cur["console_no"] or "") == cn:
                continue
            c.execute("UPDATE comms SET console_no=?, updated_at=? WHERE comm_id=?",
                      (cn, ts, cid))
            updated += 1
        unknown = c.execute(
            "SELECT COUNT(*) FROM comms m JOIN units u ON u.id=m.unit_id "
            "WHERE u.shop_id=? AND (m.console_no IS NULL OR m.console_no='')",
            (shop_id,)).fetchone()[0]
    return {"ok": True, "scanned": len(pos), "updated": updated, "unknown": unknown}


# 宝点「项目分组」→ 本地两类码表（2026-11-26 用户定）
# 红票弹珠机：2026-11-26 用户要求收进来（鄂州 4 台西游弹珠机挂在这组）
BD_PACHINKO_TEAMS = ("弹珠机", "出珠弹珠机", "蓝票弹珠机", "红票弹珠机")
# 只认「推币机类」这个大类的子分组，别的一概不进码表（2026-11-26 用户口径）
BD_PUSHER_TEAMS = ("推币机", "其他推币机", "屏幕类", "连排推币类", "中岛推币类")
# 不做关键词兜底：名字像不等于就是，误判会把彩票机、儿童屏幕机拉进码表
TEAM_KIND_KW = ()


def _bd_team(raw) -> str:
    """projectTeamName 是 JSON 数组字符串，取第一个。"""
    try:
        v = json.loads(raw or "[]")
        if isinstance(v, list) and v:
            return str(v[0])
        if isinstance(v, str):
            return v
    except Exception:
        pass
    return str(raw or "")


def bd_device_list(shop_id: str) -> list:
    """宝点设备清单，只留要写码表的两类（弹珠机 / 推币机）。"""
    shop = shop_of(shop_row(shop_id))
    bd = baodian(shop_id)
    out = []
    unknown = {}
    page = 0
    while True:
        j = bd.post("/terminals/terminal/getlist",
                    {"page": page, "limit": 300, "filters": [], "sort": "desc",
                     "sortField": "deviceOrderBy"}, shop)
        rows = j.get("listData") or []
        for x in rows:
            team = _bd_team(x.get("projectTeamName"))
            if team in BD_PACHINKO_TEAMS:
                kind = "pachinko"
            elif team in BD_PUSHER_TEAMS:
                kind = "pusher"
            else:
                unknown.setdefault(team or "(没分组)", []).append(
                    (x.get("deviceName") or "").strip())
                continue
            comms = [c.strip() for c in str(x.get("commIds") or "").split(",") if c.strip()]
            # 脏数据：宝点里有 commIds 是 "0" 的（彩虹王国），不是真卡头，跳过
            comms = [c for c in comms if c.isdigit() and c != "0"]
            if not comms:
                continue
            out.append({"bd_id": x.get("id"), "name": (x.get("deviceName") or "").strip(),
                        "kind": kind, "team": team, "comms": comms,
                        "enabled": bool(x.get("isEnabled"))})
        page += 1
        if page >= int(j.get("totalPage") or 1) or not rows:
            break
        time.sleep(0.2)
    bd_device_list.unknown = unknown      # 给预览用：哪些分组没认出来
    return out


def bd_sync_preview(shop_id: str, days: int = 30) -> dict:
    """拉宝点机台清单 + P位，和本地比对，给出一张待同步表。"""
    shop_id = guard_shop(shop_id)
    devices = bd_device_list(shop_id)
    pos = bd_pos_matrix(shop_id, days)
    # 近 30 天没下币的机台（长期停开）再往前翻 180 天，别让它们一直显示「未标」
    need = {c for d in devices for c in d["comms"]}
    if need - set(pos):
        pos = bd_pos_deep(shop_id, need)
    with conn() as c:
        units = c.execute("SELECT id,name,kind,locked FROM units WHERE shop_id=? AND active=1",
                          (shop_id,)).fetchall()
        cmap = {}
        for r in c.execute("SELECT m.comm_id, m.unit_id FROM comms m JOIN units u ON u.id=m.unit_id "
                           "WHERE u.shop_id=?", (shop_id,)):
            cmap.setdefault(r["unit_id"], set()).add(str(r["comm_id"]))

    rows = []
    for d in devices:
        hit = next((u for u in units if cmap.get(u["id"], set()) & set(d["comms"])), None)
        if not hit:
            hit = next((u for u in units if (u["name"] or "").strip() == d["name"]), None)
        local = None
        status = "new"
        if hit:
            uc = sorted(cmap.get(hit["id"], set()))
            same = ((hit["name"] or "").strip() == d["name"]
                    and set(uc) == set(d["comms"]))
            status = "same" if same else "update"
            local = {"unit_id": hit["id"], "name": hit["name"], "kind": hit["kind"],
                     "comms": uc, "locked": bool(hit["locked"])}
        rows.append({"name": d["name"], "kind": d["kind"], "team": d["team"],
                     "comms": d["comms"],
                     "pos": _poss_from_map(pos, d["comms"]),
                     "status": status, "local": local,
                     "local_name": (local or {}).get("name"),
                     "local_comms": (local or {}).get("comms") or [],
                     "merge": bool(local and len(d["comms"]) > len(local.get("comms") or []))})

    bd_comms = {c for d in devices for c in d["comms"]}
    orphan = []
    for u in units:
        uc = cmap.get(u["id"], set())
        if uc and not (uc & bd_comms):
            orphan.append({"unit_id": u["id"], "name": u["name"], "kind": u["kind"],
                           "comms": sorted(uc)})
    unknown = getattr(bd_device_list, "unknown", {}) or {}
    return {"ok": True, "rows": rows, "orphan": orphan,
            "unknown": [{"team": k, "machines": v} for k, v in sorted(unknown.items())],
            "pos_have": len(pos), "devices": len(devices), "days": days}


def bd_sync_apply(shop_id: str, items: list) -> dict:
    """把用户勾选的宝点机台写进本地：名称/类型/卡头/P位全部以宝点为准。"""
    shop_id = guard_shop(shop_id)
    created = updated = pos_n = merged = 0
    for it in items or []:
        name = (it.get("name") or "").strip()
        kind = it.get("kind")
        comms = [str(x).strip() for x in (it.get("comms") or []) if str(x).strip()]
        if not name or kind not in ("pusher", "pachinko") or not comms:
            continue
        uid = it.get("unit_id")
        # 这些卡头现在挂在哪些机台上：合并后如果它们空了，停用掉，不留空壳
        with conn() as c:
            marks = ",".join("?" * len(comms))
            owners = {r["unit_id"] for r in c.execute(
                "SELECT unit_id FROM comms WHERE comm_id IN (%s)" % marks, comms)}
        # 默认一台卡头一块码表：宝点一个机台带 4 个卡头，同步下来是 4 行，不自动并成「共用码表」
        targets = set()
        for i, cid in enumerate(comms):
            # 拆开的几块仍归同一台机：第一块用原名，其余自动加 (2)(3)(4)，
            # 这样设置页还是一行一台机、显示 N 个卡头，只是不再是「共用码表」
            with conn() as c:
                raw_name = name if i == 0 else "%s P%d" % (name, i + 1)
                sub = unique_unit_name(c, shop_id, raw_name, skip_id=(uid if i == 0 else None))
            res = save_unit({"shop_id": shop_id, "id": (uid if i == 0 else None),
                             "name": sub, "kind": kind, "comms": [cid], "shared": False})
            targets.add(res["id"])
            if i == 0 and uid:
                updated += 1
            else:
                created += 1
        with conn() as c:
            for other in owners - targets:
                left = c.execute("SELECT COUNT(*) FROM comms WHERE unit_id=?",
                                 (other,)).fetchone()[0]
                if not left:
                    c.execute("UPDATE units SET active=0, locked=1 WHERE id=?", (other,))
                    merged += 1
        pos = it.get("pos") or []
        with conn() as c:
            for cid, p in zip(comms, pos):
                if p:
                    c.execute("UPDATE comms SET console_no=?, updated_at=? WHERE comm_id=?",
                              (p, now(), cid))
                    pos_n += 1
    return {"ok": True, "created": created, "updated": updated, "pos": pos_n, "merged": merged}



QR_DESIGN = 3   # 贴纸版式版本，改一次 +1，图片 URL 跟着变，避免浏览器用旧缓存

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


def qr_label_png(url: str, title: str) -> bytes:
    """一张贴纸（白底）：机台名 + 二维码 + 「码表二维码」。"""
    import qrcode
    from PIL import Image, ImageDraw

    qr = qrcode.QRCode(border=1, box_size=10,
                       error_correction=qrcode.constants.ERROR_CORRECT_M)
    qr.add_data(url)
    qr.make(fit=True)
    qimg = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    qw = qimg.height

    W = 660
    pad_top, gap1, gap2, tail = 40, 30, 30, 40
    H = pad_top + 50 + gap1 + qw + gap2 + 40 + tail
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W - 1, H - 1], outline=(226, 228, 232), width=2)

    f_name = _qr_font(44, bold=True)
    f_cap = _qr_font(26)

    def center(text, f, y, fill=(16, 16, 18)):
        w = d.textlength(text, font=f)
        d.text(((W - w) / 2, y), text, fill=fill, font=f)

    center(title, f_name, pad_top)
    img.paste(qimg, ((W - qw) // 2, pad_top + 50 + gap1))
    center("码表二维码", f_cap, pad_top + 50 + gap1 + qw + gap2, (110, 110, 118))

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
            sub = " / ".join("%s %s" % (x["p"] or "未标", x["comm"]) for x in m["pcomms"])
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
        sub = " / ".join("%s %s" % (x["p"] or "未标", x["comm"]) for x in m["pcomms"])
        cards.append(
            '<label class="qrc">'
            '<input type="checkbox" class="qrc__cb" value="%s">'
            '<img src="qr/img?c=%s&d=%s" alt="%s" loading="lazy">'
            '<span class="qrc__t">%s</span>'
            '<span class="qrc__s">%s</span>'
            '</label>' % (m["code"], m["code"], QR_DESIGN, m["name"], m["name"], sub))
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
            r = c.execute("SELECT id,name,COALESCE(provider,'baodian') AS provider "
                          "FROM shops WHERE id=?", (own,)).fetchone()
            shop = dict(r) if r else None
    return {
        "type": a.get("type"),
        "name": a.get("name"),
        "cwsj_name": a.get("name"),
        "shop": shop,
        "bound": bool(own) if a.get("type") == "store" else True,
    }


# 弹珠机按品牌选一种玩法，玩法决定抄哪几个数
PACHINKO_MODES = [
    ("swipe_bead_card", "刷卡出珠出卡", ["in_r", "p_tokens", "p_out", "p_cards"]),
    ("swipe_card", "刷卡出卡", ["in_r", "p_cards"]),
    ("bead_card", "投珠出卡", ["p_tokens", "p_cards"]),
    ("bead_out_card", "投珠出珠出卡", ["p_tokens", "p_out", "p_cards"]),
]
MODE_FIELDS = {k: v for k, _, v in PACHINKO_MODES}
DEFAULT_MODE = "swipe_bead_card"


def brand_mode(brand: str) -> str:
    if not brand:
        return DEFAULT_MODE
    with conn() as c:
        r = c.execute("SELECT flags FROM brand_flags WHERE brand=?", (brand,)).fetchone()
    if r and r["flags"]:
        try:
            m = (json.loads(r["flags"]) or {}).get("mode")
            if m in MODE_FIELDS:
                return m
        except Exception:
            pass
    return DEFAULT_MODE


_BRAND_TAILS = ("超级弹珠台", "弹珠台", "弹珠机", "多珠机", "推币机")


def unit_brand(name: str) -> str:
    """从机台名剥出机台品牌：塔兰弹珠机-001 → 塔兰。"""
    s0 = re.sub(r"\s+\d{5,}$", "", name or "").strip()
    s1 = re.sub(r"[-_ ]*\d+$", "", s0).strip()
    s1 = re.sub(r"\d+号$", "", s1).strip()
    s1 = re.sub(r"\d+人位$", "", s1).strip()
    for t in _BRAND_TAILS:
        if s1 == t:
            return t
        if s1.endswith(t):
            head = s1[: -len(t)].strip()
            if head:
                return head
    return s1 or s0 or (name or "")


def brand_of(u) -> str:
    """这台机属于哪个品牌：库里存的优先，没存才从机台名推。"""
    return (u.get("brand") or "").strip() or unit_brand((u.get("name") or ""))


def brand_list(shop_id: str = "") -> list:
    """当前有哪些机台品牌，各多少台，各自的设置。"""
    with conn() as c:
        if shop_id:
            rs = c.execute("SELECT name, brand FROM units WHERE kind='pachinko' AND active=1 "
                           "AND shop_id=?", (shop_id,)).fetchall()
        else:
            rs = c.execute("SELECT name, brand FROM units WHERE kind='pachinko' "
                           "AND active=1").fetchall()
    cnt = {}
    for r in rs:
        b = brand_of(dict(r))
        cnt[b] = cnt.get(b, 0) + 1
    with conn() as c:
        alias = {r["key"]: r["label"] for r in c.execute("SELECT key,label FROM brand_alias")}
    out = []
    for b in sorted(cnt):
        fl = brand_flags(b)
        out.append({"brand": b, "label": alias.get(b) or b, "units": cnt[b], **fl})
    return out


DEFAULT_UNIT = "coin"


def brand_unit(brand: str) -> str:
    """码表的「投币」数一律记币，投珠数另有一列，不再有「记珠」这种设置。"""
    return DEFAULT_UNIT


def _brand_json(brand: str) -> dict:
    if not brand:
        return {}
    with conn() as c:
        r = c.execute("SELECT flags FROM brand_flags WHERE brand=?", (brand,)).fetchone()
    if r and r["flags"]:
        try:
            return json.loads(r["flags"]) or {}
        except Exception:
            pass
    return {}


def brand_out_incl(brand: str) -> bool:
    """总出珠里是否含「投币换出来的首出珠」。默认含（老机器都是含）。"""
    v = _brand_json(brand).get("out_incl")
    return True if v is None else bool(v)


def brand_per_coin(brand: str):
    """一币几珠（老位置，兼容）；现在主配置在门店参数 shops.beads_per_coin。"""
    v = _brand_json(brand).get("per_coin")
    try:
        return float(v) if v else None
    except Exception:
        return None


def brand_flags(brand: str) -> dict:
    m = brand_mode(brand)
    return {"mode": m, "fields": MODE_FIELDS[m], "unit": DEFAULT_UNIT,
            "out_incl": brand_out_incl(brand), "per_coin": brand_per_coin(brand)}


def api_bd_status(q) -> dict:
    sid = one(q.get("shop_id"))
    if not sid:
        try:
            sid = resolve_shop(actor()) or ""
        except Exception:
            sid = ""
    return {"ok": True, **bd_alive(sid)}


def api_bd_start(b: dict) -> dict:
    """生成一个宝点登录二维码，扫完会另存成码表专用会话。"""
    sid = guard_shop(one(b.get("shop_id")) or (resolve_shop(actor()) or ""))
    r = bd_call("/api/start", {"sys": "baodian", "wait": 2, "key": "meter-" + sid})
    if not r.get("ok"):
        raise RuntimeError(r.get("error") or "生成二维码失败")
    return {"sid": r.get("sid"), "png": r.get("png") or "",
            "url": r.get("url") or "", "state": r.get("state") or ""}


def api_bd_poll(b: dict) -> dict:
    sid = (b.get("sid") or "").strip()
    if not sid:
        raise RuntimeError("缺 sid")
    shop_id = guard_shop(one(b.get("shop_id")) or (resolve_shop(actor()) or ""))
    r = bd_call("/api/status", {"sid": sid, "key": "meter-" + shop_id})
    out = {"ok": bool(r.get("ok")), "state": r.get("state") or "",
           "error": r.get("error") or "", "err": r.get("err") or ""}
    if r.get("state") == "ok" and out["ok"]:
        reset_baodian()
        out.update(bd_alive(shop_id))
    return out


def api_manage_devices(q) -> dict:
    """拉这家店最近几天的宝点设备，标出哪些已经加过了。"""
    sid = guard_shop((q.get("shop_id") or [""])[0] or (resolve_shop(actor()) or ""))
    days = int((q.get("days") or ["3"])[0])
    shop = shop_of(shop_row(sid))
    bd = baodian(sid)
    seen: dict = {}
    for i in range(1, days + 1):
        day = (date.today() - timedelta(days=i)).isoformat()
        try:
            devs = bd.fetch_up(shop, day)
        except Exception:
            continue
        for d in devs:
            cid = str(d.get("commId") or "")
            if not cid or cid in seen:
                continue
            seen[cid] = {
                "comm": cid,
                "name": d.get("deviceName") or "未命名",
                "team": d.get("projectTeamName") or "",
                "kind": kind_of(d.get("projectTeamName")) or "",
                "day": day,
            }
    with conn() as c:
        have = {str(r["comm_id"]) for r in c.execute(
            "SELECT m.comm_id FROM comms m JOIN units u ON u.id=m.unit_id "
            "WHERE u.shop_id=?", (sid,))}
        brands = [r["brand"] for r in c.execute(
            "SELECT DISTINCT brand FROM units WHERE shop_id=? AND brand IS NOT NULL "
            "AND brand<>'' ORDER BY brand", (sid,))]
    rows = list(seen.values())
    for v in rows:
        v["added"] = v["comm"] in have
    rows.sort(key=lambda x: (x["kind"], x["name"], x["comm"]))
    return {"rows": rows, "brands": brands}


def api_brand_list(q) -> dict:
    sid = (q.get("shop_id") or [""])[0]
    sh = shop_row(sid) or {}
    return {"rows": brand_list(sid),
            "modes": [[k, l] for k, l, _ in PACHINKO_MODES],
            "shop": {"id": sid, "name": sh.get("name"),
                     "bead_value": sh.get("bead_value"),
                     "card_value": sh.get("card_value"),
                     "beads_per_coin": sh.get("beads_per_coin")}}


def api_brand_flags(q) -> dict:
    brand = (q.get("brand") or [""])[0]
    return {"brand": brand, "flags": brand_flags(brand),
            "modes": [[k, label] for k, label, _ in PACHINKO_MODES]}


def save_brand_flags(b: dict) -> dict:
    brand = (b.get("brand") or "").strip()
    if not brand:
        raise RuntimeError("缺品牌名")
    # 门店只能改自己店里有的品牌
    if is_store():
        own = resolve_shop(actor())
        rows = [dict(r) for r in conn().execute(
            "SELECT name, brand FROM units WHERE shop_id=? AND kind='pachinko' AND active=1",
            (own,))]
        if brand not in {brand_of(x) for x in rows}:
            raise AuthError("这家店没有「%s」这个品牌，只能改本店有的品牌" % brand)
    label = (b.get("label") or "").strip()
    if label and label != brand:
        with conn() as c:
            c.execute("INSERT INTO brand_alias(key,label) VALUES(?,?) "
                      "ON CONFLICT(key) DO UPDATE SET label=excluded.label", (brand, label))
    elif label == brand:
        with conn() as c:
            c.execute("DELETE FROM brand_alias WHERE key=?", (brand,))
    mode = b.get("mode") or DEFAULT_MODE
    if mode not in MODE_FIELDS:
        raise RuntimeError("没有这种玩法")
    oi = b.get("out_incl")
    out_incl = True if oi in (None, "") else str(oi).lower() not in ("0", "false", "no", "off")
    per = b.get("per_coin")          # 兼容老调用，前端已改到门店参数里填
    try:
        per = float(per) if per not in (None, "", 0, "0") else None
    except Exception:
        per = None
    with conn() as c:
        c.execute("INSERT INTO brand_flags(brand, flags) VALUES(?,?) "
                  "ON CONFLICT(brand) DO UPDATE SET flags=excluded.flags",
                  (brand, json.dumps({"mode": mode, "out_incl": out_incl, "per_coin": per})))
    return {"ok": True, "brand": brand, "flags": brand_flags(brand)}


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
    # 隐藏集团只是不给看空壳店；一旦这家店有了机台，就该出现在列表里
    shops = [dict(r) for r in conn().execute(
        "SELECT s.* FROM shops s WHERE COALESCE(s.group_name,'') NOT IN (%s) "
        "OR EXISTS (SELECT 1 FROM units u WHERE u.shop_id=s.id AND u.active=1) "
        "ORDER BY s.group_name, s.name" % ",".join("?" * len(HIDDEN_GROUPS)),
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
        "max_day": date.today().isoformat(),
        "edit_day": edit_day(),
        "scope_class": SCOPE_CLASS,
        "rate": RATE,
        "units_confirmed": (shop_row(sid) or {}).get("units_confirmed") if sid else None,
        "brand": (shop_row(sid) or {}).get("group_name") if sid else None,
        "brand_flags": brand_flags((shop_row(sid) or {}).get("group_name") or "") if sid else None,
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
    out = []
    seen_m = set()
    with conn() as c:
        us = [dict(r) for r in c.execute(
            "SELECT * FROM units WHERE shop_id=? ORDER BY active DESC, name", (shop_id,))]
        for u in us:
            u["comms"] = [dict(r) for r in c.execute(
                "SELECT comm_id, console_no FROM comms WHERE unit_id=? ORDER BY comm_id", (u["id"],))]
            m = c.execute("SELECT * FROM machines WHERE id=?", (u.get("machine_id") or 0,)).fetchone()
            if m:
                u["machine_id"] = m["id"]
                u["machine_name"] = m["name"]
                u["machine_shared"] = m["shared"]
                u["machine_code"] = m["code"]
                u["machine_slots"] = c.execute(
                    "SELECT COUNT(*) FROM units WHERE machine_id=?", (m["id"],)).fetchone()[0]
                # 共用一块码表的机台只露一行，卡头显示整台机的（和以前的页面一致）
                if m["shared"]:
                    if m["id"] in seen_m:
                        continue
                    seen_m.add(m["id"])
                    u["shared"] = 1
                    u["comms"] = [{"comm_id": x, "console_no": None}
                                  for x in machine_comms(m["id"])]
                    u["name"] = m["name"]
            out.append(u)
    return out


def manage_machines(shop_id: str) -> list:
    """按机台名归组：一台机一个框，框里是它的卡头（通讯ID）"""
    import re as _re
    shop_id = guard_shop(shop_id)
    us = [u for u in manage_units(shop_id) if u.get("active")]
    groups: dict = {}
    for u in us:
        if u.get("machine_id"):
            key = ("m", u["machine_id"], u.get("machine_name") or u["name"])
        else:
            base = _re.sub(r"\s*\(\d+\)\s*$", "", _re.sub(r"\s+\d{5,}$", "", u["name"])).strip() or u["name"]
            key = ("n", base, base)
        groups.setdefault(key, []).append(u)
    out = []
    for (_, _, base), units in groups.items():
        units.sort(key=lambda x: (x.get("pos") or 1, x["name"]))
        comms = [c["comm_id"] for u in units for c in u["comms"]]
        all_comms = sorted(str(c) for c in comms)
        out.append({
            "key": base,
            "kind": (units[0].get("kind") or "pusher"),
            "brand": brand_of(units[0]) if (units[0].get("kind") == "pachinko") else None,
            "units": units,
            "comms": comms,
            "pcomms": pcomms_of(all_comms),
            "heads": len(comms),
            "merged": len(units) == 1 and len(comms) > 1,
            "shared": any(bool(u["shared"]) for u in units),
            "active": any(bool(u["active"]) for u in units),
        })
    order = {k: i for i, (k, _) in enumerate(KINDS)}
    out.sort(key=lambda g: (order.get(g["kind"], 9), not g["active"], g["key"]))
    return out


def save_shop_values(b: dict) -> dict:
    """弹珠机的珠值 / 卡值 / 一币几珠，按店配置；留空就是没配。"""
    shop_id = guard_shop(b["shop_id"])

    def num(v):
        try:
            f = float(v)
            return f if f > 0 else None
        except Exception:
            return None

    with conn() as c:
        c.execute("UPDATE shops SET bead_value=?, card_value=?, beads_per_coin=? WHERE id=?",
                  (num(b.get("bead_value")), num(b.get("card_value")),
                   num(b.get("beads_per_coin")), shop_id))
    return {"ok": True, "bead_value": num(b.get("bead_value")),
            "card_value": num(b.get("card_value")),
            "beads_per_coin": num(b.get("beads_per_coin"))}


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


def ensure_machine(c, shop_id: str, name: str, kind: str = "pusher") -> int:
    """按机台名拿机台 id，没有就建。"""
    base = machine_base(name)
    c.execute("INSERT OR IGNORE INTO machines(shop_id,name,kind,shared,active) VALUES(?,?,?,0,1)",
              (shop_id, base, kind))
    return c.execute("SELECT id FROM machines WHERE shop_id=? AND name=?",
                     (shop_id, base)).fetchone()["id"]


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
        mid = ensure_machine(c, shop_id, name, kind)
        c.execute("UPDATE machines SET shared=?, active=1, kind=?, brand=? WHERE id=?",
                  (shared, kind, (b.get("brand") or "").strip() or None, mid))
        base = c.execute("SELECT name FROM machines WHERE id=?", (mid,)).fetchone()["name"]
        # this machine 现有的码表位
        slots = [dict(r) for r in c.execute(
            "SELECT * FROM units WHERE machine_id=? ORDER BY pos, id", (mid,))]
        keep = set(comms)
        today = date.today().isoformat()
        # 不在新清单里的卡头：解绑；位子留着但停用
        for u in slots:
            for r in c.execute("SELECT comm_id FROM comms WHERE unit_id=?", (u["id"],)):
                if r["comm_id"] not in keep:
                    c.execute("DELETE FROM comms WHERE comm_id=?", (r["comm_id"],))
                    c.execute("UPDATE comm_owner SET to_day=? WHERE comm_id=? AND to_day IS NULL",
                              (_tomorrow(today), r["comm_id"]))
        for i, cid in enumerate(comms, start=1):
            u = next((x for x in slots if (x["pos"] or 0) == i), None)
            if u is None:
                nm = base if i == 1 else "%s P%d" % (base, i)
                c.execute("INSERT INTO units(shop_id,name,team,shared,active,locked,kind,pos,"
                          "machine_id,from_day,brand) VALUES(?,?,?,0,1,1,?,?,?,?,?) "
                          "ON CONFLICT(shop_id,name) DO UPDATE SET active=1, locked=1, "
                          "machine_id=excluded.machine_id, pos=excluded.pos",
                          (shop_id, nm, b.get("team"), kind, i, mid, today,
                           (b.get("brand") or "").strip() or None))
                u = c.execute("SELECT * FROM units WHERE shop_id=? AND name=?",
                              (shop_id, nm)).fetchone()
                slots.append(dict(u))
            c.execute("UPDATE units SET active=1, locked=1, machine_id=?, pos=? WHERE id=?",
                      (mid, i, u["id"]))
            c.execute("INSERT INTO comms(comm_id,unit_id,console_no,team,device,updated_at) "
                      "VALUES(?,?,NULL,NULL,NULL,?) ON CONFLICT(comm_id) DO UPDATE SET "
                      "unit_id=excluded.unit_id, updated_at=excluded.updated_at",
                      (cid, u["id"], now()))
            comm_owner_set(c, cid, u["id"], shop_id, today)
        # 多出来的位子停用（保留历史读数）
        for u in slots:
            n = c.execute("SELECT COUNT(*) FROM comms WHERE unit_id=?", (u["id"],)).fetchone()[0]
            if not n:
                c.execute("UPDATE units SET active=0 WHERE id=?", (u["id"],))
    return {"ok": True, "machine_id": mid, "slots": len(comms)}


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
            # 归属历史跟着改，否则核对页还按旧机台把几个卡头算成一台（2026-11-26）
            comm_owner_move(c, cid, keep["id"], shop_id, date.today().isoformat())
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
            c.execute("UPDATE units SET name=?, brand=?, shared=?, locked=1, active=1 WHERE id=?",
                      (name, (b.get("brand") or "").strip() or None,
                       1 if len(comms) > 1 else 0, keep))
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
            label = unique_unit_name(c, shop_id, name)
            uid = by_comm.get(cid)
            if uid:
                c.execute("UPDATE units SET name=?, brand=?, shared=0, locked=1, active=1 "
                          "WHERE id=?",
                          (label, (b.get("brand") or "").strip() or None, uid))
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
        shop_id = u["shop_id"]
        comms = [r["comm_id"] for r in c.execute(
            "SELECT comm_id FROM comms WHERE unit_id=? ORDER BY comm_id", (uid,))]
        if len(comms) < 2:
            raise RuntimeError("只有一个卡头，不用拆")
        base = _re.sub(r"\s*\(\d+\)\s*$", "", _re.sub(r"\s+\d{5,}$", "", u["name"])).strip() or u["name"]
        first = unique_unit_name(c, shop_id, base, skip_id=uid)
        c.execute("UPDATE units SET name=?, shared=0, locked=0 WHERE id=?", (first, uid))
        for cid in comms[1:]:
            label = unique_unit_name(c, shop_id, base)
            c.execute("INSERT INTO units(shop_id,name,team,shared,active,kind,brand) "
                      "VALUES(?,?,?,0,1,?,?) "
                      "ON CONFLICT(shop_id,name) DO UPDATE SET active=1, shared=0, "
                      "kind=excluded.kind, brand=excluded.brand",
                      (shop_id, label, u["team"], u["kind"], u["brand"]))
            nid = c.execute("SELECT id FROM units WHERE shop_id=? AND name=?",
                            (u["shop_id"], label)).fetchone()["id"]
            c.execute("UPDATE comms SET unit_id=? WHERE comm_id=?", (nid, cid))
            # 拆出去的卡头归属也要跟着走，否则核对页仍按原机台合并显示（2026-11-26）
            comm_owner_move(c, cid, nid, shop_id, date.today().isoformat())
    return {"ok": True, "units": len(comms)}


def retire_unit(uid: int, day: str | None = None) -> dict:
    """机台卖掉/撤掉：停用并记下停用日，历史那天仍能查到。"""
    with conn() as c:
        row = c.execute("SELECT shop_id FROM units WHERE id=?", (uid,)).fetchone()
        if row:
            guard_shop(row["shop_id"])
        d = day or date.today().isoformat()
        # 停用当天它还在跑，从次日起才不再出现
        stop = _tomorrow(d)
        c.execute("UPDATE units SET active=0, to_day=? WHERE id=?", (stop, uid))
        c.execute("UPDATE comm_owner SET to_day=? WHERE unit_id=? AND (to_day IS NULL OR to_day>?)",
                  (stop, uid, stop))
    return {"ok": True, "to_day": d}


def remove_unit(uid: int) -> dict:
    with conn() as c:
        row = c.execute("SELECT shop_id FROM units WHERE id=?", (uid,)).fetchone()
        if row:
            guard_shop(row["shop_id"])
        today = date.today().isoformat()
        # 卡头归属封口，别在历史里继续挂着
        c.execute("UPDATE comm_owner SET to_day=? WHERE unit_id=? AND to_day IS NULL",
                  (today, uid))
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
    out["max_day"] = date.today().isoformat()
    return out


PROVIDERS = [("baodian", "宝点"), ("jingjian", "鲸舰"), ("shiruan", "世软"), ("manual", "纯手工")]


def api_shop_provider(b) -> dict:
    """改门店取数方式（只有总部能改）。"""
    require_hq()
    sid = guard_shop(b["shop_id"])
    prov = (b.get("provider") or "").strip()
    if prov not in dict(PROVIDERS):
        raise RuntimeError("取数方式只能是 baodian / jingjian / shiruan / manual")
    with conn() as c:
        c.execute("UPDATE shops SET provider=? WHERE id=?", (prov, sid))
    return {"ok": True, "provider": prov}


def api_recon_reset(b) -> dict:
    sid = guard_shop(b["shop_id"])
    day = (b.get("day") or "").strip()
    uid = int(b["unit_id"])
    if not day:
        raise RuntimeError("缺日期")
    # 只有总部能动
    if (actor() or {}).get("type") != "hq":
        raise AuthError("只有总部能标记码表归零")
    return unit_reset_set(uid, day, bool(b.get("on", True)))


def api_recon_freeze(b) -> dict:
    require_hq()
    sid = guard_shop(b["shop_id"])
    day = (b.get("day") or "").strip()
    if not day:
        raise RuntimeError("缺日期")
    if b.get("unfreeze"):
        return recon_unfreeze(sid, day)
    return recon_freeze(sid, day)


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
                "kind": row.get("kind") or kind,
                "name": row["name"],
                "comms": row.get("comms") or [],   # 卡头ID：共用码表时是多个
                "poss": row.get("poss") or [],     # 对应的 P 位，和 comms 一一对齐
                "fields": row.get("fields"),   # 页面按它决定显示哪几列，导出也照它
                "p": row.get("p"),             # 珠/出珠/出卡/中奖出珠都在这里
                "meter_in": row["meter_in"],
                "meter_out": row["meter_out"],
                "up": row["up"],
                "down": row["down"],
                "head": row["head"],
                "meter": row["meter"],
                "diff": row["diff"],
                "rate": row["rate"],
                "status": row["status"],
                "photos": row.get("photos", 0),
                "photos_expired": row.get("photos_expired", False),
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
    try:
        n = cleanup_photos()
        if n:
            print(f"清理过期照片 {n} 张")
    except Exception as e:
        print("清理照片失败：", e)
    threading.Thread(target=photo_gc_loop, daemon=True).start()
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
