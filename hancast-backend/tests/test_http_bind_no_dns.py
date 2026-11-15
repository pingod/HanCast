"""启动路径不能依赖反向 DNS

2026-11-15 的 v2.4.14 macos-arm64 CI 作业红在 sidecar 冒烟测试：进程卡在
DLNA 服务器 bind，20 秒内没有打印 "DLNA server started"。标准库
``HTTPServer.server_bind`` 会调用 ``socket.getfqdn(host)`` 做反向 DNS，
macOS 的 mDNSResponder/DNS 慢、断网或 VPN 刚起时这一步能阻塞几十秒；sidecar
又是同步启动的，整个应用会停在「正在启动」。

两个 HTTP 服务器（DLNA 描述服务、媒体服务）都改成绑定后不反查，
UPnP SERVER 头由 ``build_server_info()`` 生成，不依赖 ``server_name``。
"""

import os
import socket
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hancast_sidecar.media.server import MediaServer
from hancast_sidecar.protocol.server import DLNAServer


def _forbidden(host):
    raise RuntimeError(f"bind 期间不该做反向 DNS: getfqdn({host!r})")


class TestBindSkipsReverseDns(unittest.TestCase):
    def test_dlna_server_bind_does_not_reverse_dns(self):
        with patch.object(socket, "getfqdn", _forbidden):
            srv = DLNAServer("HanCast test", "uuid:test", "127.0.0.1", port=0)
            try:
                srv.start()
                self.assertGreater(srv._server.server_address[1], 0)
            finally:
                srv.stop()

    def test_media_server_bind_does_not_reverse_dns(self):
        srv = MediaServer(lan_ip="127.0.0.1")
        with patch.object(socket, "getfqdn", _forbidden):
            try:
                srv.start(port=0)
                self.assertGreater(srv.port, 0)
            finally:
                srv.stop()


if __name__ == "__main__":
    unittest.main()
