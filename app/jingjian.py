# -*- coding: utf-8 -*-
"""鲸舰取数（码表核对用）。

只要机台级「投币额」——鲸舰的投币额就是投币数（次数只是交易笔数）。
出珠/出卡在鲸舰里没有，只能从机器屏幕抄。

sign = MD5(timestamp \\n nonce \\n 参数串 \\n token)
GET 参数串 = query 按 key 排序拼 &；POST = 紧凑 JSON body
token 放 /opt/taocan/.cache/jingjian.json，用 refresh 接口续期（约 41h）。
"""
import hashlib
import json
import os
import time
import urllib.error
import urllib.request

TOKEN_FILE = os.environ.get("JJ_TOKEN_FILE") or "/opt/taocan/.cache/jingjian.json"
DEFAULT_BASE = os.environ.get("JJ_BASE") or "http://jt.jingjianx.vip"
JSON_KW = {"separators": (",", ":"), "ensure_ascii": False}
CRED_FILE = os.environ.get("BD_CRED_FILE") or "/opt/taocan/.cache/baodian_account.json"
JT = os.environ.get("JJ_LOGIN_BASE") or "http://jt.jingjianx.vip"
NONCE = "mabiao01"


class JingjianError(RuntimeError):
    pass


def _md5(t: str) -> str:
    return hashlib.md5(t.encode("utf-8")).hexdigest()


def _sign(ts: str, nonce: str, param: str, token: str) -> str:
    return _md5("%s\n%s\n%s\n%s" % (ts, nonce, param, token))


def _param_get(query: str) -> str:
    if not query:
        return ""
    return "&".join(sorted(query.split("&")))


def _load() -> dict:
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(d: dict) -> None:
    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)


