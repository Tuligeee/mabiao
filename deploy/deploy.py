# -*- coding: utf-8 -*-
"""码表核对 · 部署脚本

规矩（2026-11-10 定）：
  · 只传代码，**绝不动服务器上的数据库**
  · 服务器没有库时，才用本地快照初始化一次
  · 每次部署前先把服务器库备份一份，出事能回滚

用法：
  <workbuddy python> deploy.py            # 正常部署
  <workbuddy python> deploy.py --init-db  # 强制用本地库初始化（会覆盖！）
"""
import io
import json
import os
import sys
import time

import paramiko

CFG_PATH = r"C:\Users\Tuilge\Desktop\自动化合集\日报自动化\.server_ssh.json"
APP = r"C:\Users\Tuilge\Desktop\码表线上化\app"
REMOTE = "/opt/meter"

# 只传这些，数据库不在里面
FILES = [
    "server.py", "index.html", "app.js", "app.css",
    "ui.js", "theme.css", "tokens.css", "machine_type_map.json",
]


def main():
    init_db = "--init-db" in sys.argv
    cfg = json.load(io.open(CFG_PATH, encoding="utf-8"))
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(cfg["host"], username="root", password=cfg["password"], timeout=25)

    def run(cmd, t=120):
        _i, o, e = c.exec_command(cmd, timeout=t)
        return (o.read() + e.read()).decode("utf-8", "replace").strip()

    run("mkdir -p %s/data" % REMOTE)

    # 1) 备份服务器库
    ts = time.strftime("%Y%m%d-%H%M%S")
    print(run("if [ -f {0}/data/meter.db ]; then cp {0}/data/meter.db "
              "{0}/data/meter.db.bak-{1} && echo '备份 {1}'; else echo '服务器还没有库'; fi"
              .format(REMOTE, ts)))

    # 2) 传代码
    sftp = c.open_sftp()
    for f in FILES:
        sftp.put(os.path.join(APP, f), "%s/%s" % (REMOTE, f))
    # 3) 只有服务器没库（或显式 --init-db）才传本地快照
    exists = run("[ -f {0}/data/meter.db ] && echo yes || echo no".format(REMOTE))
    if init_db or exists == "no":
        import sqlite3
        snap = os.path.join(APP, "data", "_deploy.db")
        src = sqlite3.connect(os.path.join(APP, "data", "meter.db"))
        dst = sqlite3.connect(snap)
        src.backup(dst)
        dst.close()
        src.close()
        sftp.put(snap, "%s/data/meter.db" % REMOTE)
        os.remove(snap)
        print("已用本地库初始化服务器数据库")
    else:
        print("服务器库已存在，保持不动")
    sftp.close()

    # 4) 重启
    print(run("systemctl restart meter && sleep 2 && systemctl is-active meter"))
    print(run("curl -s -o /dev/null -w 'page %{http_code}\\n' http://127.0.0.1/meter/"))
    print(run("ls -lh %s/data/meter.db* | tail -3" % REMOTE))
    c.close()


if __name__ == "__main__":
    main()
