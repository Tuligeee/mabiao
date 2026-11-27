#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把每家店的机台从各自后台同步进码表库（宝点 / 鲸舰 / 世软）。

为什么要有这个脚本
------------------
设置页里的「从后台同步机台信息」一次只做一家店；铺全部门店要一家家点，
而且手工店（嘻哈卡卡）后台没有数据，得按清单建。这个脚本把两件事一次做完：

  · 非手工店：机台名、类型、卡头、P 位一律以后台为准（宝点设备清单 / 鲸舰机台表 / 世软游戏机数据）
  · 手工店：按 deploy/xiha_s21.json 的清单建（嘻哈卡卡 36 台萌力双胞胎弹珠机）

它不碰门店配置、品牌参数、二维码编号规则，也不删任何历史读数；
重复跑是幂等的（同名机台按名字更新，不会重开一台）。

用法（在服务器上，/opt/meter 目录）
----------------------------------
    python3 sync_machines.py --dry-run          # 只拉清单，不写库
    python3 sync_machines.py --apply            # 写库（先自动备份 meter.db）
    python3 sync_machines.py --apply --only S18,S23
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import server as S            # noqa: E402
import backend_sync           # noqa: E402

MANUAL_MANIFEST = os.path.join(HERE, "xiha_s21.json")


def visible_shops() -> list:
    """和界面同一口径：宝点集团那 3 家内部测试店不铺。"""
    with S.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT id, code, name, provider, group_name FROM shops ORDER BY code")]
    out = []
    for r in rows:
        if (r.get("group_name") or "") in S.HIDDEN_GROUPS:
            continue
        if (r.get("name") or "") in S.HIDDEN_SHOPS:
            continue
        out.append(r)
    return out


def _items_from_devices(devices: list, pos: dict) -> list:
    items = []
    for d in devices:
        items.append({"name": (d.get("name") or "").strip(),
                      "kind": d.get("kind"),
                      "comms": [str(x) for x in d.get("comms") or []],
                      "pos": [pos.get(str(x)) for x in d.get("comms") or []]})
    return items


def plan_baodian(shop_id: str, days: int) -> dict:
    devices = S.bd_device_list(shop_id)
    pos = S.bd_pos_matrix(shop_id, days)
    need = {c for d in devices for c in d["comms"]}
    missing = need - set(pos)
    deep = False
    if missing and len(missing) * 10 > len(need) * 4:
        pos = S.bd_pos_deep(shop_id, need, 60)
        deep = True
    items = _items_from_devices(devices, pos)
    return {"items": items, "pos_have": sum(1 for i in items for p in i["pos"] if p),
            "deep": deep, "unknown": getattr(S.bd_device_list, "unknown", {}) or {}}


def plan_platform(shop_id: str, days: int) -> dict:
    out = backend_sync.preview_rows(shop_id, days)
    items = [{"name": (r.get("name") or "").strip(), "kind": r.get("kind"),
              "comms": [str(x) for x in r.get("comms") or []],
              "pos": list(r.get("pos") or [])} for r in out.get("rows") or []]
    return {"items": items, "pos_have": out.get("pos_have") or 0,
            "deep": False, "unknown": out.get("unknown") or []}


def _comm_key(comms: list) -> int:
    nums = [int(x) for x in comms or [] if str(x).isdigit()]
    return min(nums) if nums else 0


def disambiguate(items: list) -> list:
    """后台同名机台（如多家「弹珠机」）加 -2、-3 后缀，别被合成一台。

    后缀按卡头号从小到大定，重复跑结果一样；不能写成「弹珠机 (2)」——
    machine_base 会把括号序号剥掉，又并回一台。
    """
    groups: dict = {}
    for it in items:
        groups.setdefault(S.machine_base(it["name"]), []).append(it)
    used = {S.machine_base(it["name"]) for it in items}
    for base, group in groups.items():
        if len(group) < 2:
            continue
        group.sort(key=lambda it: _comm_key(it.get("comms")))
        n = 2
        for it in group[1:]:
            while True:
                cand = "%s-%d" % (base, n)
                n += 1
                if cand not in used:
                    break
            it["name"] = cand
            used.add(cand)
    return items


def unique_items(items: list, allow_no_comm: bool = False) -> tuple:
    """同一家店里同名的机台不能各建一台（会撞 UNIQUE）。返回可用清单 + 被跳过的名字。"""
    seen, out, dup = set(), [], []
    for it in items:
        if not it["name"] or (not (it.get("comms") or []) and not allow_no_comm):
            continue
        key = S.machine_base(it["name"])
        if key in seen:
            dup.append(it["name"])
            continue
        seen.add(key)
        out.append(it)
    return out, dup


def seed_manual(shop: dict) -> dict:
    """手工店按清单建机台：一台机一个码表位，没有卡头。"""
    with open(MANUAL_MANIFEST, encoding="utf-8") as f:
        man = json.load(f)
    if (man.get("shop_code") or "") != shop["code"]:
        raise RuntimeError("清单是给 %s 的，不是 %s" % (man.get("shop_code"), shop["code"]))
    names = [str(x).strip() for x in man.get("names") or [] if str(x).strip()]
    if not names:
        raise RuntimeError("清单是空的")
    for nm in names:
        S.save_unit({"shop_id": shop["id"], "name": nm, "kind": man.get("kind") or "pachinko",
                     "team": man.get("team"), "brand": man.get("brand"), "comms": []})
    return {"items": [{"name": nm} for nm in names], "pos_have": 0, "deep": False}


