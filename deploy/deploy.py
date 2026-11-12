# -*- coding: utf-8 -*-
"""码表核对 · 部署脚本

两套环境，同一份代码：

    正式  /opt/meter      meter      8899   https://mancado.cn/cwsj/meter
    测试  /opt/meter-stg  meter-stg  8900   https://mancado.cn/cwsj/meter-stg

规矩（2026-11-10 定）：
  · 只传代码，**绝不动数据库**
  · 目标环境没有库时，才初始化一次（测试环境从正式库拷一份，方便验证真数据）
  · 每次部署前先把目标库备份一份，出事能回滚

用法：
  <python> deploy.py            部署正式
  <python> deploy.py --stg      部署测试
  <python> deploy.py --init-db  强制用本地库初始化（会覆盖，慎用）
"""
import io
import json
import os
import sys
import time

import paramiko

CFG_PATH = r"C:\Users\Tuilge\Desktop\自动化合集\日报自动化\.server_ssh.json"
REPO = r"C:\Users\Tuilge\Desktop\码表线上化"
APP = os.path.join(REPO, "app")

TARGETS = {
    "prod": {
        "dir": "/opt/meter",
        "service": "meter",
        "port": "8899",
        "public": "https://mancado.cn/cwsj/meter",
        "seed_from_prod": False,
        "cert_watch": True,
    },
    "stg": {
        "dir": "/opt/meter-stg",
        "service": "meter-stg",
        "port": "8900",
        "public": "https://mancado.cn/cwsj/meter-stg",
        "seed_from_prod": True,
        "cert_watch": False,
    },
}

# 只传这些，数据库不在里面
FILES = [
    "server.py", "index.html", "app.js", "app.css",
    "ui.js", "theme.css", "tokens.css", "machine_type_map.json",
    "vendor/jsQR.js",
]

# 不在 app 目录里的：本地相对路径 -> 远端相对路径
EXTRA = [("deploy/cert_watch.py", "bin/cert_watch.py")]

UNIT = """[Unit]
Description=Meter check ({label})
After=network.target

[Service]
Type=simple
WorkingDirectory={dir}
Environment=PORT={port}
Environment=METER_HOST=127.0.0.1
Environment=CWSJ_BASE=http://127.0.0.1:8789
Environment=METER_PUBLIC_BASE={public}
ExecStart=/usr/bin/python3 {dir}/server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
"""


def main():
    name = "stg" if "--stg" in sys.argv else "prod"
    t = TARGETS[name]
    init_db = "--init-db" in sys.argv
    remote = t["dir"]

    cfg = json.load(io.open(CFG_PATH, encoding="utf-8"))
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    c.connect(cfg["host"], username="root", password=cfg["password"], timeout=25)

    def run(cmd, timeout=180):
        _i, o, e = c.exec_command(cmd, timeout=timeout)
        return (o.read() + e.read()).decode("utf-8", "replace").strip()

    print("目标：%s  %s  :%s" % (name, remote, t["port"]))
    run("mkdir -p %s/data %s/vendor %s/bin" % (remote, remote, remote))

    # 1) 备份目标库
    ts = time.strftime("%Y%m%d-%H%M%S")
    print(run("if [ -f {0}/data/meter.db ]; then cp {0}/data/meter.db "
              "{0}/data/meter.db.bak-{1} && echo '已备份 {1}'; else echo '目标还没有库'; fi"
              .format(remote, ts)))

    # 2) 传代码
    sftp = c.open_sftp()
    for f in FILES:
        sftp.put(os.path.join(APP, f.replace("/", os.sep)), "%s/%s" % (remote, f))
    for local_p, rel in EXTRA:
        sftp.put(os.path.join(REPO, local_p.replace("/", os.sep)), "%s/%s" % (remote, rel))

    # 3) 库：没有才初始化
    exists = run("[ -f {0}/data/meter.db ] && echo yes || echo no".format(remote))
    if init_db or exists == "no":
        prod_ok = run("[ -f /opt/meter/data/meter.db ] && echo y || echo n") == "y"
        if t["seed_from_prod"] and prod_ok:
            run("cp /opt/meter/data/meter.db {0}/data/meter.db".format(remote))
            print("已从正式库拷一份做初始数据")
        else:
            import sqlite3
            snap = os.path.join(APP, "data", "_deploy.db")
            src = sqlite3.connect(os.path.join(APP, "data", "meter.db"))
            dst = sqlite3.connect(snap)
            src.backup(dst)
            dst.close()
            src.close()
            sftp.put(snap, "%s/data/meter.db" % remote)
            os.remove(snap)
            print("已用本地库初始化")
    else:
        print("目标库已存在，保持不动")
    sftp.close()

    # 4) systemd 单元
    unit = UNIT.format(label="正式" if name == "prod" else "测试",
                       dir=remote, port=t["port"], public=t["public"])
    sftp = c.open_sftp()
    with sftp.open("/etc/systemd/system/%s.service" % t["service"], "w") as f:
        f.write(unit)
    sftp.close()

    # 5) 证书监控（只给正式）
    if t["cert_watch"]:
        for unit_name in ("cert-watch.service", "cert-watch.timer"):
            src_u = os.path.join(REPO, "deploy", unit_name)
            if os.path.exists(src_u):
                sftp = c.open_sftp()
                with sftp.open("/etc/systemd/system/" + unit_name, "w") as f:
                    f.write(io.open(src_u, encoding="utf-8").read())
                sftp.close()
        print(run("systemctl daemon-reload && systemctl enable --now cert-watch.timer "
                  "> /dev/null 2>&1; systemctl is-active cert-watch.timer"))

    print(run("systemctl daemon-reload && systemctl enable {svc} > /dev/null 2>&1; "
              "systemctl restart {svc} && sleep 2 && systemctl is-active {svc}"
              .format(svc=t["service"])))
    print(run("curl -s -o /dev/null -w '本机 :%s -> %%{http_code}\\n' http://127.0.0.1:%s/"
              % (t["port"], t["port"])))
    print(run("ls -lh %s/data/meter.db | awk '{print $5, $9}'" % remote))
    c.close()


if __name__ == "__main__":
    main()
