#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""码表核对 · HTTPS 证书监控

证书是 Let's Encrypt 的 6 天短证书，续期是命脉。
这个脚本每天跑两次，剩余天数不足阈值就用「纯玩叮当」单聊发给你。

用法：
    cert_watch.py           正常检查，需要才发
    cert_watch.py --test    强制发一条，验证通道

发消息复用 /opt/bd-login/push_link.py 那套（钉钉内部应用机器人 oToMessages），
凭据从日报自动化的 linux/env.sh 读，不在这里另存一份。
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

CERT = "/etc/letsencrypt/live/mabiao-ip/fullchain.pem"
CONF = "/opt/meter/cert_watch.json"
ENV_SH = "/opt/日报自动化_服务器部署包/linux/env.sh"
SITE = "https://47.114.33.192/meter/"
DEFAULT_THRESHOLD = 1.5
DEFAULT_USER = "261408165826344012"  # 李灏铭，和宝点登录提醒同一个接收人

TOKEN_URL = "https://api.dingtalk.com/v1.0/oauth2/accessToken"
DM_URL = "https://api.dingtalk.com/v1.0/robot/oToMessages/batchSend"


def load_conf():
    try:
        with open(CONF, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def load_env():
    """从日报自动化的 env.sh 读钉钉凭据。"""
    out = {}
    try:
        with open(ENV_SH, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r'\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)=(.*)', line)
                if not m:
                    continue
                v = m.group(2).strip().strip('"').strip("'")
                out[m.group(1)] = v
    except Exception:
        pass
    return out


def access_token(app_key, app_secret):
    body = json.dumps({"appKey": app_key, "appSecret": app_secret}).encode("utf-8")
    req = urllib.request.Request(
        TOKEN_URL, data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        j = json.loads(r.read().decode("utf-8"))
    tok = j.get("accessToken")
    if not tok:
        raise RuntimeError("钉钉取 token 失败: %s" % json.dumps(j, ensure_ascii=False)[:200])
    return tok


def send_dm(title, text):
    """用「纯玩叮当」单聊发给指定用户。"""
    env = load_env()
    conf = load_conf()
    app_key = env.get("DINGTALK_APP_KEY") or os.environ.get("DINGTALK_APP_KEY", "")
    app_secret = env.get("DINGTALK_APP_SECRET") or os.environ.get("DINGTALK_APP_SECRET", "")
    robot = env.get("DINGTALK_ROBOT_CODE") or os.environ.get("DINGTALK_ROBOT_CODE", "")
    user = conf.get("user_id") or DEFAULT_USER

    if not (app_key and app_secret and robot):
        print("缺钉钉凭据（appKey/appSecret/robotCode），跳过发送")
        return False

    tok = access_token(app_key, app_secret)
    body = {
        "robotCode": robot,
        "userIds": [user],
        "msgKey": "sampleMarkdown",
        "msgParam": json.dumps({"title": title, "text": text}, ensure_ascii=False),
    }
    req = urllib.request.Request(
        DM_URL, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-acs-dingtalk-access-token": tok,
        }, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        j = json.loads(r.read().decode("utf-8"))
    if j.get("processQueryKey") or j.get("flowControlledStaffIdList") is not None:
        print("单聊已发送")
        return True
    print("单聊返回异常:", json.dumps(j, ensure_ascii=False)[:300])
    return False


def cert_expiry():
    """返回 (到期时间 UTC, 剩余天数)。读不到返回 (None, None)。"""
    try:
        out = subprocess.run(
            ["openssl", "x509", "-in", CERT, "-noout", "-enddate"],
            capture_output=True, text=True, timeout=20,
        ).stdout
    except Exception:
        return None, None
    m = re.search(r"notAfter=(.+)", out)
    if not m:
        return None, None
    try:
        exp = datetime.strptime(m.group(1).strip(), "%b %d %H:%M:%S %Y %Z").replace(tzinfo=timezone.utc)
    except ValueError:
        return None, None
    left = (exp - datetime.now(timezone.utc)).total_seconds() / 86400.0
    return exp, left


def main():
    conf = load_conf()
    test = "--test" in sys.argv
    exp, left = cert_expiry()

    if test:
        text = (
            "### 码表核对 · 告警通道测试\n\n"
            "收到这条说明证书告警能正常发到你这里。\n\n"
            "- 当前剩余：{}\n"
            "- 到期时间：{}\n"
            "- 站点：{}\n"
            "- 告警阈值：{} 天\n\n"
            "以后只有剩余天数低于阈值才会再发，平时不打扰。"
        ).format(
            "读不到" if left is None else "**{:.1f} 天**".format(left),
            "读不到" if exp is None else exp.strftime("%Y-%m-%d %H:%M UTC"),
            SITE,
            conf.get("threshold_days", DEFAULT_THRESHOLD),
        )
        send_dm("证书告警通道测试", text)
        return 0

    if left is None:
        send_dm("证书读不到",
                "### 码表核对 · HTTPS 证书读不到\n\n"
                "证书文件解析失败：`{}`\n\n站点：{}\n\n"
                "网站可能已经打不开，尽快看服务器。".format(CERT, SITE))
        return 1

    threshold = float(conf.get("threshold_days", DEFAULT_THRESHOLD))
    print("剩余 {:.2f} 天，阈值 {} 天".format(left, threshold))

    if left > threshold:
        return 0

    text = (
        "### 码表核对 · HTTPS 证书快到期\n\n"
        "**剩余 {:.1f} 天**（{} 到期）\n\n"
        "站点：{}\n\n"
        "---\n\n"
        "证书是 6 天的短证书，正常每天自动续两次。"
        "到这个点还没续上，说明续期失败了。\n\n"
        "**怎么处理**\n\n"
        "1. 上服务器跑 `certbot renew --force-renewal`\n"
        "2. 失败就看 `/var/log/letsencrypt/letsencrypt.log`\n"
        "3. 常见原因：80 端口被占（续期验证走 webroot）、安全组改过\n\n"
        "证书一过期，店长打开就是「无法访问」，而且不会知道原因。"
    ).format(left, exp.strftime("%Y-%m-%d %H:%M UTC"), SITE)
    send_dm("证书快到期", text)
    return 2


if __name__ == "__main__":
    sys.exit(main())
