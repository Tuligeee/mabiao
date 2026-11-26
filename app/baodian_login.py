#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""宝点云「纯 HTTP 登录」——不启动浏览器。

背景（2026-10-21 抓包实测，详见 baodian skill `references/api-direct.md`）：
  POST /customers/account/login/captcha  body {"codeid": "<客户端 uuid4>"}
      -> {"data": "data:image/gif;base64,…"}        ← codeid 由**客户端**生成
  POST /customers/account/login/pwd      body {"username","password","codeid","code"}
      -> {"success": true, "data": {"token": {"accessToken": "eyJ…"}}}
  两个接口都**不需要 XH-SIGN**；登录响应**无 Set-Cookie**
  （cookie xuehua_admin_token 是前端 JS 自己写的）。

实测收益：登录 83s（浏览器 + 滑块 + 12 次重试）→ 4s（纯 HTTP，OCR 重试 2~3 次）。

⚠️ 关键坑位：
  1. codeid 每次都要新生成，一个 codeid 只用一次；认错就换号重新要图。
  2. token 在 `data.token.accessToken`，**不是** `data.accessToken`。
  3. 验证码是 6 帧 GIF 动图，必须逐帧 OCR 投票（单帧容易错）。
  4. 拿到 token 后必须带 XH-SIGN 才能调数据接口（签名逻辑不在这里）。

对外接口：
    http_login(cfg=None, retries=8) -> dict
        {"token": str, "token_obj": dict, "shops": [...], "groups": [...]}
    token_exp(token) -> (exp_ts, 剩余秒数)
    is_token_fresh(token, margin=1800) -> bool
