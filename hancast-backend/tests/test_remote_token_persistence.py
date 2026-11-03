"""遥控器页地址跨重启稳定（v2.4.10）—— 令牌必须落盘复用，不能每次启动换一段。

现场：同一台机器三次启动拿到三个不同 token（17:41:23 / 17:41:45 / 17:59:50），
旧 token 的 `/r/<token>/` 直接 404——而界面文案写的是「地址固定不变，可加书签」。
按那句话加书签的手机，第二天打开就是 404。根因是 token 只活在进程内存里
（`if not self.token: self.token = secrets.token_urlsafe(9)`），进程一换就没了。

注意：`CommandHandler()` 构造时就会起服务（DLNA/遥控器页），所以这些用例自带
「启动一次」的副作用，`cleanup()` 收尾。
"""
import json

import pytest

from hancast_sidecar.commands import CommandHandler
from hancast_sidecar.remote.server import RemoteControlServer


def _server(token: str, ip: str = "127.0.0.1") -> RemoteControlServer:
    return RemoteControlServer(get_state=lambda: {}, action=lambda *_: {}, local_ip=lambda: ip, token=token)


def test_构造时可注入令牌():
    assert _server("tok-A").token == "tok-A"
    assert _server("").token == ""


def test_地址里的令牌与注入的一致():
    s = _server("tok-E")
    s.port = 8899  # 只要端口有值 url 就成形，不需要真起服务
    assert s.url == "http://127.0.0.1:8899/r/tok-E/"


def test_首次启动后令牌自动落盘():
    h = CommandHandler()
    try:
        token = h.remote_server.token
        assert token, "首次启动应生成令牌"
        assert h.config.get("remote_control_token") == token, "生成的令牌必须写回配置"
        with open(h.config.setting_path, encoding="utf-8") as f:
            assert json.load(f)["remote_control_token"] == token, "配置文件里没落盘"
    finally:
        h.cleanup()


def test_再次启动复用同一令牌():
    h1 = CommandHandler()
    try:
        token = h1.remote_server.token
        assert token
    finally:
        h1.cleanup()
    h2 = CommandHandler()
    try:
        assert h2.remote_server.token == token, "重启后令牌变了，用户的书签就失效了"
    finally:
        h2.cleanup()


def test_配置里已有令牌时直接沿用(monkeypatch):
    h = CommandHandler()
    try:
        h.config.set("remote_control_token", "tok-preset")
        h.config.save()
    finally:
        h.cleanup()
    h2 = CommandHandler()
    try:
        assert h2.remote_server.token == "tok-preset"
    finally:
        h2.cleanup()


def test_令牌未变时不重复写盘(monkeypatch):
    h = CommandHandler()
    try:
        h.config.set("remote_control_token", h.remote_server.token or "tok-C")
        h.remote_server.token = h.config.get("remote_control_token")
        saved = []
        monkeypatch.setattr(h.config, "save", lambda: saved.append(1))
        h._remember_remote_token()
        assert saved == [], "令牌与配置一致时不该再写一次盘"
    finally:
        h.cleanup()


def test_空令牌不写入配置(monkeypatch):
    h = CommandHandler()
    try:
        h.config.set("remote_control_token", "")
        h.remote_server.token = ""
        saved = []
        monkeypatch.setattr(h.config, "save", lambda: saved.append(1))
        h._remember_remote_token()
        assert saved == []
        assert h.config.get("remote_control_token") == ""
    finally:
        h.cleanup()


def test_真起服务后地址里的令牌仍是注入的那个():
    s = _server("tok-F")
    if not s.start(port=0):
        pytest.skip("aiohttp 不可用或端口起不来")
    try:
        assert s.port and s.port > 0
        assert s.url == f"http://127.0.0.1:{s.port}/r/tok-F/"
    finally:
        s.stop()