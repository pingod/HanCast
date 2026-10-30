"""交付给电视的直链必须带播放器可识别的后缀（2026-10-28 现场取证）

现场：同一条抖音分享，投到另一台 HanCast（mpv 渲染）正常，投到 TCL 85T8G Max
「点了没反应」。真机逐变量对照（本机起 Range 可懂的 HTTP 服务，只改 URL 形态，
字节与 Content-Type 全同），电视侧只看它自己有没有发起 GET：

    /ext/test.mp4              → 有 GET，播得动
    /proxy/a1b2c3.mp4          → 有 GET，播得动
    /proxy/a1b2c3.ts           → 有 GET，播得动（MP4 字节挂 .ts 也肯拉）
    /ext/test.mp4?vid=1&t=2    → 有 GET，播得动
    /noext/stream?vid=1&t=2    → **一次 GET 都没有**，带满 DIDL 也一样

即这台电视无视 DIDL 的 protocolInfo，只按 URL 后缀决定要不要发起拉流。而抖音
CDN 的直链路径正是 `…/video/tos/cn/tos-cn-ve-15/<hash>/`（无后缀），本机 curl
却拉得到 206 —— 于是 `_tv_can_fetch` 判定「电视能拉」，把无后缀直链原样交付，
电视根本不碰，会话只在轮询里看到它自己回到 STOPPED。

同轮查出的第二个口子：`DeviceCastSession.play()` 收了 `mime_type` 却没往下传，
媒体投屏的 `CurrentURIMetaData` 恒为空串（本例不是致因，但对按 protocolInfo
选解码栈的渲染端是实打实的信息缺失）。
"""

import threading
import time
import unittest
from unittest.mock import patch

import requests

from hancast_sidecar.media.server import player_ext_ok, player_safe_suffix
from hancast_sidecar.media.share_resolver import ShareResolver
from hancast_sidecar.models.device import Device
from hancast_sidecar.models.session import DeviceCastSession

# 现场那条直链的形状（去掉签名参数，只留路径形态）
DOUYIN_URL = ("https://2408-8722-3000-2-0-0-0-fa-neaa216e.free-lbv6.idouyinvod.com"
              "/v6-cold.douyinvod.com/3d6f3347/6ae19d0c/video/tos/cn/tos-cn-ve-15/"
              "oACb7iGFwfieP4oIhtEYEWzIge6LQBV1BIAACV/?a=1128&br=784")
BILI_URL = "http://cn-sccd-ct-02-07.bilivideo.com/upgcxcode/48/12/1234/1234-1-360.mp4" \
           "?e=ig&b=20000&ua=tvproj"


class FakeMediaServer:
    def __init__(self):
        self.registered: list[tuple[str, str]] = []

    def register_proxy(self, remote_url, headers=None, suffix=""):
        self.registered.append((remote_url, suffix))
        return f"http://192.168.1.5:61482/proxy/h{len(self.registered)}{suffix}"


def _resolver():
    import os
    import tempfile
    return ShareResolver(FakeMediaServer(), rules_dir=tempfile.mkdtemp())


class TestSuffixGate(unittest.TestCase):
    """可播性判定必须站在电视的取值习惯上，不是站在「本机能不能拉」上"""

    def test_extensionless_url_is_not_tv_safe(self):
        r = _resolver()
        with patch.object(ShareResolver, "_peek",
                          return_value=(b"\x00\x00\x00\x18ftypmp42", "video/mp4", 206)):
            self.assertFalse(r._tv_can_fetch(DOUYIN_URL, "video/mp4",
                                             time.monotonic() + 20))

    def test_suffixed_url_still_direct(self):
        r = _resolver()
        with patch.object(ShareResolver, "_peek",
                          return_value=(b"\x00\x00\x00\x18ftypmp42", "video/mp4", 206)):
            self.assertTrue(r._tv_can_fetch(BILI_URL, "video/mp4",
                                            time.monotonic() + 20))

    def test_master_playlist_gate_does_not_fire(self):
        """主列表分支不能被后缀门截走（子列表交给代理是另一条既有判据）"""
        r = _resolver()
        master = (b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=1280x720\n"
                  b"https://cdn.example/720.m3u8\n")
        playlist = "https://live.example.com/api/playlist?id=9"

        def fake_peek(url, deadline, use_range=True):
            return (master if url == playlist else b"G\x47\x00\x10",
                    "application/vnd.apple.mpegurl", 200)

        with patch.object(ShareResolver, "_peek", side_effect=fake_peek):
            self.assertTrue(r._tv_can_fetch(
                playlist, "application/vnd.apple.mpegurl", time.monotonic() + 20))

    def test_dirty_mime_playlist_body_stays_direct(self):
        """正文是列表但 MIME 报脏：不能被后缀门推进代理再补成 .mp4，等于把列表骗成视频"""
        r = _resolver()
        master = (b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\n"
                  b"https://cdn.example/720.m3u8\n")
        with patch.object(ShareResolver, "_peek", return_value=(master, "text/plain", 200)):
            self.assertTrue(r._tv_can_fetch("https://live.example.com/api/playlist?id=9",
                                            "text/plain", time.monotonic() + 20))

    def test_video_url_without_ext_still_gated(self):
        """上一条的例外只护列表正文，普通视频字节照门走"""
        r = _resolver()
        with patch.object(ShareResolver, "_peek",
                          return_value=(b"\x00\x00\x00\x18ftypmp42", "text/plain", 200)):
            self.assertFalse(r._tv_can_fetch(DOUYIN_URL, "text/plain",
                                             time.monotonic() + 20))


