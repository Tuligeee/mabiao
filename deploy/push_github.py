# -*- coding: utf-8 -*-
"""把本地仓库推到 GitHub。

为什么绕这一圈：这台电脑连不上 github.com（DNS 和 TLS 都过不去），
但服务器 47.114.33.192 上有部署密钥、能直连 GitHub。
所以流程是：本地打包 → 传到服务器 → 服务器推。

服务器上的两把钥匙（各管一个仓库）：
    ~/.ssh/cwsj_deploy     →  Tuligeee/cwsj     （Host 别名 github.com）
    ~/.ssh/mabiao_deploy   →  Tuligeee/mabiao   （Host 别名 github-mabiao）

用法：
    <workbuddy python> deploy/push_github.py
"""
import io
import json
import os
import subprocess
import sys

import paramiko

REPO = r"C:\Users\Tuilge\Desktop\码表线上化"
CFG_PATH = r"C:\Users\Tuilge\Desktop\自动化合集\日报自动化\.server_ssh.json"
REMOTE = "git@github-mabiao:Tuligeee/mabiao.git"
BRANCH = "main"


def main():
    # 1) 本地打包
    bundle = os.path.join(os.environ["TEMP"], "mabiao.bundle")
    r = subprocess.run(["git", "-C", REPO, "bundle", "create", bundle, "--all"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0:
        print("打包失败:", r.stderr)
        return 1
    print("已打包 %.1f KB" % (os.path.getsize(bundle) / 1024))

    # 2) 传上去并推
    cfg = json.load(io.open(CFG_PATH, encoding="utf-8"))
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(cfg["host"], username="root", password=cfg["password"], timeout=25)

    def run(cmd, t=240):
        _i, o, e = c.exec_command(cmd, timeout=t)
        return (o.read() + e.read()).decode("utf-8", "replace").strip()

    sftp = c.open_sftp()
    sftp.put(bundle, "/tmp/mabiao.bundle")
    sftp.close()

    print(run("rm -rf /tmp/mabiao_push && "
              "git clone -q /tmp/mabiao.bundle /tmp/mabiao_push && echo 已解包"))
    run("cd /tmp/mabiao_push && git remote set-url origin %s" % REMOTE)
    out = run("cd /tmp/mabiao_push && git push -u origin %s 2>&1 | tail -4" % BRANCH)
    print(out)

    # 3) 确认远端
    head = run("git ls-remote --heads %s %s" % (REMOTE, BRANCH))
    print("远端 %s: %s" % (BRANCH, head.split("\t")[0] if head else "【没读到】"))
    c.close()

    # 4) 顺手对比本地 HEAD
    local = subprocess.run(["git", "-C", REPO, "rev-parse", "HEAD"],
                           capture_output=True, text=True).stdout.strip()
    print("本地 HEAD: %s" % local)
    print("一致" if local and local in head else "【不一致，检查一下】")
    return 0


if __name__ == "__main__":
    sys.exit(main())
