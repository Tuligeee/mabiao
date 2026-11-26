# -*- coding: utf-8 -*-
"""无头验证：返回键是否逐级退、退到顶是否被挡一次。

用真 app.js（预览台那套 fetch 桩），走 http 起服务，避免 file:// 限制 pushState。
"""
import io
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

D = r"C:\Users\Tuilge\Desktop\码表线上化\_preview"
APP = r"C:\Users\Tuilge\Desktop\码表线上化\app"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
PORT = 8791

SYNC = ["index.html", "app.js", "app.css", "ui.js", "theme.css", "tokens.css"]


def sync():
    import shutil
    for f in SYNC:
        src = os.path.join(APP, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(D, f))


def load(name):
    with io.open(os.path.join(D, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


DATA = {
    "/api/me": load("me"),
    "/api/bootstrap": load("bootstrap"),
    "/api/entry": load("entry"),
    "/api/recon": load("recon"),
    "/api/kinds": load("kinds"),
    "/api/brand/list": load("brands"),
    "/api/manage/machines": load("machines"),
    "/api/manage/shops": load("shops"),
    "/api/overview": load("overview"),
    "/api/bd/status": load("bdstatus"),
    "/api/manage/devices": load("devices"),
}

STUB = """
<script>
window.__DATA__ = __DATA_JSON__;
(function(){
  window.fetch = function(url){
    var p = String(url);
    var m = p.match(/\\/api\\/[A-Za-z0-9_\\/-]+/);
    var key = m ? m[0] : p;
    var body = window.__DATA__[key];
    return Promise.resolve(new Response(JSON.stringify(body === undefined ? {} : body),
      {status:200, headers:{'Content-Type':'application/json'}}));
  };
})();
</script>
""".replace("__DATA_JSON__", json.dumps(DATA, ensure_ascii=False))

TEST = """
<script>
(function(){
  var out = [];
  function vis(){
    var ks = ['home','entry','recon','overview','settings','scan'];
    for (var i = 0; i < ks.length; i++) {
      var n = document.getElementById('view-' + ks[i]);
      if (n && !n.hidden) return ks[i];
    }
    return '?';
  }
  function sleep(ms){ return new Promise(function(r){ setTimeout(r, ms); }); }
  function nav(v){ var b = document.querySelector('.nav__item[data-view="' + v + '"]'); if (b) b.click(); }
  function beat(t){ new Image().src = '/__result?d=' + encodeURIComponent(t); }
  window.addEventListener('error', function(e){ beat('ERR:' + (e.message || e.type)); });
  window.addEventListener('unhandledrejection', function(e){
    beat('REJ:' + ((e.reason && (e.reason.message || e.reason)) || '?'));
  });
  function step(name, fn){ try { fn(); } catch (err) { beat('THROW@' + name + ':' + err.message); throw err; } }
  window.__hb = setInterval(function(){ beat(out.join('|') || 'idle'); }, 600);
  (async function(){
    out.push('started');
    await sleep(1500);
    out.push('boot=' + vis());
    step('navRecon', function(){ nav('recon'); });
    await sleep(400); out.push('goRecon=' + vis());
    history.back(); await sleep(500);
    out.push('back1=' + vis());
    nav('settings');await sleep(400); out.push('goSettings=' + vis());
    history.back(); await sleep(500); out.push('back2=' + vis());
    history.back(); await sleep(600); out.push('back3=' + vis());
    out.push('hash=' + location.hash);
    clearInterval(window.__hb);
    var line = out.join('|');
    document.title = 'RESULT|' + line;
    new Image().src = '/__result?d=' + encodeURIComponent(line);
  })();
})();
</script>
"""


def build():
    html = io.open(os.path.join(D, "index.html"), encoding="utf-8").read()
    html = html.replace("</head>", STUB + "</head>", 1)
    html = html.replace("</body>", TEST + "</body>", 1)
    p = os.path.join(D, "hist_harness.html")
    io.open(p, "w", encoding="utf-8").write(html)
    return p


RESULT = {}


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/__result'):
            from urllib.parse import urlparse, parse_qs
            q = parse_qs(urlparse(self.path).query)
            RESULT['text'] = (q.get('d') or [''])[0]
            self.send_response(204)
            self.end_headers()
            return
        return SimpleHTTPRequestHandler.do_GET(self)

    def log_message(self, *a):
        pass


def serve():
    os.chdir(D)
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return httpd


def main():
    sync()
    build()
    httpd = serve()
    time.sleep(0.5)
    png = os.path.join(D, "hist_shot.png")
    prof = os.path.join(D, "_chrome1")
    cmd = [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
           "--user-data-dir=" + prof,
           "--window-size=1280,900", "--virtual-time-budget=15000",
           "--screenshot=" + png,
           "http://127.0.0.1:%d/hist_harness.html" % PORT]
    try:
        subprocess.run(cmd, capture_output=True, timeout=70)
    except subprocess.TimeoutExpired:
        print("(chrome 超时，看已经回传的结果)")
    time.sleep(0.5)
    httpd.shutdown()
    httpd.server_close()
    text = RESULT.get("text") or "(没拿到结果)"
    print("结果:", text)
    parts = dict(x.split("=", 1) for x in text.split("|") if "=" in x)
    checks = [
        ("开局在导航", parts.get("boot") == "home"),
        ("点核对能进", parts.get("goRecon") == "recon"),
        ("返回退回导航", parts.get("back1") == "home"),
        ("点设置能进", parts.get("goSettings") == "settings"),
        ("返回退回导航", parts.get("back2") == "home"),
        ("再返回仍停在导航（提示退出）", parts.get("back3") == "home"),
        ("地址栏带视图名", (parts.get("hash") or "").startswith("#")),
    ]
    ok = 0
    for name, good in checks:
        print(("  PASS " if good else "  FAIL ") + name)
        ok += 1 if good else 0
    # 第二轮：带 #settings 直接打开，验证刷新/直达能落到原页
    RESULT.clear()
    httpd2 = serve()
    time.sleep(0.4)
    prof2 = os.path.join(D, "_chrome2")
    cmd2 = [CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars",
            "--user-data-dir=" + prof2,
            "--window-size=1280,900", "--virtual-time-budget=12000",
            "--screenshot=" + os.path.join(D, "hist_shot2.png"),
            "http://127.0.0.1:%d/hist_harness.html#settings" % PORT]
    try:
        subprocess.run(cmd2, capture_output=True, timeout=70)
    except subprocess.TimeoutExpired:
        pass
    time.sleep(0.4)
    httpd2.shutdown()
    httpd2.server_close()
    t2 = RESULT.get("text") or ""
    p2 = dict(x.split("=", 1) for x in t2.split("|") if "=" in x)
    deep = p2.get("boot") == "settings"
    print(("  PASS " if deep else "  FAIL ") + "带 #settings 打开直接落到设置页（实际 %r）" % p2.get("boot"))
    ok += 1 if deep else 0
    checks.append(("直达", deep))

    print("\n通过 %d / %d" % (ok, len(checks)))
    return 0 if ok == len(checks) else 1


if __name__ == "__main__":
    sys.exit(main())