class TestPublishUsesProxyForExtensionless(unittest.TestCase):
    """门判下来的地址要落到代理，并且代理路径带上容器相符的后缀"""

    def test_douyin_shape_hands_tv_a_suffixed_proxy_url(self):
        r = _resolver()
        with patch.object(ShareResolver, "_peek",
                          return_value=(b"\x00\x00\x00\x18ftypmp42", "video/mp4", 206)):
            uri, mime, via = r._publish(DOUYIN_URL, "video/mp4", requests.Session(), "",
                                        time.monotonic() + 20, "hancast-douyin")
        self.assertNotEqual(uri, DOUYIN_URL)
        self.assertTrue(uri.endswith(".mp4"), uri)
        self.assertTrue(via.endswith("+proxy"))
        self.assertEqual(r.media_server.registered, [(DOUYIN_URL, ".mp4")])

    def test_suffixed_url_stays_direct(self):
        r = _resolver()
        with patch.object(ShareResolver, "_peek",
                          return_value=(b"\x00\x00\x00\x18ftypmp42", "video/mp4", 206)):
            uri, _, via = r._publish(BILI_URL, "video/mp4", requests.Session(), "",
                                     time.monotonic() + 20, "hancast-bilibili")
        self.assertEqual(uri, BILI_URL)
        self.assertNotIn("+proxy", via)
        self.assertEqual(r.media_server.registered, [])

    def test_tight_budget_skips_the_check_entirely(self):
        """预算见底仍按直链交付（既有行为，别把检查变成新的失败面）"""
        r = _resolver()
        with patch.object(ShareResolver, "_peek",
                          side_effect=AssertionError("不该发请求")):
            uri, _, via = r._publish(DOUYIN_URL, "video/mp4", requests.Session(), "",
                                     time.monotonic() + 1.0, "hancast-douyin")
        self.assertEqual(uri, DOUYIN_URL)


