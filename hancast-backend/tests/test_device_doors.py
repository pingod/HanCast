"""两条曾经的单向门：隐藏设备找不回来、手动地址删不掉

两者都不是"没实现"，而是**后端实现好、界面没入口**：

- `unhide_device` / `get_hidden_devices` 在 Sidecar 注册表里（commands.py），
  但壳层没暴露、前端没调用 ⇒ 手滑隐藏一台设备，界面上再也找不回来，
  只能去改 `hancast_setting.json`。
- `Config.remove_manual_device()` 全仓零调用点 ⇒ 填错一个 IP，每次开机都
  自动重连一遍、失败一遍，没有任何地方能删掉它。

这里钉死后端语义：删除只摘持久化地址（不动已发现设备）、幂等、空值报错；
隐藏列表要带可辨识信息（名字/在离线/是否本机），不能把裸 UDN 丢给界面。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hancast_sidecar.commands import CommandHandler


class TestManualDeviceRemoval(unittest.TestCase):
    def setUp(self):
        self.handler = CommandHandler()
        self.config = self.handler.config

    def tearDown(self):
        self.handler.cleanup()

    def test_删除已保存的地址(self):
        self.config.add_manual_device("192.168.1.99:8080")
        self.config.save()
        self.assertIn("192.168.1.99:8080", self.handler.execute("get_manual_devices", {}))

        self.handler.execute("remove_manual_device", {"target": "192.168.1.99:8080"})
        self.assertEqual(self.handler.execute("get_manual_devices", {}), [])

    def test_幂等_删不存在的地址不报错(self):
        # 地址可能已被 remove_device 连带清掉，再删一次不该炸
        self.handler.execute("remove_manual_device", {"target": "10.0.0.1:9000"})
        self.assertEqual(self.handler.execute("get_manual_devices", {}), [])

    def test_空地址报错(self):
        with self.assertRaises(ValueError):
            self.handler.execute("remove_manual_device", {"target": "   "})

    def test_支持_address_别名(self):
        self.config.add_manual_device("192.168.1.77")
        self.handler.execute("remove_manual_device", {"address": "192.168.1.77"})
        self.assertEqual(self.handler.execute("get_manual_devices", {}), [])


class TestHiddenDeviceList(unittest.TestCase):
    def setUp(self):
        self.handler = CommandHandler()
        self.config = self.handler.config

    def tearDown(self):
        self.handler.cleanup()

    def test_空列表(self):
        for udn in list(self.config.hidden_devices):
            self.config.unhide_device(udn)
        self.assertEqual(self.handler.execute("get_hidden_devices", {}), [])

    def test_未知设备退回_UDN_尾段(self):
        udn = "uuid:deadbeef-aaaa-bbbb-cccc-000000000001"
        self.config.hide_device(udn)
        items = self.handler.execute("get_hidden_devices", {})
        self.assertEqual(len(items), 1)
        row = items[0]
        self.assertEqual(row["udn"], udn)
        self.assertEqual(row["name"], "deadbeef")   # UDN 尾段前 8 位
        self.assertFalse(row["online"])
        self.assertFalse(row["is_self"])

    def test_本机自己会被标出来(self):
        # 本程序作为渲染端会被自己的 SSDP 发现，隐藏它是正常状态；
        # 界面必须标出 is_self，否则用户恢复后会以为"HanCast 投给 HanCast"是 bug
        self_udn = f"uuid:{self.config.get_usn()}"
        self.config.hide_device(self_udn)
        items = self.handler.execute("get_hidden_devices", {})
        row = next(i for i in items if i["udn"] == self_udn)
        self.assertTrue(row["is_self"])

    def test_取消隐藏后不再出现(self):
        udn = "uuid:11112222-3333-4444-5555-666677778888"
        self.config.hide_device(udn)
        self.handler.execute("unhide_device", {"id": udn})
        self.assertNotIn(udn, [i["udn"] for i in self.handler.execute("get_hidden_devices", {})])
