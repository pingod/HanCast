"""投屏失败台账 + 地址有效性预检

台账是 v2.4.5 补的可观测性：出站 SOAP 过去只有 DEBUG 级，面板默认 INFO 一个字看不到，
COMP-005（无后缀直链电视不拉流）就是这样查了大半天。这里钉住三件事：
① 一条失败能原样存回来（设备/阶段/错误/地址/时间）；
② 5 秒内同设备同阶段同错误合并计数，不刷屏；③ 超过保留期的自动清掉。
"""

import time

import pytest

from hancast_sidecar.media import cast_failures


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(cast_failures, "SETTING_DIR", str(tmp_path))
    yield cast_failures
    path = cast_failures.path()
    import os
    try:
        os.remove(path)
    except OSError:
        pass


def test_record_and_snapshot_roundtrip(ledger):
    ledger.record("dev-1", "客厅电视", "set_uri", "HTTP 500 | fault", "http://tv/control")
    snap = ledger.snapshot()
    assert snap["total"] == 1
    e = snap["entries"][0]
    assert e["device_id"] == "dev-1"
    assert e["device_name"] == "客厅电视"
    assert e["stage"] == "set_uri"
    assert e["stage_label"] == "SetAVTransportURI 被拒"
    assert "HTTP 500" in e["error"]
    assert e["url"] == "http://tv/control"
    assert e["count"] == 1


def test_same_failure_within_5s_merges(ledger):
    for _ in range(3):
        ledger.record("dev-1", "电视", "play", "boom")
    snap = ledger.snapshot()
    assert len(snap["entries"]) == 1
    assert snap["entries"][0]["count"] == 3
    assert snap["total"] == 3


def test_different_stage_is_separate_entry(ledger):
    ledger.record("dev-1", "电视", "set_uri", "boom")
    ledger.record("dev-1", "电视", "play", "boom")
    assert len(ledger.snapshot()["entries"]) == 2


def test_dedupe_window_expires(ledger, monkeypatch):
    ledger.record("dev-1", "电视", "play", "boom")
    # 把最后一条的时间推到 6 秒前，再记同样的失败 → 应是新的一条
    entries = ledger._load()
    entries[-1]["at"] = time.time() - 6
    ledger._save(entries)
    ledger.record("dev-1", "电视", "play", "boom")
    assert len(ledger.snapshot()["entries"]) == 2


def test_retention_prunes_old_entries(ledger):
    now = time.time()
    ledger._save([{"at": now - 20 * 86400, "device_id": "old", "stage": "play",
                   "error": "x", "count": 1},
                  {"at": now, "device_id": "new", "stage": "play",
                   "error": "y", "count": 1}])
    entries = ledger.snapshot()["entries"]
    assert [e["device_id"] for e in entries] == ["new"]


def test_cap_keeps_latest_100(ledger):
    now = time.time()
    # 越靠后越新：i=0 最老、i=149 最新
    ledger._save([{"at": now - (150 - i), "device_id": str(i), "stage": "play",
                   "error": "x", "count": 1} for i in range(150)])
    entries = ledger.snapshot()["entries"]
    assert len(entries) == 100
    # 最老的 50 条被丢掉；snapshot 倒序，最前的是最新那条（149）
    assert entries[0]["device_id"] == "149"
    assert {e["device_id"] for e in entries} == {str(i) for i in range(50, 150)}


def test_clear_removes_file(ledger):
    ledger.record("dev-1", "电视", "play", "boom")
    removed = ledger.clear()
    assert removed == 1
    assert ledger.snapshot()["entries"] == []


def test_empty_snapshot(ledger):
    snap = ledger.snapshot()
    assert snap["entries"] == [] and snap["total"] == 0
    assert snap["path"].endswith("cast_failures.json")


# ── 出站动作失败映射到台账 ──

def test_send_action_failure_records_stage(monkeypatch, ledger):
    """SetAVTransportURI 打不通时，台账要记下设备名、阶段和证书错误"""
    import requests
    from hancast_sidecar.protocol import dlna

    class _Dev:
        id = "d1"
        name = "卧室电视"
        display_name = "卧室电视"
        location = ""

    p = dlna.DLNAProtocol()
    monkeypatch.setattr(p, "_resolve_control_url", lambda: None)
    p.set_device(_Dev())
    p._control_url = "http://tv:1234/control"

    def _boom(*a, **k):
        raise requests.ConnectionError("connection refused")

    monkeypatch.setattr(dlna.requests, "post", _boom)
    with pytest.raises(requests.ConnectionError):
        p._send_action(dlna.NS_AVTRANSPORT, "SetAVTransportURI",
                       {"InstanceID": 0, "CurrentURI": "http://x/y.mp4"})
    e = ledger.snapshot()["entries"][0]
    assert e["device_name"] == "卧室电视"
    assert e["stage"] == "set_uri"
    assert "connection refused" in e["error"]


def test_send_action_records_connect_when_no_control_url(monkeypatch, ledger):
    from hancast_sidecar.protocol import dlna

    class _Dev:
        id = "d2"
        name = "书房电视"
        display_name = "书房电视"
        location = ""

    p = dlna.DLNAProtocol()
    monkeypatch.setattr(p, "_resolve_control_url", lambda: None)
    p.set_device(_Dev())
    p._control_url = ""
    monkeypatch.setattr(p, "_try_resolve_control_url", lambda: False)
    assert p._send_action(dlna.NS_AVTRANSPORT, "Play", {"InstanceID": 0}) is None
    e = ledger.snapshot()["entries"][0]
    assert e["stage"] == "connect"


# ── probe_url ──

class _Resp:
    def __init__(self, status=200, headers=None, url="http://x/y.mp4", body=b"a"):
        self.status_code = status
        self.headers = headers if headers is not None else {"Content-Length": "10"}
        self.url = url
        self._body = body

    def iter_content(self, n=1):
        yield self._body

    def close(self):
        pass


def _make_handler(monkeypatch, fake_session):
    from hancast_sidecar import commands
    from hancast_sidecar.utils import httpclient
    monkeypatch.setattr(httpclient, "make_session", lambda: fake_session)
    return commands.CommandHandler.__new__(commands.CommandHandler)


def test_probe_url_ok(monkeypatch):
    class _S:
        def head(self, url, **k):
            return _Resp(200)

    h = _make_handler(monkeypatch, _S())
    r = h._probe_url({"url": "http://x/y.mp4"})
    assert r["ok"] is True and r["status"] == 200


def test_probe_url_dead_falls_back_and_reports(monkeypatch):
    class _S:
        def head(self, url, **k):
            return _Resp(403, headers={})

        def get(self, url, **k):
            return _Resp(403, headers={})

    h = _make_handler(monkeypatch, _S())
    r = h._probe_url({"url": "http://x/y.mp4"})
    assert r["ok"] is False and r["status"] == 403


def test_probe_url_rejects_non_http(monkeypatch):
    h = _make_handler(monkeypatch, object())
    r = h._probe_url({"url": "file:///etc/passwd"})
    assert r["ok"] is False and "http" in r["error"]


def test_probe_url_exception_is_not_raised(monkeypatch):
    class _S:
        def head(self, url, **k):
            raise RuntimeError("dns boom")

        def get(self, url, **k):
            raise RuntimeError("dns boom")

    h = _make_handler(monkeypatch, _S())
    r = h._probe_url({"url": "http://x/y.mp4"})
    assert r["ok"] is False and "dns boom" in r["error"]