class TestPlayerSafeSuffixByMime(unittest.TestCase):
    def test_mime_used_only_when_url_has_no_compliant_ext(self):
        self.assertEqual(player_safe_suffix(DOUYIN_URL, "video/mp4"), ".mp4")
        self.assertEqual(player_safe_suffix("https://f.cdn/proxy/abcdef", "video/mp4"),
                         ".mp4")
        self.assertEqual(player_safe_suffix("https://f.cdn/x/y_1.png", "video/mp4"),
                         ".mp4")

    def test_codecs_parameter_tolerated(self):
        self.assertEqual(player_safe_suffix("https://f.cdn/a/b/",
                                            "video/mp4; codecs=\"avc1.42E01E\""), ".mp4")

    def test_remote_ext_wins_over_mime(self):
        """后缀本来就合规时不改它，字节形状不该被 MIME 猜错带偏"""
        self.assertEqual(player_safe_suffix("https://f.cdn/x/init.mp4", "video/quicktime"),
                         ".mp4")
        self.assertEqual(player_safe_suffix("https://f.cdn/x/y_1.ts?k=a", "video/mp4"),
                         ".ts")

    def test_empty_and_unknown_mime_falls_back_to_ts(self):
        self.assertEqual(player_safe_suffix("https://f.cdn/proxy/abcdef"), ".ts")
        self.assertEqual(player_safe_suffix("https://f.cdn/proxy/abcdef", ""), ".ts")
        self.assertEqual(player_safe_suffix("https://f.cdn/proxy/abcdef",
                                            "application/octet-stream"), ".ts")

    def test_playlist_never_goes_through_the_mime_path(self):
        self.assertEqual(player_safe_suffix("https://f.cdn/api/m3u8?id=1", "video/mp4"),
                         ".m3u8")

    def test_playlist_by_mime_not_only_by_path(self):
        """路径无后缀、靠 MIME 声明的播放列表也得给 .m3u8

        此前这一支把 mime 参数丢了（`_looks_like_playlist("", url)`），
        于是「mpegurl 类型 + 无后缀路径」被兜底成 .ts —— 代理出去等于给电视
        一个假装成 TS 的播放列表，照旧播不了。2026-10-30 由
        `tools/verify-tv-url-suffix.py` 的矩阵抓出来。
        """
        for mime in ("application/vnd.apple.mpegurl",
                     "application/x-mpegURL",
                     "audio/mpegurl"):
            with self.subTest(mime=mime):
                self.assertEqual(
                    player_safe_suffix("https://f.cdn/live/master", mime), ".m3u8")


class TestTvExtOkShape(unittest.TestCase):
    def test_trailing_slash_and_bare_name_both_fail(self):
        self.assertFalse(player_ext_ok(DOUYIN_URL))
        self.assertFalse(player_ext_ok("https://f.cdn/m3u8"))
        self.assertFalse(player_ext_ok("https://f.cdn/a/b/"))

    def test_query_and_fragment_forms_pass(self):
        self.assertTrue(player_ext_ok(BILI_URL))
        self.assertTrue(player_ext_ok("https://f.cdn/a.mp4?x=1"))
        self.assertTrue(player_ext_ok("https://f.cdn/a.mp4#t=0,10"))


class _RecordingProtocol:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def play(self, media_uri, metadata=""):
        self.calls.append((media_uri, metadata))

    def get_state_position(self):
        return "00:00:01"

    def get_state_duration(self):
        return "00:04:55"


def _session() -> DeviceCastSession:
    dev = Device(id="tv-1", name="TCL 85T8G Max", device_type="tv",
                 ip="192.168.1.13", port=8080, status="online")
    s = DeviceCastSession.__new__(DeviceCastSession)
    s.device = dev
    s.device_id = dev.id
    s.protocol = _RecordingProtocol()
    s.media_server = FakeMediaServer()
    s.cast_state = type("CS", (), {"media": None, "status": "", "position": "",
                                   "duration": ""})()
    s._poll_lock = threading.Lock()
    s.stop_poll = lambda: None
    s.start_poll = lambda: None
    return s


class TestMimeTypeReachesTheTv(unittest.TestCase):
    """`mime_type` 收进来不能就地断掉：媒体投屏也该下发 protocolInfo"""

    def test_video_mime_builds_didl(self):
        s = _session()
        s.play(BILI_URL, "video/mp4")
        uri, metadata = s.protocol.calls[0]
        self.assertIn("DIDL-Lite", metadata)
        self.assertIn('protocolInfo="http-get:*:video/mp4:*"', metadata)
        # res 里的地址按 XML 转义（& → &amp;），断言按转义形态来
        self.assertIn(BILI_URL.replace("&", "&amp;"), metadata)

    def test_image_mime_keeps_metadata_empty(self):
        s = _session()
        s.play("http://192.168.1.5:61482/poster", "image/jpeg")
        self.assertEqual(s.protocol.calls[0][1], "")

    def test_caller_supplied_metadata_is_not_overwritten(self):
        s = _session()
        s.play("http://192.168.1.5:61482/live.m3u8", "application/vnd.apple.mpegurl",
               metadata="<DIDL-Lite>调用方自带</DIDL-Lite>")
        self.assertEqual(s.protocol.calls[0][1], "<DIDL-Lite>调用方自带</DIDL-Lite>")

    def test_unknown_mime_keeps_old_behaviour(self):
        s = _session()
        s.play(BILI_URL, "")
        self.assertEqual(s.protocol.calls[0][1], "")


if __name__ == "__main__":
    unittest.main()