def count_db(shop_id: str) -> dict:
    with S.conn() as c:
        machines = c.execute("SELECT COUNT(*) FROM machines WHERE shop_id=? AND active=1",
                             (shop_id,)).fetchone()[0]
        units = c.execute("SELECT COUNT(*) FROM units WHERE shop_id=? AND active=1",
                          (shop_id,)).fetchone()[0]
        comms = c.execute("SELECT COUNT(*) FROM comms m JOIN units u ON u.id=m.unit_id "
                          "WHERE u.shop_id=?", (shop_id,)).fetchone()[0]
        nopos = c.execute("SELECT COUNT(*) FROM comms m JOIN units u ON u.id=m.unit_id "
                          "WHERE u.shop_id=? AND (m.console_no IS NULL OR m.console_no='')",
                          (shop_id,)).fetchone()[0]
    return {"machines": machines, "units": units, "comms": comms, "no_pos": nopos}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="写库")
    ap.add_argument("--dry-run", action="store_true", help="只拉清单（默认）")
    ap.add_argument("--only", default="", help="只做这些门店编号，逗号分隔，如 S18,S23")
    ap.add_argument("--days", type=int, default=30, help="宝点 P 位回溯天数")
    ap.add_argument("--report", default="", help="把结果写成 JSON 文件")
    args = ap.parse_args()
    apply = bool(args.apply) and not args.dry_run

    only = {x.strip().upper() for x in args.only.split(",") if x.strip()}
    shops = [s for s in visible_shops() if not only or s["code"].upper() in only]
    if not shops:
        print("没有要同步的门店")
        return 1

    if apply:
        ts = time.strftime("%Y%m%d-%H%M%S")
        bak = str(S.DB) + ".bak-syncall-" + ts
        src = sqlite3.connect(str(S.DB))
        dst = sqlite3.connect(bak)
        src.backup(dst)
        dst.close()
        src.close()
        print("已备份数据库 -> %s" % bak)

    report = {"apply": apply, "started": S.now(), "shops": []}
    total_m = total_u = total_c = 0
    t0 = time.time()

    for sh in shops:
        code, name, prov = sh["code"], sh["name"], sh["provider"] or "baodian"
        t1 = time.time()
        row = {"code": code, "name": name, "provider": prov}
        try:
            if prov == "manual":
                plan = seed_manual(sh) if apply else {
                    "items": [{"name": x} for x in json.load(
                        open(MANUAL_MANIFEST, encoding="utf-8"))["names"]],
                    "pos_have": 0, "deep": False}
            elif prov == "baodian":
                plan = plan_baodian(sh["id"], args.days)
            else:
                plan = plan_platform(sh["id"], 7)
            plan["items"] = disambiguate(plan["items"])
            items, dup = unique_items(plan["items"], allow_no_comm=(prov == "manual"))
            row.update({"machines": len(items),
                        "comms": sum(len(i.get("comms") or []) for i in items),
                        "pos_have": plan.get("pos_have", 0),
                        "deep": plan.get("deep", False),
                        "duplicates": dup})
            if prov == "manual" and len(items) != 36:
                row["warn"] = "清单不是 36 台（%d）" % len(items)
            if apply:
                if prov == "manual":
                    pass                       # seed_manual 里已写
                else:
                    res = S.bd_sync_apply(sh["id"], items)
                    row["apply"] = res
                row.update(count_db(sh["id"]))
                total_m += row.get("machines", 0)
                total_u += row.get("units", 0)
                total_c += row.get("comms", 0)
            print("%-4s %-34s %-9s 机台 %-3d 卡头 %-3d P位 %-3d/%-3d %s (%.1fs)"
                  % (code, name[:34], prov, row["machines"], row["comms"],
                     row["pos_have"], row["comms"], "写好" if apply else "预览",
                     time.time() - t1), flush=True)
            if dup:
                print("     跳过重名：%s" % ", ".join(dup), flush=True)
        except Exception as e:                       # noqa: BLE001
            row["error"] = str(e)[:300]
            print("%-4s %-34s %-9s 失败：%s" % (code, name[:34], prov, e), flush=True)
        report["shops"].append(row)

    if apply:
        S.ensure_codes()
        for row in report["shops"]:
            if row.get("error"):
                continue
            sh = next((x for x in shops if x["code"] == row["code"]), None)
            if sh:
                row.update(count_db(sh["id"]))
        print("合计：%d 店，%d 台机，%d 个码表位，用时 %.1fs"
              % (len(shops), total_m, total_u, time.time() - t0))
    else:
        print("预览合计：%d 店，%d 台机，用时 %.1fs"
              % (len(shops), sum(s.get("machines", 0) for s in report["shops"]),
                 time.time() - t0))

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print("报告：%s" % args.report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
