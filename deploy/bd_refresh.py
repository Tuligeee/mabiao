#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""码表 · 宝点登录态续期（2026-11-26）

宝点 JWT 固定 12 小时、没有 refresh 接口，只能账密重登（要过图形验证码）。
这个脚本由 systemd timer 每 30 分钟调一次：**剩余不足 1 小时才重登**，
写回 /opt/taocan/.cache/session.json，日报/套餐/码表都读这一份。

和取数时的兜底登录共用一把文件锁（同目录 .bd_login.lock），
避免两边同时过验证码（白费一次尝试，还可能招风控）。

用法：
    bd_refresh.py              按需（剩 <1h 才登）
    bd_refresh.py --force      不管剩多少，立刻重登一次
    bd_refresh.py --check      只打印当前状态，不登录
    bd_refresh.py --dry-run    只打印判定结果，不登录
    bd_refresh.py --alert-test 发一条测试告警，验证钉钉通道

选项：
    --app DIR         server.py / baodian_login.py 所在目录（默认脚本的上一级）
    --session PATH    覆盖 session.json（演练用，别指向正式那份）
    --min-hours N     阈值，默认 1.0 小时
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_APP = os.path.dirname(HERE)          # /opt/meter/bin -> /opt/meter
SITE = "https://mancado.cn/cwsj/meter"


def log(msg: str) -> None:
    print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def default_app() -> str:
    for p in (DEFAULT_APP, "/opt/meter"):
        if os.path.exists(os.path.join(p, "baodian_login.py")):
            return p
    return DEFAULT_APP


def send_alert(title: str, text: str) -> None:
    """复用证书监控那条钉钉单聊通道，不另建机器人。"""
    try:
        sys.path.insert(0, HERE)
        import cert_watch
        cert_watch.send_dm(title, text)
    except Exception as e:                      # 告警失败不能影响续期判定
        log("告警发送失败：%s" % e)


def state_file(session_path: str) -> str:
    return os.path.join(os.path.dirname(session_path), ".bd_refresh_state.json")


def read_fail(session_path: str) -> int:
    try:
        with open(state_file(session_path), encoding="utf-8") as f:
            return int(json.load(f).get("fail") or 0)
    except Exception:
        return 0


def write_fail(session_path: str, n: int, note: str = "") -> None:
    try:
        path = state_file(session_path)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"fail": n, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "note": note[:200]}, f, ensure_ascii=False)
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--if-needed", action="store_true", help="默认行为，写出来给调用方看")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--alert-test", action="store_true")
    ap.add_argument("--app", default="")
    ap.add_argument("--session", default="")
    ap.add_argument("--min-hours", type=float,
                    default=float(os.environ.get("BD_REFRESH_MIN_HOURS") or 1.0))
    args = ap.parse_args()

    if args.alert_test:
        send_alert("宝点登录态续期 · 告警测试",
                   "### 码表核对 · 宝点续期告警通道测试\n\n"
                   "收到这条说明续期失败时能通知到你。\n\n"
                   "- 站点：%s\n- 阈值：剩余 < %.1f 小时就重登\n\n"
                   "平时不会发，只有连续两次重登失败才发。" % (SITE, args.min_hours))
        return 0

    app = args.app or default_app()
    sys.path.insert(0, app)
    import baodian_login as BL              # noqa: E402

    if args.session:
        BL.SESSION_FILE = args.session
    elif os.environ.get("BD_SESSION_FILE"):
        BL.SESSION_FILE = os.environ["BD_SESSION_FILE"]
    session_path = BL.SESSION_FILE

    st = BL.session_state()
    left = float(st.get("left_hours") or -1)
    stamp = st.get("exp_text") or "-"
    log("当前登录态：剩余 %.2f 小时（到期 %s）" % (left, stamp))

    if args.check:
        print(json.dumps(st, ensure_ascii=False))
        return 0

    need = args.force or not st.get("alive") or left < args.min_hours
    if not need:
        log("剩余 %.2f 小时，超过阈值 %.1f 小时，不登录" % (left, args.min_hours))
        print(json.dumps({"ok": True, "skipped": True, "left_hours": left}, ensure_ascii=False))
        return 0

    log("剩余 %.2f 小时（阈值 %.1f），开始重登%s"
        % (left, args.min_hours, "（--force）" if args.force else ""))
    if args.dry_run:
        log("[dry-run] 只判定不登录")
        print(json.dumps({"ok": True, "dry_run": True, "would_login": True,
                          "left_hours": left}, ensure_ascii=False))
        return 0

    # 和取数兜底共用一把锁：拿不到就跳过，下一轮再来
    lock_path = os.path.join(os.path.dirname(session_path), ".bd_login.lock")
    lf = None
    try:
        os.makedirs(os.path.dirname(lock_path), exist_ok=True)
        lf = open(lock_path, "w")
        fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("另一个登录进程正在跑（锁被占），本轮跳过")
        print(json.dumps({"ok": True, "skipped": "locked"}, ensure_ascii=False))
        if lf:
            lf.close()
        return 0

    fail = read_fail(session_path)
    try:
        res = BL.http_login(BL._creds(), retries=6)
        out = BL._write_session(res)
        new_left = float(out.get("left_hours") or 0)
        log("重登成功：新 token 剩余 %.2f 小时（门店 %s）"
            % (new_left, out.get("shops")))
        if fail > 0:
            write_fail(session_path, 0, "ok")
            send_alert("宝点登录态已恢复",
                       "### 码表核对 · 宝点登录态已恢复\n\n"
                       "刚才连续 %d 次续期失败，这次已经重新登上。\n\n"
                       "- 新 token 剩余：**%.2f 小时**\n- 站点：%s\n\n"
                       "如果最近没收到失败告警，这条可以忽略。" % (fail, new_left, SITE))
        print(json.dumps({"ok": True, "renewed": True, "left_hours": new_left,
                          "shops": out.get("shops")}, ensure_ascii=False))
        return 0
    except Exception as e:
        fail += 1
        msg = "%s: %s" % (type(e).__name__, str(e)[:200])
        write_fail(session_path, fail, msg)
        log("重登失败（连续第 %d 次）：%s" % (fail, msg))
        if fail == 2 or fail % 6 == 0:
            send_alert("宝点登录态续期失败",
                       "### 码表核对 · 宝点登录态续期失败\n\n"
                       "连续 **%d** 次重登没成功，取数会跟着失败。\n\n"
                       "- 剩余：%.2f 小时\n- 原因：`%s`\n- 站点：%s\n\n"
                       "多为图形验证码识别抖动；若持续失败，去服务器手动跑一次 "
                       "`bd_refresh.py --force` 看日志。" % (fail, max(left, 0), msg, SITE))
        print(json.dumps({"ok": False, "error": msg, "fail": fail}, ensure_ascii=False))
        return 2
    finally:
        if lf:
            try:
                fcntl.flock(lf, fcntl.LOCK_UN)
            finally:
                lf.close()


if __name__ == "__main__":
    sys.exit(main())
