# -*- coding: utf-8 -*-
"""机台信息从后台系统同步（宝点 / 鲸舰 / 世软）。

原则（2026-11-26 定）：
  · 非手工店，平台的「机台 / 卡头 / P 位」以取数后台为准；
  · 后台给不出的字段（共用码表、品牌玩法、门店参数、二维码编号）仍归平台，这里不碰；
  · 本模块只产出「后台认为应该是什么样」，写库由调用方 dry-run 确认后再做。

产出统一结构：
    {"name": 机台名, "kind": "pusher|pachinko", "source": "baodian|jingjian|shiruan",
     "comms": [{"comm": "123456", "pos": "P2" | None, "pos_src": "flow|letter|none"}]}
"""
from __future__ import annotations

import datetime
import re

LAST_SKIPPED = []

BD_PUSHER = {"推币机", "推币", "娃娃机", "礼品机"}
BD_PACHINKO = {"弹珠机", "弹珠"}


def _days(n: int) -> list:
    today = datetime.date.today()
    return [(today - datetime.timedelta(days=i)).isoformat() for i in range(1, max(1, n) + 1)]


def _posnum(s) -> int | None:
    m = re.search(r"(\d+)", str(s or ""))
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------- 世软
def shiruan_machines(d: dict, days: int = 7) -> list:
    """世软的「游戏机数据查询」：Mach_No=机位（编号+字母），M_Mach_No=机台编号。

    字母 A→P1、B→P2，就是它的 P 位（平台里运营另起的字母名是历史包袱，不再用）。
    """
    import shiruan
    store = (d.get("jj_shop_id") or "").strip()
    if not store:
        raise RuntimeError("这家店没配世软门店号（jj_shop_id）")
    best: dict = {}
    for day in _days(days):
        try:
            rows = shiruan.day_summary(day, store)
        except Exception:
            continue
        for r in rows:
            comm = str(r.get("machine") or "").strip()
            if not comm:
                continue
            cur = best.get(comm)
            if cur is None or (r.get("pos") and not cur["pos"]):
                best[comm] = {"comm": comm,
                              "base": str(r.get("base") or "").strip()
                                      or re.sub(r"[A-Za-z]+$", "", comm),
                              "title": str(r.get("name") or "").strip(),
                              "pos": ("P%d" % r["pos"]) if r.get("pos") else None,
                              "pos_src": "letter" if r.get("pos") else "none",
                              "day": day}
    by_machine: dict = {}
    for x in best.values():
        by_machine.setdefault(x["base"], []).append(x)
    out = []
    for base, items in by_machine.items():
        items.sort(key=lambda x: (_posnum(x["pos"]) or 99, x["comm"]))
        title = next((x["title"] for x in items if x["title"]), "") or base
        out.append({"name": title, "kind": "pusher", "source": "shiruan", "no": base,
                    "comms": [{"comm": x["comm"], "pos": x["pos"],
                               "pos_src": x["pos_src"]} for x in items]})
    out.sort(key=lambda m: m["name"])
    return out


# ---------------------------------------------------------------- 鲸舰
# 鲸舰的分类：kindName=大类（模拟机/卡片机/弹珠机/配套机台），groupName=小类（推币机/推球机/…）
# 只收「大类/小类里带推币」和「弹珠机」；推球机、卡片机、配套机台、抽卡机一律不收。
# 注意：不看机台名——「黄金赛车」的小类就是推币机，按名字过滤会误杀。
def jj_kind_of(name: str, kind_name: str, group_name: str):
    """按鲸舰分类判机型：pusher / pachinko / None（不进码表）。

    2026-11-27：以「机台分组」为准 —— 鲸舰的机种名称列一律写礼品机，
    但分组里明确有「弹珠机类-潮弹社-刷卡出珠」「卡片类-推币机」这类真机型。
    """
    g = (group_name or "").strip()
    k = (kind_name or "").strip()
    nm = (name or "").strip()      # 机台名 + bdszhMachineName，里面常带「弹珠机/推币机」
    first = g or nm or k           # 分组 > 机台名 > 机种名
    if "推球" in g or "推球" in k:
        return None
    if "弹珠" in first or "出珠" in first:
        return "pachinko"
    if "推币" in first:
        return "pusher"
    # 分组和机台名都没写机型时，再看机种名兜底
    if "弹珠" in k:
        return "pachinko"
    if "推币" in k:
        return "pusher"
    return None


