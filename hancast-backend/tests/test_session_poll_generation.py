"""轮询代次竞态：只构造 session，DLNA/媒体服务全 mock，不启动 sidecar。

用 Event 栅栏控制网络返回和每次轮询等待；超时仅作卡死保护，不靠 sleep 排时序。
"""
import threading
from queue import Queue
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hancast_sidecar.models import session as session_module
from hancast_sidecar.models.device import Device


TIMEOUT = 5
POSITION = {"TrackDuration": "00:10:00", "RelTime": "00:00:07"}


def joined(thread):
    thread.join(TIMEOUT)
    assert not thread.is_alive(), "poll thread did not exit"


class PollEvent(threading.Event):
    """真实取消 Event + 可逐次放行的等待栅栏，避免等真实的 2 秒/1 秒。"""

    def __init__(self):
        super().__init__()
        self.waits = Queue()
        self.wake = threading.Event()

    def wait(self, timeout=None):
        self.waits.put(timeout)
        if not self.is_set():
            assert self.wake.wait(TIMEOUT), "poll wait was not released"
            self.wake.clear()
        return self.is_set()

    def set(self):
        super().set()
        self.wake.set()

    def advance(self):
        self.wake.set()

    def waiting(self, timeout):
        assert self.waits.get(timeout=TIMEOUT) == timeout


class BlockedReply:
    def __init__(self, result):
        self.result = result
        self.entered = threading.Event()
        self.release = threading.Event()

    def __call__(self, *args, **kwargs):
        self.entered.set()
        assert self.release.wait(TIMEOUT), "network reply was not released"
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class PollHarness:
    def __init__(self, monkeypatch):
        self.tokens = []
        self.threads = []
        self.replies = []
        self.events = []
        self.callback_locks_free = []
        # 替换整个构造器，连 set_device 的描述地址探测也不会触网。
        self.protocol = Mock(spec=session_module.DLNAProtocol)
        self.protocol.get_state_position.return_value = "00:00:00"
        self.protocol.get_state_duration.return_value = "00:00:00"
        self.protocol.get_position_info.return_value = POSITION.copy()
        self.protocol.get_transport_info.return_value = {
            "CurrentTransportState": "PLAYING"}
        self.media_server = Mock()
        monkeypatch.setattr(session_module, "DLNAProtocol", lambda: self.protocol)
        # 只替换 session 模块引用，不改 threading 全局或 Thread 内部用到的 Event。
        monkeypatch.setattr(session_module, "threading", SimpleNamespace(
            Event=self.make_event, Thread=self.make_thread, Lock=threading.Lock))
        self.session = session_module.DeviceCastSession(
            Device(id="tv", name="Test TV", device_type="tv",
                   ip="192.0.2.1", port=8080, status="online"),
            self.media_server)
        self.session.set_poll_callback(self.record)

    def make_event(self):
        token = PollEvent()
        self.tokens.append(token)
        return token

    def make_thread(self, *args, **kwargs):
        thread = threading.Thread(*args, **kwargs)
        self.threads.append(thread)
        return thread

    def record(self, origin, event_type, generation):
        free = origin._poll_lock.acquire(blocking=False)
        self.callback_locks_free.append(free)
        if free:
            origin._poll_lock.release()
        self.events.append((origin, event_type, generation))

    def started(self):
        token = self.session._poll_generation
        token.waiting(2.0)
        return token, self.session._poll_thread

    def play(self):
        self.session.play("http://media.test/video.mp4", "video/mp4")
        return self.started()

    def tick(self, token):
        token.advance()
        token.waiting(1.0)

    def block(self, method, result):
        reply = BlockedReply(result)
        self.replies.append(reply)
        getattr(self.protocol, method).side_effect = reply
        return reply

    def terminal_ready(self, kind):
        token, thread = self.play()
        if kind == "stopped":
            self.protocol.get_transport_info.return_value = {
                "CurrentTransportState": "STOPPED"}
            self.tick(token)
            self.tick(token)
        else:
            self.session.POLL_FAIL_LIMIT = 2
            self.protocol.get_position_info.side_effect = OSError("offline")
            self.tick(token)
        return token, thread

    def close(self):
        self.session.stop_poll()
        for token in self.tokens:
            token.set()
        for reply in self.replies:
            reply.release.set()
        for thread in self.threads:
            joined(thread)


@pytest.fixture
def poller(monkeypatch):
    harness = PollHarness(monkeypatch)
    try:
        yield harness
    finally:
        harness.close()


