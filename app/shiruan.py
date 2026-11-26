# -*- coding: utf-8 -*-
"""世软云 POS（cloud.zs-shiruan.cn / data.zs-shiruan.cn）机台收币取数。

零鉴权：只需要 Host 头，纯 HTTP POST，没有 token / 签名 / 登录。
接口：DataHandleProxy/Request → uploaddata.GameMachineData.GameMachineDataSumMachNo
（报表 → 游戏机数据查询 → 按机位汇总）

口径：
  BusyDate      营业日期（不是实际时间）
  StoreID       世软门店编号，南昌大悦城是 0696873
  SortName      游戏机分类，只取「娱乐机」——这家店里娱乐机全是推币机
  Mach_No       机位号，回去对码表的机台名
  Coin_Num      投币数
  Times         投币次数（交易笔数，不是币数）
"""
import datetime
import hashlib
import json
import urllib.error
import urllib.request

API = "https://data.zs-shiruan.cn/v1/api/data/DataHandleProxy/Request"
DATA_URL = "uploaddata.GameMachineData.GameMachineDataSumMachNo"
SORT_PUSHER = "娱乐机"          # 推币机都挂在这个分类下


class ShiruanError(RuntimeError):
    pass


HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Sr-Language": "zh-cn",
    "Origin": "https://data.zs-shiruan.cn",
    "Referer": "https://data.zs-shiruan.cn/BICustome/BusinessDataQuery",
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"),
}


def _now_iso() -> str:
    """和 JS new Date().toISOString() 一致：UTC、毫秒 3 位、末尾 Z。"""
    t = datetime.datetime.now(datetime.timezone.utc)
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (t.microsecond // 1000)


def sign_of(busy: str, data: str, sid: str = "") -> str:
    """Sign = MD5('BusyDate={busy}&Data={data}&StoreID={sid}&key={sid}').upper()"""
    src = "BusyDate=%s&Data=%s&StoreID=%s&key=%s" % (busy, data, sid, sid)
    return hashlib.md5(src.encode("utf-8")).hexdigest().upper()


def _post(payload: dict, timeout: int = 60) -> dict:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(API, data=body, method="POST", headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise ShiruanError("世软接口 HTTP %s" % e.code)
    except urllib.error.URLError as e:
        raise ShiruanError("世软网络异常：%s" % (e.reason,))


def day_summary(day: str, store_id: str, sort_name: str = SORT_PUSHER,
                limit: int = 2000) -> list:
    """一天一店的机台收币 → [{machine, coins, count}]。

    day 用营业日（YYYY-MM-DD）。
    """
    inner = {
        "Url": DATA_URL,
        "Body": {
            "StoreID": [str(store_id)],
            "Skip": 0,
            "Limit": limit,
            "Order": "",
            "AccountCode": "",
            "Filters": {
                "BusyDate": {"Operator": "in", "Values": [day]},
                "DaTime": {"Operator": "between", "Values": []},
                "Mach_No": {"Operator": "contain", "Values": []},
                "MMachNo": {"Operator": "in", "Values": []},
                "RevenueSort": {"Operator": "in", "Values": []},
                "SortName": {"Operator": "in", "Values": [sort_name] if sort_name else []},
                "Leag_No": {"Operator": "eq", "Values": []},
                "IsUnUseMach": {"Operator": "eq", "Values": ["FALSE"]},
            },
        },
    }
    inner["Method"] = "POST"
    data = json.dumps(inner, ensure_ascii=False, separators=(",", ":"))
    busy = _now_iso()          # 签名用的是当前时间，不是营业日
    payload = {"BusyDate": busy, "StoreID": "", "Data": data,
               "Sign": sign_of(busy, data, "")}
    resp = _post(payload)
    if resp.get("Exstatus") != 200:
        raise ShiruanError("世软返回 %s：%s" % (resp.get("Exstatus"), str(resp.get("Exmsg"))[:120]))
    rows = (resp.get("Data") or {}).get("Data") or []
    out = []
    for r in rows:
        mach = str(r.get("Mach_No") or "").strip()
        if not mach:
            continue
        out.append({
            "machine": mach,
            "coins": float(r.get("Coin_Num") or 0),
            "count": float(r.get("Times") or 0),
        })
    out.sort(key=lambda x: x["machine"])
    return out


def machines(day: str, store_id: str, sort_name: str = SORT_PUSHER) -> list:
    """只取机位号清单（导入机台用）。"""
    return [x["machine"] for x in day_summary(day, store_id, sort_name)]
