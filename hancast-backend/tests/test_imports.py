"""
测试导入
"""

import unittest


class TestImports(unittest.TestCase):
    """测试模块导入"""

    def test_types(self):
        """测试类型模块"""
        from hancast_sidecar.models.device import Device
        from hancast_sidecar.models.media import MediaInfo
        from hancast_sidecar.models.cast import CastState

        # 测试 Device
        device = Device(
            id="test-id",
            name="Test Device",
            device_type="tv",
            ip="192.168.1.100",
            port=8080,
            status="online"
        )
        self.assertEqual(device.display_name, "Test Device")
        self.assertIn("id", device.to_dict())

        # 测试 MediaInfo
        media = MediaInfo(
            media_type="file",
            uri="/path/to/video.mp4",
            title="video.mp4"
        )
        self.assertEqual(media.title, "video.mp4")

        # 测试 CastState
        state = CastState()
        self.assertEqual(state.status, "idle")

    def test_utils(self):
        """测试工具模块"""
        from hancast_sidecar.utils.config import Config
        from hancast_sidecar.utils.logger import setup_logger

        # 测试 Config
        config = Config()
        self.assertIsNotNone(config.usn)
        self.assertIsNotNone(config.version)

    def test_ssdp(self):
        """测试 SSDP 模块"""
        from hancast_sidecar.ssdp import SSDPService

        service = SSDPService()
        self.assertIsNotNone(service)

    def test_protocol(self):
        """测试协议模块"""
        from hancast_sidecar.protocol.dlna import DLNAProtocol

        protocol = DLNAProtocol()
        self.assertIsNotNone(protocol)

    def test_renderer(self):
        """测试渲染器模块"""
        from hancast_sidecar.renderer.base import Renderer
        from hancast_sidecar.renderer.mpv import MPVRenderer

        # 测试基类
        renderer = Renderer()
        self.assertIsNotNone(renderer)

    def test_media(self):
        """测试媒体模块"""
        from hancast_sidecar.media.parser import MediaParser
        from hancast_sidecar.media.server import MediaServer

        parser = MediaParser()
        self.assertIsNotNone(parser)

        server = MediaServer()
        self.assertIsNotNone(server)

    def test_commands(self):
        """测试命令模块"""
        from hancast_sidecar.commands import CommandHandler

        handler = CommandHandler()
        self.assertIsNotNone(handler)

    def test_main(self):
        """测试主模块"""
        # main 在 import 阶段就把 sys.std* 换成 UTF-8 wrapper（协议通道的硬要求），
        # 在测试进程里 import 会顶掉 pytest/unittest 的捕获缓冲、wrapper 析构时
        # 还把那条缓冲一起关掉，后续用例全变 "I/O operation on closed file"。
        # 可导入性放到子进程里查，产品行为不受影响。
        import subprocess
        import sys

        proc = subprocess.run(
            [sys.executable, "-c", "import hancast_sidecar.main as m; print(callable(m.main))"],
            capture_output=True, text=True, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), "True")


if __name__ == '__main__':
    unittest.main()