def jingjian_machines(d: dict, days: int = 7) -> list:
    """鲸舰：机台档案 getmachines + 卡头 getall（bindConsoleNo 就是 P 位，如 "1P"）。

    机台档案是权威的，不依赖最近有没有流水；卡头只保留 isBind 且绑在这台机上的。
    """
    import jingjian
    base = d.get("jj_base") or jingjian.DEFAULT_BASE
    shop_no = str(d.get("jj_shop_id") or "").strip()
    tok = jingjian.token(base, shop_no, (d.get("jj_chain_id") or "").strip())

    def getall(path: str) -> list:
        nonlocal tok
        out, page = [], 1
        while True:
            try:
                j = jingjian._request(base, "GET", "%s?page=%d&limit=100" % (path, page),
                                      None, tok)
            except jingjian.JingjianError as e:
                # 门店 token 失效：按知识库口径账密重登换一份新的，再试一次
                if "401" not in str(e):
                    raise
                tok = jingjian.login_shop(shop_no, (d.get("jj_chain_id") or "").strip(), base)
                j = jingjian._request(base, "GET", "%s?page=%d&limit=100" % (path, page),
                                      None, tok)
            data = j.get("data") or []
            out += data
            if len(data) < 100 or page >= 50:
                break
            page += 1
        return out

    machines = getall("/device/manager/machine/getmachines")
    terms = getall("/device/manager/terminal/getall")
    by_mid: dict = {}
    for t in terms:
        mid = t.get("bindMachineId")
        if not mid or not t.get("isBind"):
            continue
        by_mid.setdefault(mid, []).append(t)

    out = []
    skipped = []
    for m in machines:
        name = (m.get("machineName") or "").strip()
        kind = jj_kind_of("%s %s" % (name, m.get("bdszhMachineName") or ""),
                           m.get("kindName") or "", m.get("groupName") or "")
        if not kind:                     # 娃娃机 / 礼品机 / 配套机台等，不进码表
            skipped.append({"name": name, "kind": m.get("kindName") or "",
                            "group": m.get("groupName") or ""})
            continue
        mid = m.get("machineId")
        coms = []
        for t in by_mid.get(mid, []):
            cn = str(t.get("bindConsoleNo") or "").strip()
            coms.append({"comm": str(t.get("commId")), "pos": ("P%d" % _posnum(cn)) if _posnum(cn) else None,
                         "pos_src": "console" if _posnum(cn) else "none"})
        if not coms:
            for c in str(m.get("bindCommIds") or "").split(","):
                if c.strip().isdigit():
                    coms.append({"comm": c.strip(), "pos": None, "pos_src": "none"})
        if not coms:
            continue
        coms.sort(key=lambda x: (_posnum(x["pos"]) or 99, x["comm"]))
        out.append({"name": name, "kind": kind, "source": "jingjian", "comms": coms})
    out.sort(key=lambda m: m["name"])
    jingjian_machines.skipped = skipped
    global LAST_SKIPPED
    LAST_SKIPPED = skipped
    return out


# ---------------------------------------------------------------- 宝点
def baodian_machines(shop_id: str, days: int = 7) -> list:
    """宝点：机台档案接口不给 P 位，P 位从「机台获分记录」的 consoleNo 反推。

    取近 N 天里每个卡头**最后一次**出现的 consoleNo；没出现的卡头 pos=None（保持库里原值）。
    """
    import server as S
    bd = S.baodian(shop_id)
    shop = S.shop_of(S.shop_row(shop_id))
    best: dict = {}
    for day in _days(days):
        try:
            rows = bd.fetch_down(shop, day)
        except Exception:
            continue
        for r in rows:                       # 接口按 CreateTime desc，先到的就是最新
            cid = str(r.get("commId") or "").strip()
            if not cid or cid in best:
                continue
            best[cid] = {"comm": cid,
                         "name": (r.get("deviceName") or "").strip(),
                         "team": (r.get("projectTeamName") or "").strip(),
                         "pos": ("P%d" % _posnum(r.get("consoleNo"))) if _posnum(r.get("consoleNo")) else None,
                         "pos_src": "console" if _posnum(r.get("consoleNo")) else "none"}
    by_machine: dict = {}
    for x in best.values():
        by_machine.setdefault(x["name"] or "未命名", []).append(x)
    out = []
    for name, items in by_machine.items():
        team = (items[0].get("team") or "")
        kind = "pachinko" if team in BD_PACHINKO or "弹珠" in team else "pusher"
        items.sort(key=lambda x: (_posnum(x["pos"]) or 99, x["comm"]))
        out.append({"name": name, "kind": kind, "source": "baodian",
                    "comms": [{"comm": x["comm"], "pos": x["pos"], "pos_src": x["pos_src"]}
                              for x in items]})
    out.sort(key=lambda m: m["name"])
    return out