@pytest.mark.parametrize("result", [
    {"TrackDuration": "00:50:00", "RelTime": "00:17:00"},
    OSError("old request failed"),
], ids=["success", "failure"])
def test_old_position_reply_cannot_overwrite_restarted_poll(poller, result):
    session = poller.session
    session.POLL_FAIL_LIMIT = 1
    old, old_thread = poller.play()
    reply = poller.block("get_position_info", result)
    old.advance()
    assert reply.entered.wait(TIMEOUT)

    new, new_thread = poller.play()
    poller.protocol.get_position_info.side_effect = None
    poller.tick(new)
    expected = session.get_state()
    poller.events.clear()
    reply.release.set()
    joined(old_thread)

    assert session.get_state() == expected
    assert poller.events == []
    assert old.is_set() and not session.is_current_poll(old)
    assert session.is_current_poll(new)
    assert session._poll_running and session._poll_thread is new_thread
    # 新一代仍能继续；旧失败不能把新一代的 running 清掉。
    poller.tick(new)
    assert poller.events == [(session, "progress_update", new)]


@pytest.mark.parametrize("result", [
    {"CurrentTransportState": "PLAYING"},
    {"CurrentTransportState": "PAUSED_PLAYBACK"},
    {"CurrentTransportState": "STOPPED"},
    OSError("old transport failed"),
], ids=["playing", "paused", "stopped", "failure"])
def test_old_transport_reply_cannot_update_or_terminate_new_poll(poller, result):
    session = poller.session
    session.POLL_FAIL_LIMIT = 1
    old, old_thread = poller.play()
    poller.tick(old)
    poller.tick(old)
    reply = poller.block("get_transport_info", result)
    old.advance()
    assert reply.entered.wait(TIMEOUT)

    new, new_thread = poller.play()
    expected = session.get_state()
    poller.events.clear()
    reply.release.set()
    joined(old_thread)

    assert session.get_state() == expected
    assert poller.events == []
    assert session.is_current_poll(new) and not session.is_current_poll(old)
    assert session._poll_running and session._poll_thread is new_thread


@pytest.mark.parametrize("kind,event_type", [
    ("stopped", "playback_stopped"), ("failure", "device_unreachable")])
def test_self_termination_keeps_final_event_valid_until_explicit_stop(
        poller, kind, event_type):
    token, thread = poller.terminal_ready(kind)
    token.advance()
    joined(thread)

    assert poller.events[-1] == (poller.session, event_type, token)
    assert [e[1] for e in poller.events].count(event_type) == 1
    assert all(poller.callback_locks_free)
    assert poller.session.get_state()["status"] == "idle"
    assert not poller.session._poll_running
    assert not token.is_set() and poller.session.is_current_poll(token)

    poller.session.stop_poll()
    assert token.is_set() and not poller.session.is_current_poll(token)


@pytest.mark.parametrize("kind", ["stopped", "failure"])
def test_terminal_callback_waiting_for_registry_is_stale_after_restart(poller, kind):
    token, thread = poller.terminal_ready(kind)
    session = poller.session
    registry_lock = threading.RLock()
    registry = {session.device_id: session}
    entered = threading.Event()
    delivered, errors = [], []

    def callback(origin, event_type, generation):
        entered.set()
        # 有界 acquire 仅作错误实现的死锁保护，不用于制造时序。
        if not registry_lock.acquire(timeout=TIMEOUT):
            errors.append("session lock held while calling handler")
            return
        try:
            if (registry.get(origin.device_id) is origin
                    and origin.is_current_poll(generation)):
                delivered.append(event_type)
        finally:
            registry_lock.release()

    session.set_poll_callback(callback)
    with registry_lock:
        token.advance()
        assert entered.wait(TIMEOUT)
        # 自行终止已落地，但当前终止事件仍应有效。
        assert not session._poll_running and session.is_current_poll(token)
        session.start_poll()  # 不经 stop_poll，也必须失效自行终止的上一代。
        new, new_thread = poller.started()
        assert new is not token and token.is_set()
    joined(thread)

    assert errors == [] and delivered == []
    assert session.is_current_poll(new) and not session.is_current_poll(token)
    assert session._poll_running and session._poll_thread is new_thread


