# -*- coding: utf-8 -*-
"""一次跑完所有测试：后端逻辑 + 管理接口 + 前端返回键。"""
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
PY = sys.executable

SUITES = [
    ("后端逻辑", "test_all.py"),
    ("管理接口", "test_api.py"),
    ("前端返回", "test_history.py"),
]


def main():
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("METER_QUIET", "1")
    total_ok = 0
    total_fail = 0
    for name, f in SUITES:
        print("=" * 60)
        print("跑：" + name)
        r = subprocess.run([PY, str(HERE / f)], capture_output=True, env=env)
        out = (r.stdout + r.stderr).decode("utf-8", "replace")
        tail = [x for x in out.strip().splitlines() if x.strip()][-8:]
        print("\n".join(tail))
        ok = r.returncode == 0
        total_ok += 1 if ok else 0
        total_fail += 0 if ok else 1
    print("=" * 60)
    print("三套测试：通过 %d / %d" % (total_ok, len(SUITES)))
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
