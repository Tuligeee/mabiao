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


def token(base: str = DEFAULT_BASE, shop_id: str = "") -> str:
    """取某家店的可用 token（鲸舰 token 绑店，一店一份）；过期就 refresh。"""
    d = _load()
    key = str(shop_id or "")
    ent = (d.get("tokens") or {}).get(key) or {}
    if not ent and not key:
        ent = d                      # 兼容旧的单 token 结构
    tok = (ent.get("token") or "").strip()
    saved = float(ent.get("saved_at") or 0)
    fresh = saved and (time.time() - saved) < 30 * 3600
    if tok and fresh:
        return tok
    if not tok:
        raise JingjianError("鲸舰 token 未初始化（需要贴一次 HAR 里的 token）")
    r = None
    for path in ("/basic/account/refresh/token", "/basic/login/refresh/token"):
        try:
            r = _request(base, "POST", path, {}, token=tok)
            if r.get("success"):
                break
        except JingjianError:
            continue
    if r is None:
        raise JingjianError("鲸舰 token 续期失败：两个 refresh 路径都不通")
    new = (r.get("data") or {}).get("token")
    if not r.get("success") or not new:
        raise JingjianError("鲸舰 token 续期失败: %r" % (r.get("msg"),))
    d.setdefault("tokens", {})[key] = {"token": new, "saved_at": time.time(), "base": base}
    _save(d)
    return new


def save_initial(tok: str, base: str = DEFAULT_BASE, shop_id: str = "") -> dict:
    d = _load()
    d.setdefault("tokens", {})[str(shop_id or "")] = {
        "token": tok.strip(), "saved_at": time.time(), "base": base}
    _save(d)
    return {"ok": True, "shop_id": str(shop_id or "")}


def day_summary(base: str, shop_id, day: str) -> list:
    """一天一店的机台汇总 → [{machine, coins, count}]。"""
    tok = token(base, shop_id)
    body = {"startDate": day, "endDate": day, "KindIdStr": "", "isCalculate": True}
    r = _request(base, "POST", "/device/manager/machineplaylog/getsummarylist", body, token=tok)
    if not r.get("success"):
        raise JingjianError("鲸舰汇总接口失败: %r" % (r.get("msg"),))
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