"""

from __future__ import annotations
import sys

import base64
import hashlib
import io
import json
import os
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

API_BASE = "https://api.degrade.xhyun.vip"
CONFIG_PATH = Path(__file__).resolve().parent / "daily_performance" / "config.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
BASE_HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://md.xhyun.vip",
    "Referer": "https://md.xhyun.vip/",
}

_ocr_engine = None


def log(msg: str) -> None:
    print(f"[宝点HTTP登录] {msg}", flush=True)


def load_config() -> dict:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def _ocr():
    global _ocr_engine
    if _ocr_engine is None:
        import ddddocr
        _ocr_engine = ddddocr.DdddOcr(show_ad=False)
    return _ocr_engine


def _post(path: str, body: dict, extra_headers: dict | None = None, timeout: int = 30):
    headers = dict(BASE_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    payload = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
    req = urllib.request.Request(API_BASE + path, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code} {path}: {text[:200]}") from None


def _get(path: str, params: dict, token: str, customer_id: str, timeout: int = 30):
    params = {k: v for k, v in (params or {}).items() if v not in (None, "")}
    ts = str(int(time.time() * 1000))
    nonce = str(uuid.uuid4())
    parts = [f"{k}={v}" for k, v in params.items()]
    sign = hashlib.md5("&".join(sorted(parts) + [nonce, ts, token]).encode()).hexdigest()
    query = ("?" + "&".join(parts)) if parts else ""
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://md.xhyun.vip",
        "Referer": "https://md.xhyun.vip/",
        "Authorization": f"Bearer {token}",
        "XH-CUSTOMERID": customer_id,
        "XH-TERMINAL": "backend",
        "XH-TYPE": "0",
        "XH-NONCE": nonce,
        "XH-TIMESTAMP": ts,
        "XH-SIGN": sign,
    }
    req = urllib.request.Request(API_BASE + path + query, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", "replace")
        raise RuntimeError(f"HTTP {exc.code} {path}: {text[:200]}") from None


def _recognize_gif(raw: bytes) -> tuple[str, dict]:
    """逐帧 OCR + 多数投票。返回 (识别结果, 投票明细)。"""
    from PIL import Image, ImageSequence

    engine = _ocr()
    try:
        image = Image.open(io.BytesIO(raw))
    except Exception:
        return "", {}
    votes: dict[str, int] = {}
    frames = 0
    try:
        for frame in ImageSequence.Iterator(image):
            frames += 1
            buf = io.BytesIO()
            frame.convert("RGB").save(buf, format="PNG")
            text = engine.classification(buf.getvalue()) or ""
            text = "".join(ch for ch in text.lower() if ch.isalnum())
            if text:
                votes[text] = votes.get(text, 0) + 1
    except Exception:
        pass
    if not votes:
        return "", {}
    best = sorted(votes.items(), key=lambda kv: (-kv[1], -len(kv[0])))[0][0]
    return best, votes


def _pick_token(payload: dict) -> tuple[str | None, dict | None]:
    data = payload.get("data")
    if not isinstance(data, dict):
        return None, None
    token_obj = data.get("token")
    if isinstance(token_obj, dict):
        access = token_obj.get("accessToken")
        if isinstance(access, str) and access.count(".") == 2:
            return access, token_obj
    for key in ("accessToken", "access_token", "token"):
        value = data.get(key)
        if isinstance(value, str) and value.count(".") == 2:
            return value, {"accessToken": value}
    return None, None


def fetch_shops(token: str, customer_id: str | None = None) -> list[dict]:
    """GET /customers/shop/list —— 一次返回全部可见门店（账号 18639065205 = 22 店）。

    customer_id 不传时自动取 JWT 的 sub（实测可行）。
    """
    header_customer = customer_id or jwt_sub(token)
    if not header_customer:
        raise RuntimeError("无法确定 XH-CUSTOMERID（JWT 里没有 sub）")
    payload = _get("/customers/shop/list", {"shopName": ""}, token, header_customer)
    if not payload.get("success"):
        raise RuntimeError(f"门店清单获取失败: {payload.get('msg')!r}")
    data = payload.get("data") or []
    return data if isinstance(data, list) else []


def group_shops(shops: list[dict]) -> list[dict]:
    """按 customerId 分组成集团视角，结构对齐 baodian_client.login_and_get_context。"""
    groups: dict[str, dict] = {}
    for shop in shops:
        cid = shop.get("customerId") or ""
        group = groups.setdefault(cid, {"customerId": cid, "customerName": shop.get("customerName"),
                                        "shops": []})
        group["shops"].append({
            "id": shop.get("id"),
            "shopName": (shop.get("shopName") or "").strip(),
            "chShopId": shop.get("chShopId"),
            "status": shop.get("status"),
        })
    return list(groups.values())


def token_exp(token: str) -> tuple[int, int]:
    """解码 JWT，返回 (exp 时间戳, 剩余秒数)。解码失败返回 (0, 0)。"""
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        payload = json.loads(base64.urlsafe_b64decode(seg))
        exp = int(payload.get("exp") or 0)
        return exp, exp - int(time.time())
    except Exception:
        return 0, 0


def jwt_sub(token: str) -> str | None:
    """取 JWT 的 sub（= 账号用户 ID）。

    ⚠️ 这是本模块最反直觉的一处：`XH-CUSTOMERID` 头传**用户 ID（sub）**就能拉到
    22 店全量清单；传全 0 或省略 → `签名验证失败 - 客户编码不正确`。
    实测 2026-10-21：`GET /customers/shop/list` + XH-CUSTOMERID=<sub> → 200 / 22 店。
    """
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg)).get("sub") or None
    except Exception:
        return None


def is_token_fresh(token: str | None, margin: int = 1800) -> bool:
    """token 是否仍可用（默认要求剩余 > 30 分钟）。"""
    if not token:
        return False
    exp, left = token_exp(token)
    return bool(exp) and left > margin


def http_login(cfg: dict | None = None, retries: int = 8, want_shops: bool = True) -> dict:
    """纯 HTTP 登录。失败抛 RuntimeError（由调用方决定是否降级到浏览器）。"""
    cfg = cfg or load_config()
    bd = cfg["baodian"]
    username, password = bd["username"], bd["password"]
    started = time.time()
    last_error = "unknown"

    for attempt in range(1, retries + 1):
        codeid = str(uuid.uuid4())
        try:
            _, cap = _post("/customers/account/login/captcha", {"codeid": codeid})
        except Exception as exc:
            last_error = f"取验证码失败: {exc}"
            log(f"第{attempt}次 {last_error}")
            continue

        data_uri = cap.get("data") or ""
        if not isinstance(data_uri, str) or not data_uri.startswith("data:image"):
            last_error = f"验证码结构异常: {str(cap)[:120]}"
            log(f"第{attempt}次 {last_error}")
            continue

        raw = base64.b64decode(data_uri.split(",", 1)[1])
        code, votes = _recognize_gif(raw)
        if not code:
            last_error = "OCR 置信不足"
            log(f"第{attempt}次 {last_error}")
            continue

        try:
            _, payload = _post("/customers/account/login/pwd",
                               {"username": username, "password": password,
                                "codeid": codeid, "code": code})
        except Exception as exc:
            last_error = f"登录请求失败: {exc}"
            log(f"第{attempt}次 {last_error}")
            continue

        if not payload.get("success"):
            last_error = f"code={code!r} msg={payload.get('msg')!r}"
            log(f"第{attempt}次 {last_error}")
            if "密码" in str(payload.get("msg") or ""):
                raise RuntimeError(f"账号或密码错误，请检查 config.json：{payload.get('msg')}")
            continue

        token, token_obj = _pick_token(payload)
        if not token:
            last_error = f"登录成功但未取到 token: {str(payload)[:200]}"
            log(f"第{attempt}次 {last_error}")
            continue

        shops: list[dict] = []
        if want_shops:
            try:
                shops = fetch_shops(token)
            except Exception as exc:
                log(f"登录成功但门店清单失败（不致命）: {exc}")

        elapsed = time.time() - started
        exp, left = token_exp(token)
        log(f"登录成功：{elapsed:.1f}s / 第{attempt}次 / JWT 剩余 {left/3600:.1f}h "
            f"/ 门店 {len(shops)}")
        return {
            "token": token,
            "token_obj": token_obj,
            "shops": shops,
            "groups": group_shops(shops) if shops else [],
            "elapsed": elapsed,
            "attempts": attempt,
            "exp": exp,
        }

    raise RuntimeError(f"纯 HTTP 登录失败（{retries} 次）：{last_error}")


def cookie_from_token(token_obj: dict) -> tuple[str, str]:
    """构造浏览器 cookie (name, value)，用于把 HTTP 登录态注入 Playwright context。"""
    import urllib.parse
    raw = json.dumps(token_obj, ensure_ascii=False, separators=(",", ":"))
    return "xuehua_admin_token", urllib.parse.quote(raw, safe="")




# ---------------------------------------------------------------- 码表用的入口
# 凭据文件（600）：{"username": "...", "password": "..."}
CRED_FILE = os.environ.get("BD_CRED_FILE") or "/opt/taocan/.cache/baodian_account.json"
# 共享登录态（日报/套餐/etc 都读这个）
SESSION_FILE = os.environ.get("BD_SESSION_FILE") or "/opt/taocan/.cache/session.json"


def _creds() -> dict:
    with open(CRED_FILE, encoding="utf-8") as f:
        d = json.load(f)
    user = (d.get("username") or "").strip()
    pwd = d.get("password") or ""
    if not user or not pwd:
        raise RuntimeError("凭据文件不全：" + CRED_FILE)
    return {"baodian": {"username": user, "password": pwd}}


def _write_session(res: dict) -> dict:
    """把登录结果写成日报/套餐共用的 session.json（ctx + exp + ts）。"""
    token = res["token"]
    exp, left = token_exp(token)
    ctx = {
        "token": token,
        "exp": exp,
        "shops": res.get("shops") or [],
        "groups": res.get("groups") or [],
        "curCustomers": [],
    }
    data = {"ts": time.time(), "exp": exp, "ctx": ctx}
    os.makedirs(os.path.dirname(SESSION_FILE), exist_ok=True)
    tmp = SESSION_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, SESSION_FILE)
    try:
        os.chmod(SESSION_FILE, 0o600)
    except OSError:
        pass
    return {"token_len": len(token), "exp": exp, "left_hours": round(left / 3600, 2),
            "shops": len(ctx["shops"]), "groups": len(ctx["groups"])}


def session_state() -> dict:
    """当前共享登录态还活着吗。"""
    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {"alive": False, "why": "没有登录态文件"}
    tok = ((d.get("ctx") or {}).get("token") or "").strip()
    if not tok:
        return {"alive": False, "why": "登录态里没有 token"}
    exp, left = token_exp(tok)
    return {"alive": bool(exp) and left > 1800, "exp": exp,
            "left_hours": round(left / 3600, 2),
            "exp_text": time.strftime("%m-%d %H:%M", time.localtime(exp)) if exp else ""}


def ensure(min_hours: float = 0.5) -> dict:
    """登录态快过期就用账密重新登一次，成功返回摘要。"""
    st = session_state()
    if st.get("alive") and (st.get("left_hours") or 0) > min_hours:
        return {"ok": True, "reused": True, **st}
    res = http_login(_creds(), retries=6)
    out = _write_session(res)
    return {"ok": True, "reused": False, **out}


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只看登录态")
    ap.add_argument("--ensure", action="store_true", help="过期就重登")
    args = ap.parse_args()
    try:
        if args.check and not args.ensure:
            print(json.dumps(session_state(), ensure_ascii=False))
            sys.exit(0)
        print(json.dumps(ensure(), ensure_ascii=False))
        sys.exit(0)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)[:300]}, ensure_ascii=False))
        sys.exit(3)