def test_same_session_image_replacement_cancels_video_before_preparing_media(poller):
    session = poller.session
    old, thread = poller.play()
    reply = poller.block("get_position_info", POSITION)
    old.advance()
    assert reply.entered.wait(TIMEOUT)
    cancelled_before_prepare = []

    def prepare(*args):
        cancelled_before_prepare.append(old.is_set() and not session.is_current_poll(old))
        return "http://media.test/image.png"

    poller.media_server.serve_file.side_effect = prepare
    poller.protocol.play.side_effect = prepare
    session.play("/unused/image.png", "image/png")
    expected = session.get_state()
    reply.release.set()
    joined(thread)

    assert cancelled_before_prepare == [True, True]
    assert len(poller.tokens) == 1 and not session._poll_running
    assert not session.is_current_poll(old)
    assert expected["status"] == "playing" and expected["position"] == "00:00:00"
    assert session.get_state() == expected and poller.events == []
    assert poller.protocol.get_position_info.call_count == 1
    poller.protocol.get_transport_info.assert_not_called()


@pytest.mark.parametrize("phase", ["initial", "interval"])
def test_stop_poll_wakes_the_current_wait_without_another_request(poller, phase):
    token, thread = poller.play()
    if phase == "interval":
        poller.tick(token)
    calls = poller.protocol.get_position_info.call_count
    poller.session.stop_poll()
    joined(thread)

    assert token.is_set() and not poller.session.is_current_poll(token)
    assert not poller.session._poll_running
    assert poller.protocol.get_position_info.call_count == calls
    assert len(poller.tokens) == 1  # 初始等待和间隔等待复用本代次令牌。


def test_running_start_is_idempotent_but_restart_uses_fresh_token(poller):
    session = poller.session
    assert not session.is_current_poll(threading.Event())
    old, old_thread = poller.play()
    session.start_poll()
    assert session._poll_thread is old_thread and len(poller.tokens) == 1
    session.stop_poll()
    session.start_poll()
    new, _ = poller.started()
    joined(old_thread)
    assert new is not old and session.is_current_poll(new)
    assert not session.is_current_poll(old)


def test_current_status_and_progress_callbacks_run_after_atomic_state_update(poller):
    token, _ = poller.play()
    poller.protocol.get_transport_info.return_value = {
        "CurrentTransportState": "PAUSED_PLAYBACK"}
    for _ in range(3):
        poller.tick(token)
    assert [e[1] for e in poller.events] == [
        "progress_update", "progress_update", "status_changed", "progress_update"]
    assert all(origin is poller.session and generation is token
               for origin, _, generation in poller.events)
    assert all(poller.callback_locks_free)
    assert poller.session.get_state()["status"] == "paused"
    expected = poller.session.get_state()
    poller.protocol.get_position_info.return_value = {
        "TrackDuration": "00:00:00", "RelTime": None}
    poller.tick(token)
    assert poller.session.get_state() == expected


def test_success_resets_only_its_generation_failure_count(poller):
    session = poller.session
    session.POLL_FAIL_LIMIT = 2
    token, thread = poller.play()
    poller.protocol.get_position_info.side_effect = [
        OSError("first"), POSITION, OSError("second"), OSError("third")]
    for _ in range(3):
        poller.tick(token)
        assert session._poll_running
    token.advance()
    joined(thread)
    assert [e[1] for e in poller.events] == ["progress_update", "device_unreachable"]
    assert session.is_current_poll(token)


@pytest.mark.parametrize("outcome", [True, False, OSError("Stop unavailable")],
                         ids=["ack", "no-control-url", "exception"])
def test_stop_delivery_semantics_unchanged_while_cancelling_poll(poller, outcome):
    token, thread = poller.play()
    if isinstance(outcome, Exception):
        poller.protocol.stop.side_effect = outcome
    else:
        poller.protocol.stop.return_value = outcome
    assert poller.session.stop() is (outcome is True)
    joined(thread)
    assert poller.session.get_state()["status"] == "idle"
    assert not poller.session.is_current_poll(token)
    if outcome is True:
        assert poller.session.stop_error is None
    elif outcome is False:
        assert "Test TV" in poller.session.stop_error
    else:
        assert poller.session.stop_error == "Stop unavailable"


@pytest.mark.parametrize("stage", ["serve_file", "play"])
def test_failed_media_replacement_does_not_leave_previous_poll_running(poller, stage):
    token, thread = poller.play()
    target = poller.media_server if stage == "serve_file" else poller.protocol
    getattr(target, stage).side_effect = RuntimeError("media setup failed")
    poller.media_server.serve_file.return_value = "http://media.test/new.mp4"
    with pytest.raises(RuntimeError, match="media setup failed"):
        poller.session.play("/unused/new.mp4", "video/mp4")
    joined(thread)
    assert token.is_set() and not poller.session.is_current_poll(token)
    assert not poller.session._poll_running and len(poller.tokens) == 1