# ---------------------------------------------------------------- 统一入口
def machines_from_backend(shop_id: str, days: int = 7) -> dict:
    """按门店的取数方式，拿「后台认为的机台清单」。"""
    import server as S
    row = S.shop_row(shop_id)
    if not row:
        raise RuntimeError("门店不存在")
    d = dict(row)
    prov = (d.get("provider") or "baodian")
    if prov == "manual":
        raise RuntimeError("手工店不同步")
    if prov == "shiruan":
        ms = shiruan_machines(d, days)
    elif prov == "jingjian":
        ms = jingjian_machines(d, days)
    else:
        ms = baodian_machines(shop_id, days)
    return {"shop_id": shop_id, "provider": prov, "days": days, "machines": ms,
            "missing_pos": [c["comm"] for m in ms for c in m["comms"] if not c["pos"]]}


def preview_rows(shop_id: str, days: int = 7) -> dict:
    """统一预览：三家的机台清单整理成宝点弹窗用的那套结构（new/same/update）。"""
    import server as S
    plan = machines_from_backend(shop_id, days)
    with S.conn() as c:
        units = [dict(r) for r in c.execute(
            "SELECT id, name, kind, locked FROM units WHERE shop_id=? AND active=1", (shop_id,))]
        cmap: dict = {}
        for r in c.execute(
                "SELECT m.comm_id, m.unit_id FROM comms m JOIN units u ON u.id=m.unit_id "
                "WHERE u.shop_id=?", (shop_id,)):
            cmap.setdefault(r["unit_id"], set()).add(str(r["comm_id"]))

    rows = []
    for m in plan["machines"]:
        comms = [c["comm"] for c in m["comms"]]
        pos = [c["pos"] for c in m["comms"]]
        hit = next((u for u in units if cmap.get(u["id"], set()) & set(comms)), None)
        if not hit:
            hit = next((u for u in units if (u["name"] or "").strip() == m["name"]), None)
        local, status = None, "new"
        if hit:
            uc = sorted(cmap.get(hit["id"], set()))
            same = ((hit["name"] or "").strip() == m["name"] and set(uc) == set(comms))
            status = "same" if same else "update"
            local = {"unit_id": hit["id"], "name": hit["name"], "kind": hit["kind"],
                     "comms": uc, "locked": bool(hit["locked"])}
        rows.append({"name": m["name"], "kind": m["kind"], "team": m.get("no") or "",
                     "comms": comms, "pos": pos, "status": status, "local": local,
                     "local_name": (local or {}).get("name"),
                     "local_comms": (local or {}).get("comms") or [],
                     "merge": bool(local and len(comms) > len(local.get("comms") or []))})

    all_comms = {c["comm"] for m in plan["machines"] for c in m["comms"]}
    orphan = []
    for u in units:
        uc = cmap.get(u["id"], set())
        if uc and not (uc & all_comms):
            orphan.append({"unit_id": u["id"], "name": u["name"], "kind": u["kind"],
                           "comms": sorted(uc)})
    return {"ok": True, "rows": rows, "orphan": orphan,
            "unknown": LAST_SKIPPED,
            "pos_have": sum(1 for m in plan["machines"] for c in m["comms"] if c["pos"]),
            "devices": len(plan["machines"]), "days": days, "provider": plan["provider"]}


def diff_against_db(shop_id: str, plan: dict) -> dict:
    """和库里现状比：将新建机台 / 将改名 / 新增卡头 / 卡头挪机台 / 缺 P 位 / 库里有后台没有。"""
    import server as S
    with S.conn() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT u.id, u.name, u.pos, u.machine_id, m.name AS mname, "
            "  (SELECT comm_id FROM comms WHERE unit_id=u.id ORDER BY comm_id LIMIT 1) AS comm "
            "FROM units u LEFT JOIN machines m ON m.id=u.machine_id "
            "WHERE u.shop_id=? ORDER BY u.id", (shop_id,))]
    db_by_comm = {r["comm"]: r for r in rows if r["comm"]}
    plan_comms = {c["comm"] for m in plan["machines"] for c in m["comms"]}
    new_machines, new_comms, moved, renamed = [], [], [], []
    for m in plan["machines"]:
        for c in m["comms"]:
            db = db_by_comm.get(c["comm"])
            if not db:
                new_comms.append((m["name"], c["comm"], c["pos"]))
                continue
            if (db["mname"] or "") != m["name"]:
                moved.append((c["comm"], db["mname"], m["name"]))
            if c["pos"] and (db["pos"] or 0) != (_posnum(c["pos"]) or 0):
                renamed.append((c["comm"], db["mname"], db["pos"], c["pos"]))
    extra = [(r["mname"] or "?", r["comm"]) for r in rows
             if r["comm"] and r["comm"] not in plan_comms]
    return {"machines": len(plan["machines"]), "comms": len(plan_comms),
            "new_comms": new_comms, "moved": moved, "pos_shift": renamed,
            "missing_pos": plan["missing_pos"], "db_only": extra,
            "db_units": len(rows)}
