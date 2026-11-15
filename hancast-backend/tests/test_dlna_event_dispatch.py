"""DLNA 事件分发：重复退订不能把事件线程打死

2026-11-15 审计：`send_states_to_clients()` 用 `del self.event_subscribes[sid]`
取消失败的订阅，而 `remove_subscribe()` 只入队、不去重。同一个 SID 被排两次
（控制点重发 UNSUBSCRIBE，或超时移除撞上显式移除）时第二次 `del` 抛 KeyError；
异常会穿出 `event()` 的 while 循环，DLNA 事件线程**永久退出**，此后所有订阅者
再也收不到 LastChange NOTIFY，而 `running` 还是 True（start() 见 running 直接
返回，不会重启）。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hancast_sidecar.protocol.dlna import DLNAProtocol, ObserveClient


class _Client:
    def __init__(self, sid="uuid:test-1"):
        self.sid = sid
        self.service = "AVTransport"
        self.error = 0
        self.sent = []

    def is_timeout(self):
        return False

    def send_event_callback(self, data):
        self.sent.append(data)


class TestDuplicateUnsubscribe(unittest.TestCase):
    def setUp(self):
        self.proto = DLNAProtocol()
        self.client = _Client()
        self.proto.event_subscribes[self.client.sid] = self.client

    def test_duplicate_unsubscribe_does_not_raise(self):
        self.proto.remove_subscribe(self.client.sid)
        self.proto.remove_subscribe(self.client.sid)      # 控制点重发一次
        # 早先这里第二次必然 KeyError，并顺着 event() 的调用栈把线程带走
        self.proto.send_states_to_clients({"TransportState": "STOPPED"})
        self.assertNotIn(self.client.sid, self.proto.event_subscribes)

    def test_unknown_sid_is_tolerated(self):
        # 排队阶段才判存在性；这里的 SID 从未登记过，也不该炸
        self.proto.removed_device_queue.put("uuid:never-subscribed")
        self.proto.send_states_to_clients({"TransportState": "PLAYING"})


class TestCallbackUrlParsing(unittest.TestCase):
    def test_hostname_callback_keeps_its_host(self):
        c = ObserveClient("AVTransport", "http://phone.local:1234/cb?x=1")
        self.assertEqual(c.host, "phone.local:1234")
        self.assertEqual(c.path, "/cb?x=1")

    def test_callback_without_double_slash_does_not_indexerror(self):
        # `re.findall(...)[0]` 在这种写法上直接 IndexError，do_SUBSCRIBE 会掉连接
        c = ObserveClient("AVTransport", "192.168.1.9:9999/cb")
        self.assertEqual(c.host, "")
        c.send_event_callback({"TransportState": "PLAYING"})   # 空主机只跳过，不抛


if __name__ == "__main__":
    unittest.main()
