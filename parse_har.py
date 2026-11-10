# -*- coding: utf-8 -*-
"""解析 HAR：列出所有请求，标出与「机台消费币汇总」「机台获分记录」相关的接口。"""
import io, json, re

HAR = r"C:\Users\Tuilge\Desktop\md.xhyun.vip4推币机码表.har"
OUT = r"C:\Users\Tuilge\Desktop\码表线上化\har_dump.txt"

buf = io.StringIO()
def P(*a): print(*a, file=buf)

with open(HAR, encoding="utf-8") as f:
    har = json.load(f)

entries = har["log"]["entries"]
P(f"共 {len(entries)} 条请求\n")

KEY = re.compile(r"consum|payout|score|coin|machine|report|terminal|interdock|datahandler", re.I)

rows = []
for i, e in enumerate(entries):
    req = e["request"]
    url = req["url"]
    method = req["method"]
    path = re.sub(r"^https?://[^/]+", "", url.split("?")[0])
    q = url.split("?")[1] if "?" in url else ""
    post = ""
    pd = req.get("postData") or {}
    if pd.get("text"):
        post = pd["text"]
    resp = e["response"]
    body = (resp.get("content") or {}).get("text") or ""
    rows.append(dict(i=i, method=method, path=path, query=q, post=post,
                     status=resp["status"], size=len(body), body=body,
                     mime=(resp.get("content") or {}).get("mimeType", "")))

P("=" * 100)
P("【全部请求一览】")
for r in rows:
    flag = "*" if KEY.search(r["path"]) else " "
    P(f"{flag}[{r['i']:03d}] {r['method']:4} {r['status']} {r['size']:>8}B  {r['path']}"
      + (f"?{r['query'][:80]}" if r["query"] else ""))

P("\n" + "=" * 100)
P("【命中关键字的接口：请求体 + 响应片段】")
for r in rows:
    if not KEY.search(r["path"]):
        continue
    P("\n" + "-" * 100)
    P(f"[{r['i']:03d}] {r['method']} {r['path']}")
    if r["query"]:
        P(f"  query: {r['query'][:300]}")
    if r["post"]:
        P(f"  body : {r['post'][:600]}")
    P(f"  resp : {r['status']} {r['size']}B {r['mime']}")
    if r["body"]:
        P("  " + r["body"][:900].replace("\n", " "))

with open(OUT, "w", encoding="utf-8") as f:
    f.write(buf.getvalue())
print("written", OUT)
