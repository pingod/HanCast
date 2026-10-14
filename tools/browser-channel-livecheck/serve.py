#!/usr/bin/env python3
"""浏览器通道验收用的假站点：播放地址只由页面 JS 挂上去，HTTP 抓页永远看不到。

一条命令验的是整条链，不是某一环：

    规则包 host.sniff/host.render → sidecar browser_request 事件 → Rust 离屏 WebView
    → 注入钩子 → 加载完再等 settle → 回收 HTML/地址 → cookies_for_url（HttpOnly）
    → browser_result 命令 → 唤醒解析线程 → Cookie 双罐同步

判据（都成立才算通道通了）：
  1. 嗅到 /play/master.m3u8——这个地址在 HTML 里不存在，只有真渲染才拿得到；
  2. host.cookies 里出现 hcvisitor=webview-only——它是 HttpOnly，HTTP 侧抓不到，
     只有 WebView 罐子→解析会话这条同步链路成立才会出现。

只监听本机与局域网，不打外网。Ctrl-C 退出。

用法:
  hancast-backend/.venv/bin/python tools/browser-channel-livecheck/serve.py [--port 8099]
"""

import argparse
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

COOKIE = "hcvisitor=webview-only; Path=/; HttpOnly; SameSite=Lax"

# 地址由 JS 现挂（还带一个签名接口二段取址），照抄真实播放器的手路
PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>HanCast 浏览器通道验收页</title>
</head><body><video id="v" controls></video><div id="out">loading…</div>
<script>
setTimeout(function () {
  fetch('/api/sign?u=' + encodeURIComponent(location.pathname))
    .then(function (r) { return r.json(); })
    .then(function (d) {
      var v = document.getElementById('v');
      v.src = d.url;                       // 播放器拿到地址后挂 src —— 钩子就在这一步嗅
      document.getElementById('out').textContent = d.url;
    });
}, %d);
</script></body></html>""" % 400

PLAYLIST = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n"
            "#EXTINF:1.0,\n/play/seg.ts\n#EXT-X-ENDLIST\n")


class Handler(BaseHTTPRequestHandler):
    server_version = "HCLiveCheck/1.0"

    def _send(self, status: int, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # 只在首次访问下发：验的就是「WebView 领到的券能不能搬进 HTTP 会话」
        self.send_header("Set-Cookie", COOKIE)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的命名
        path = self.path.split("?")[0]
        if path.startswith("/play/"):
            body = PLAYLIST.encode() if path.endswith(".m3u8") else b""
            self._send(200, "application/vnd.apple.mpegurl"
                       if path.endswith(".m3u8") else "video/mp2t", body)
        elif path.startswith("/api/"):
            import json
            sign = "sig-livecheck"
            hostport = self.headers.get("Host", "")
            url = f"http://{hostport}/play/master.m3u8?{sign}"
            self._send(200, "application/json", json.dumps({"url": url}).encode())
        else:
            self._send(200, "text/html; charset=utf-8", PAGE.encode())

    def log_message(self, fmt: str, *args) -> None:
        print(f"[serve] {self.address_string()} {fmt % args}", flush=True)


def lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8099)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"验收页: http://127.0.0.1:{args.port}/  （局域网: http://{lan_ip()}:{args.port}/）")
    print("把上面这个地址粘进 HanCast 主界面点「粘贴」，判据看应用日志里的 LIVECHECK 行")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[serve] 退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