def _request(base: str, method: str, path_qs: str, body=None, token: str = "") -> dict:
    path, _, query = path_qs.partition("?")
    if method.upper() == "GET":
        param = _param_get(query)
        payload = None
    else:
        param = json.dumps(body or {}, **JSON_KW)
        payload = param.encode("utf-8")
    ts = str(int(time.time() * 1000))
    req = urllib.request.Request(
        base.rstrip("/") + path_qs,
        data=payload,
        method=method.upper(),
        headers={
            "Authorization": "Bearer " + token,
            "nonce": NONCE,
            "timestamp": ts,
            "sign": _sign(ts, NONCE, param, token),
            "content-type": "application/json;charset=UTF-8",
            "User-Agent": "Mozilla/5.0 meter-check",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise JingjianError("鲸舰 HTTP %s: %s" % (e.code, path))
    except urllib.error.URLError as e:
        raise JingjianError("鲸舰网络异常: %s" % (e.reason,))


def _creds() -> tuple:
    with open(CRED_FILE, encoding="utf-8") as f:
        d = json.load(f)
    return (d.get("username") or "").strip(), d.get("password") or ""


def login_shop(shop_id, chain_id, base: str = "") -> str:
    """账密直登换某家店 token：登录 → 切集团 → 选门店拿 authCode → 换门店 token。

    鲸舰的 token 分两层：账号 token 调不了数据，必须换成门店 token。
    """
    user, pwd = _creds()
    if not user or not pwd:
        raise JingjianError("没有鲸舰账号凭据：%s" % CRED_FILE)
    body = {"deviceId": "mabiao", "userName": user, "password": pwd,
            "rightKey": "1", "metaType": 1, "authScene": 2, "remark": "码表核对"}
    r = _request(JT, "POST", "/basic/login/account", body, token="")
    if not r.get("success"):
        raise JingjianError("鲸舰登录失败：%r" % (r.get("msg"),))
    tok = (r.get("data") or {}).get("token") or ""
    if not tok:
        raise JingjianError("鲸舰登录没返回 token")
    if chain_id:
        sw = _request(JT, "POST", "/basic/login/switch/chain", {"chainId": int(chain_id)}, token=tok)
        tok = (sw.get("data") or {}).get("token") or tok
    sp = _request(JT, "POST", "/basic/login/shop",
                  {"chainId": int(chain_id or 0), "shopId": int(shop_id)}, token=tok)
    auth = (sp.get("data") or {}).get("authCode") or ""
    if not auth:
        raise JingjianError("鲸舰选门店失败：%r" % (sp.get("msg"),))
    host = base or (sp.get("data") or {}).get("url") or ""
    acr = _request(host, "POST", "/basic/manager/login/authcode", {"authCode": auth}, token=tok)
    shop_tok = (acr.get("data") or {}).get("token") or ""
    if not shop_tok:
        raise JingjianError("鲸舰换门店 token 失败：%r" % (acr.get("msg"),))
    d = _load()
    d.setdefault("tokens", {})[str(shop_id)] = {
        "token": shop_tok, "saved_at": time.time(), "base": host, "chain_id": chain_id}
    _save(d)
    return shop_tok


def token(base: str = DEFAULT_BASE, shop_id: str = "", chain_id: str = "") -> str:
    """取某家店的可用 token（鲸舰 token 绑店，一店一份）；过期就 refresh，refresh 不行就账密重登。"""
    d = _load()
    key = str(shop_id or "")
    ent = (d.get("tokens") or {}).get(key) or {}
    if not ent and d.get("token"):
        ent = d                      # 兼容旧的单 token 结构
        if key:                      # 认领到当前门店，下次直接命中
            d.setdefault("tokens", {})[key] = {
                "token": d["token"], "saved_at": d.get("saved_at") or 0,
                "base": d.get("base") or base}
            _save(d)
    tok = (ent.get("token") or "").strip()
    saved = float(ent.get("saved_at") or 0)
    fresh = saved and (time.time() - saved) < 30 * 3600
    if tok and fresh:
        return tok
    if not tok:
        if chain_id:
            return login_shop(shop_id, chain_id, base)
        raise JingjianError("鲸舰 token 未初始化（需要贴一次 HAR 里的 token）")
    r = None
    for path in ("/basic/account/refresh/token", "/basic/login/refresh/token"):
        try:
            r = _request(base, "POST", path, {}, token=tok)
            if r.get("success"):
                break
        except JingjianError:
            continue
    new = (r or {}).get("data", {}).get("token") if r else None
    if not r or not r.get("success") or not new:
        if chain_id:
            return login_shop(shop_id, chain_id, base)
        raise JingjianError("鲸舰 token 续期失败: %r" % ((r or {}).get("msg"),))
    d.setdefault("tokens", {})[key] = {"token": new, "saved_at": time.time(), "base": base}
    _save(d)
    return new


def save_initial(tok: str, base: str = DEFAULT_BASE, shop_id: str = "") -> dict:
    d = _load()
    d.setdefault("tokens", {})[str(shop_id or "")] = {
        "token": tok.strip(), "saved_at": time.time(), "base": base}
    _save(d)
    return {"ok": True, "shop_id": str(shop_id or "")}


def machine_consoles(base: str, shop_id, day_from: str, day_to: str, chain_id: str = "",
                     max_pages: int = 8) -> list:
    """机台 + 卡头清单：从消费明细里取「机台 / P位(consoleNo) / 通讯ID(commId)」。

    汇总接口只有机台级金额；明细接口 /machineplaylog/export 才带 consoleNo 和 commId。
    先用汇总拿机台（含当天没玩的），再按 machineId 过滤明细，逐台收集它的机位。
    返回 [{machine, machineNo, kind, consoles:[{p, comm}]}]
    """
    tok = token(base, shop_id, chain_id)
    body = {"startDate": day_from, "endDate": day_to, "KindIdStr": "", "isCalculate": True}
    r = _request(base, "POST", "/device/manager/machineplaylog/getsummarylist", body, token=tok)
    if not r.get("success"):
        raise JingjianError("鲸舰汇总接口失败: %r" % (r.get("msg"),))
    out = []
    for m in r.get("data") or []:
        mid = m.get("machineId")
        name = (m.get("machineName") or "").strip()
        if not mid or not name:
            continue
        consoles = {}
        for pi in range(1, max_pages + 1):
            q = {"startDate": day_from, "endDate": day_to, "KindIdStr": "", "isCalculate": True,
                 "machineId": mid, "pageIndex": pi, "pageSize": 200}
            try:
                rr = _request(base, "POST", "/device/manager/machineplaylog/export", q, token=tok)
            except JingjianError:
                break
            rows = rr.get("data") or []
            before = len(consoles)
            for x in rows:
                cno = str(x.get("consoleNo") or "").strip().upper()
                cid = x.get("commId")
                if cno and cid:
                    consoles.setdefault(cno, str(cid))
            if len(rows) < 20 or len(consoles) == before:
                break
        out.append({"machine": name, "machineNo": m.get("machineNo"),
                    "kind": m.get("kindName"),
                    "consoles": [{"p": p, "comm": consoles[p]} for p in sorted(consoles)]})
    return out


def day_consoles(base: str, shop_id, day: str, chain_id: str = "") -> list:
    """按 P 位取一天的机台汇总 → [{machine, pos, console_id, coins, count, out_coins, winning}]。

    鲸舰的「机台消费汇总」有两种口径：按机台 / 按 P 位。
    码表是按 P 位核对的，所以这里走 P 位接口。
    """
    tok = token(base, shop_id, chain_id)
    body = {"startDate": day, "endDate": day, "KindIdStr": "", "isCalculate": True}
    path = "/device/manager/machineplaylog/getconsolesummarylist"
    try:
        r = _request(base, "POST", path, body, token=tok)
        if not r.get("success"):
            raise JingjianError("鲸舰P位汇总失败: %r" % (r.get("msg"),))
    except JingjianError as exc:
        if "401" not in str(exc) or not chain_id:
            raise
        tok = login_shop(shop_id, chain_id, base)
        r = _request(base, "POST", path, body, token=tok)
    out = []
    for x in (r.get("data") or []):
        mach = str(x.get("machineName") or "").strip()
        if not mach:
            continue
        digits = "".join(ch for ch in str(x.get("consoleNo") or "") if ch.isdigit())
        out.append({
            "machine": mach,
            "pos": int(digits) if digits else 1,
            "console_id": x.get("consoleId"),
            "coins": float(x.get("inTotalAmount") or 0),
            "count": float(x.get("inTotalCount") or 0),
            "out_coins": float(x.get("outNativeCoin1TotalAmount") or 0),
            "winning": float(x.get("winningCoins") or 0),
        })
    out.sort(key=lambda r: (r["machine"], r["pos"]))
    return out


def day_summary(base: str, shop_id, day: str, chain_id: str = "") -> list:
    """一天一店的机台汇总 → [{machine, coins, count}]。"""
    tok = token(base, shop_id, chain_id)
    body = {"startDate": day, "endDate": day, "KindIdStr": "", "isCalculate": True}
    try:
        r = _request(base, "POST", "/device/manager/machineplaylog/getsummarylist", body, token=tok)
        if not r.get("success"):
            raise JingjianError("鲸舰汇总接口失败: %r" % (r.get("msg"),))
    except JingjianError as exc:
        # token 被服务端拒了（过期/换设备）→ 用账密重登一次再试
        if "401" not in str(exc) or not chain_id:
            raise
        tok = login_shop(shop_id, chain_id, base)
        r = _request(base, "POST", "/device/manager/machineplaylog/getsummarylist", body, token=tok)
    rows = r.get("data") or []
    out = []
    for x in rows:
        sid = x.get("shopId")
        if shop_id and sid and str(sid) != str(shop_id):
            continue           # 只留本店
        out.append({
            "machine": str(x.get("machineName") or "").strip(),
            "coins": float(x.get("inTotalAmount") or 0),
            "count": float(x.get("inTotalCount") or 0),
        })
    return out
