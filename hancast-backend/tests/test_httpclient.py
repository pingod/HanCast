"""HTTP 客户端适配层（utils/httpclient.py）— 全部本地回环，不触网

覆盖:
- UA → TLS 指纹映射：认得出的浏览器给对应指纹，认不出的一律不伪装
- 会话工厂：装了 curl_cffi 且参数开着才走它，否则退回 requests
- requests 语义对齐（curl_cffi 的四处分歧都会在这里翻车）：
  ``with r:``、非流式 ``iter_content``、``cookies`` 迭代出带域名的 Cookie 对象、
  异常翻译成 ``requests.RequestException``
- 代理坏了自动直连（与搬走前的 ResilientSession 行为一致）
- 真实解析路径（``ShareResolver._open``）用新会话能抓出正文
"""

import shutil
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from unittest.mock import patch

import requests

from hancast_sidecar import params
from hancast_sidecar.utils import httpclient as hc

CHROME124_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
IPHONE17_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
               "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
               "Mobile/15E148 Safari/604.1")
BODY = b"#EXTM3U\n" + b"x" * 200


class _Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Stub(unittest.TestCase):
    """本地小站：/page 下发 cookie 后 302 到 /video，/video 发正文"""

    def setUp(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                outer.seen.append((self.path, dict(self.headers)))
                if self.path == "/page":
                    self.send_response(302)
                    self.send_header("Location", f"/video")
                    self.send_header("Set-Cookie", "page=1; Path=/")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Set-Cookie", "video=2; Path=/")
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Content-Length", str(len(BODY)))
                self.end_headers()
                self.wfile.write(BODY)

            def log_message(self, *args):
                pass

        self.seen = []
        self.httpd = _Server(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.shutdown)
        self.addCleanup(self.httpd.server_close)

    def sessions(self):
        """工厂可能给出的两种会话都过同一套 requests 语义"""
        out = []
        if hc.available():
            out.append(hc.CurlSession())
        out.append(hc.ResilientSession())
        return out


class patch_param:
    """临时改后台参数（参数表是进程级单例，用完必须还原）"""

    def __init__(self, key, value):
        self.key, self.value = key, value

    def __enter__(self):
        self._saved = dict(params._overrides)
        params._overrides[self.key] = self.value
        return self

    def __exit__(self, *exc):
        params._overrides.clear()
        params._overrides.update(self._saved)
        return False


class TestFingerprintMapping(_Stub):
    def test_known_browsers(self):
        self.assertEqual(hc.fingerprint_for(CHROME124_UA), "chrome124")
        self.assertEqual(hc.fingerprint_for(IPHONE17_UA), "safari172_ios")
        self.assertEqual(hc.fingerprint_for(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"),
            "edge101")
        self.assertEqual(hc.fingerprint_for(
            "Mozilla/5.0 (Linux; Android 13; SM-G991B) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36"),
            "chrome131_android")

    def test_never_claims_newer_than_the_handshake(self):
        # UA 比所有可用指纹都旧 → 不伪装，而不是硬套一个更新的
        self.assertEqual(hc.fingerprint_for(
            "Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 "
            "Chrome/60.0 Safari/537.36"), "")

    def test_unknown_ua_is_not_spoofed(self):
        for ua in ("", "hancast-import/1.0", "curl/8.4.0",
                   "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:100.0) "
                   "Gecko/20100101 Firefox/100.0"):
            self.assertEqual(hc.fingerprint_for(ua), "", ua)

    def test_switch_off_disables_everything(self):
        with patch_param("http.tls_impersonate", False):
            self.assertEqual(hc.fingerprint_for(CHROME124_UA), "")


class TestSessionFactory(_Stub):
    def test_curl_when_available_and_enabled(self):
        if not hc.available():
            self.skipTest("本机未安装 curl_cffi")
        self.assertIsInstance(hc.make_session(), hc.CurlSession)

    def test_falls_back_when_param_off(self):
        with patch_param("http.tls_impersonate", False):
            self.assertIsInstance(hc.make_session(), hc.ResilientSession)

    def test_falls_back_when_library_missing(self):
        with patch.object(hc, "_CURL_OK", False):
            self.assertIsInstance(hc.make_session(), hc.ResilientSession)


class TestRequestsParity(_Stub):
    def test_headers_keep_our_ua(self):
        for session in self.sessions():
            self.seen.clear()
            with session.get(self.base + "/video", timeout=5,
                             headers={"User-Agent": CHROME124_UA,
                                      "Accept": "*/*"}) as r:
                self.assertEqual(r.status_code, 200)
            sent = self.seen[-1][1]
            self.assertEqual(sent.get("User-Agent"), CHROME124_UA)
            self.assertEqual(sent.get("Accept"), "*/*")   # 显式头不能被默认头盖掉

    def test_impersonation_brings_browser_side_headers(self):
        """指纹是否真挂上：curl_cffi 只在 impersonate 时补浏览器那套 Accept-Language"""
        if not hc.available():
            self.skipTest("本机未安装 curl_cffi")
        self.seen.clear()
        hc.CurlSession().get(self.base + "/video", timeout=5,
                             headers={"User-Agent": CHROME124_UA})
        self.assertIsNotNone(self.seen[-1][1].get("Accept-Language"))
        self.seen.clear()
        hc.CurlSession().get(self.base + "/video", timeout=5,
                             headers={"User-Agent": "bot/1.0"})
        self.assertIsNone(self.seen[-1][1].get("Accept-Language"))

    def test_iter_content_both_modes(self):
        for session in self.sessions():
            streamed = session.get(self.base + "/video", timeout=5, stream=True)
            plain = session.get(self.base + "/video", timeout=5, stream=False)
            chunked = b"".join(streamed.iter_content(chunk_size=64))
            whole = b"".join(plain.iter_content(chunk_size=64 * 1024))
            streamed.close()
            plain.close()
            self.assertEqual(chunked, whole)
            self.assertEqual(whole, BODY)

    def test_cookies_carry_domains_through_redirects(self):
        """沙箱 _harvest_cookies 要 .domain/.name/.value —— 只有名字会静默丢 cookie"""
        for session in self.sessions():
            jar = getattr(session, "_jar", None) or session.cookies
            r = session.get(self.base + "/page", timeout=5,
                            headers={"User-Agent": CHROME124_UA})
            names = {c.name: (c.domain, c.value) for c in r.cookies}
            self.assertEqual(names["video"][1], "2")
            self.assertTrue(names["video"][0], "响应 cookie 必须有域名")
            self.assertIn("page", {c.name for h in r.history for c in h.cookies})
            self.assertIn("page", {c.name for c in jar})
            # 下一次请求要带上上一页的 cookie（防盗链站靠这条活着）
            self.seen.clear()
            session.get(self.base + "/video", timeout=5,
                        headers={"User-Agent": CHROME124_UA})
            self.assertIn("page=1", self.seen[-1][1].get("Cookie", ""))
            r.close()

    def test_network_errors_become_requests_errors(self):
        for session in self.sessions():
            with self.assertRaises(requests.exceptions.ConnectionError):
                session.get("http://127.0.0.1:1/dead", timeout=2)

    def test_proxy_broken_falls_back_to_direct(self):
        """代理端口没人听时，解析必须还能成，而不是整条链报错

        显式把 proxies 指到一个死端口（不靠环境变量：127.0.0.1 常在系统代理例外里，
        那样两种会话都没真正走过代理，测试会假绿）。
        """
        dead = {"http": "http://127.0.0.1:1", "https": "http://127.0.0.1:1"}
        for session in self.sessions():
            r = session.get(self.base + "/video", timeout=6, proxies=dict(dead),
                            headers={"User-Agent": CHROME124_UA})
            self.assertEqual(r.status_code, 200)
            r.close()
            self.assertTrue(getattr(session, "_bypassed", False),
                            f"{type(session).__name__} 应记下「已改直连」，后续请求不再试代理")
            again = session.get(self.base + "/page", timeout=6,
                                headers={"User-Agent": CHROME124_UA})
            self.assertEqual(again.status_code, 200)
            again.close()


class TestResolvePath(_Stub):
    def test_open_uses_new_session(self):
        """真实解析入口拿 make_session() 抓页 —— 适配层的最终验收点"""
        from hancast_sidecar.media.share_resolver import ShareResolver

        rules_dir = tempfile.mkdtemp(prefix="hancast-httpclient-")
        self.addCleanup(lambda: shutil.rmtree(rules_dir, ignore_errors=True))
        resolver = ShareResolver(media_server=None, rules_dir=rules_dir)
        final_url, ctype, _length, body = resolver._open(
            self.base + "/page", deadline=time.monotonic() + 10,
            ua=CHROME124_UA, session=hc.make_session())
        self.assertTrue(final_url.endswith("/video"), final_url)
        self.assertIn("mpegurl", ctype)
        self.assertTrue(body.startswith(b"#EXTM3U"))


if __name__ == "__main__":
    unittest.main()
