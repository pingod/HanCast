"""
SSDP 服务

关键行为:
- 注册 6 个 SSDP 条目 (rootdevice, uuid, device, 3个service)
- 每 30 秒发送 NOTIFY 广播（与 CACHE-CONTROL max-age=66 匹配）
- CACHE-CONTROL: max-age=66
- 使用 per-interface Sock 类发送多播
- M-SEARCH 响应通过监听 socket 发送
"""

import socket
import sys
import threading
import logging
import requests
import time
from email.utils import formatdate
from lxml import etree
from typing import Dict, List, Optional, Callable
from .models.device import Device
from .params import get as param

logger = logging.getLogger("hancast.ssdp")

# 局域网内设备的 HTTP 请求必须绕过系统代理：macOS 的代理例外列表用 CIDR
# （192.168.0.0/16）时 urllib 的匹配器认不出来，requests 会把设备描述抓取
# 送进 127.0.0.1:7890/9565 这类本地代理；代理一掉线，现象就是「设备发现不了」
# 且日志只有超时。DLNA 是链路内协议，本来也不该经路任何代理。
_NO_PROXY = {"http": "", "https": ""}

SSDP_PORT = 1900
SSDP_ADDR = '239.255.255.250'
SERVER_ID = 'SSDP Server'


def list_network_interfaces() -> List[Dict]:
    """枚举已配置 IPv4 的网络接口，供前端"网络接口"选择。

    返回 [{"name", "ip", "netmask", "is_default"}]，
    is_default 表示该接口持有默认路由。netifaces 不可用时返回 []。
    """
    result: List[Dict] = []
    try:
        import netifaces
    except ImportError:
        return result
    try:
        default_iface = None
        gw = netifaces.gateways().get('default', {}).get(netifaces.AF_INET)
        if gw:
            default_iface = gw[1]
        for name in netifaces.interfaces():
            # 环回接口不可用于 SSDP，选了也不会生效，直接从列表过滤
            if name.startswith('lo') or name.startswith('Loopback'):
                continue
            try:
                addrs = netifaces.ifaddresses(name)
            except ValueError:
                continue
            for a in addrs.get(netifaces.AF_INET, []):
                ip = a.get('addr')
                if not ip or ip.startswith('127.'):
                    continue
                result.append({
                    "name": name,
                    "ip": ip,
                    "netmask": a.get('netmask', ''),
                    "is_default": name == default_iface,
                })
    except Exception as e:
        logger.warning(f"枚举网络接口失败: {e}")
    return result


class Sock:
    """Per-interface multicast socket"""

    def __init__(self, ip):
        self.ip = ip
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ssdp_addr = socket.inet_aton(SSDP_ADDR)
        self.interface = socket.inet_aton(self.ip)
        # 组播被系统阻断时 send 会持续失败，每实例只告警一次避免 30s 一轮刷屏
        self.send_failed = False
        try:
            self.sock.setsockopt(
                socket.IPPROTO_IP, socket.IP_MULTICAST_IF, self.interface)
            self.sock.setsockopt(
                socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                self.ssdp_addr + self.interface)
        except Exception as e:
            logger.error(e)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)

    def send_it(self, response, destination):
        """发送一个 SSDP 报文，返回是否成功。

        失败原因（多为组播被系统策略阻断，Errno 65/101）只在首次打印，
        否则每 30s × 6 条 NOTIFY 会把日志刷满。
        """
        try:
            self.sock.sendto(
                response.format(self.ip).encode(), destination)
            return True
        except (AttributeError, socket.error) as msg:
            if not self.send_failed:
                self.send_failed = True
                logger.warning(
                    "failure sending out data: from %s to %s: %s"
                    "（同类失败不再重复告警）", self.ip, destination, msg)
            return False

    def close(self):
        try:
            self.sock.setsockopt(
                socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP,
                self.ssdp_addr + self.interface)
        except Exception:
            pass
        self.sock.close()


class SSDPService:
    """SSDP 服务"""

    def __init__(self, friendly_name: str = "HanCast", port: int = 8080,
                 usn: str = None, version: str = "0.0.0",
                 preferred_ip: str = None):
        self._devices: Dict[str, Device] = {}
        self._lock = threading.Lock()
        self._running = False

        # 用户选定的网络接口 IPv4（None/空 = 自动，使用全部接口）
        self.preferred_ip = preferred_ip or None

        # 自身设备信息
        self._friendly_name = friendly_name
        self._port = port
        self._usn = usn or f"uuid:{self._generate_uuid()}"
        self._ip = self._get_local_ip()
        # SERVER 头按运行平台和真实版本号组装（此前硬编码 Windows/10 + 2.0）
        from .utils.platform_info import build_server_info
        self._server_info = build_server_info(version)

        # SSDP 状态
        self._known: Dict[str, dict] = {}
        self.ip_list: List[tuple] = []
        self.sock_list: List[Sock] = []
        self.sock: Optional[socket.socket] = None

        # restart 串行化 + 代际号：join 超时的旧线程不得清理新实例状态
        self._restart_lock = threading.Lock()
        self._gen = 0

        # M-SEARCH 组播不可用时只告警一次，避免 30s 一轮刷屏（并提示用户排查）
        self._msearch_warned = False
        # NOTIFY 广播兜底：日志只记一次；广播通道彻底不可用则不再尝试
        self._notify_bcast_logged = False
        self._notify_bcast_dead = False
        # 最近一次设备发现走的通道（multicast / broadcast），用于日志定位
        self._discovery_via = "multicast"
        # 上一轮 NOTIFY 实际发出的条数与通道（组播/广播兜底），供心跳日志
        self._last_notify_count = 0
        self._notify_channel = "multicast"

        # 已抓取过的设备描述缓存：location → 抓取时间。
        # 同一设备每 30s 会广播多条 NOTIFY（rootdevice/uuid/device/3×service），
        # 若不缓存，每条都会触发一次 HTTP GET + XML 解析，设备多时放大成
        # 大量重复请求，占用线程和带宽。
        self._fetched_locations: Dict[str, float] = {}
        self._fetched_locations_lock = threading.Lock()
        # 设备描述抓取去重窗口（秒）：同一设备 30s 广播多条 NOTIFY，
        # 窗口内只抓取一次
        self.fetch_dedup_window = 30.0
        # location → 连续抓取失败次数：失败要重试（否则一次瞬断就把设备
        # 藏住 30s 以上），但告警只打第一次，避免每轮刷屏
        self._fetch_failures: Dict[str, int] = {}
        # location → 因「不是投屏目标」被拒绝的次数。路由器/NAS 这类 UPnP 设备
        # 每 30s 就把 NOTIFY 重播一遍，同一 location 只在首次露一行，否则日志面板
        # 一天被它刷掉上万行。
        self._rejected_targets: Dict[str, int] = {}
        # udn → 最近一次「确认它还在」的时刻（alive / M-SEARCH 回包 / 探活成功）
        self._last_seen: Dict[str, float] = {}
        # udn → 连续探活失败次数：连败两次才判离线，给瞬断留一次余地
        self._probe_failures: Dict[str, int] = {}

        # 同一时刻只允许一个 M-SEARCH 扫描线程（避免用户连续点刷新时线程堆积）
        self._scan_lock = threading.Lock()

        # 回调函数
        self._on_device_found: Optional[Callable[[Device], None]] = None
        self._on_device_lost: Optional[Callable[[str], None]] = None
        self._should_ignore_device: Optional[Callable[[str], bool]] = None

        # 线程
        self._ssdp_thread: Optional[threading.Thread] = None
        self._notify_thread: Optional[threading.Thread] = None

    def _generate_uuid(self) -> str:
        import uuid
        return str(uuid.uuid4())

    def _get_local_ip(self) -> str:
        if self.preferred_ip:
            return self.preferred_ip
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def _get_all_interfaces(self) -> List[tuple]:
        """获取所有可用网络接口的 (ip, netmask) 列表

        使用纯 socket 实现，确保在所有接口上注册 SSDP。
        Windows 上还需要包含常见的 ICS 热点网段。
        """
        interfaces = []
        try:
            import netifaces
            gateways = netifaces.gateways()
            interface_names = set()
            # 获取默认网关的接口
            for gw_type in [netifaces.AF_INET, netifaces.AF_LINK]:
                if gw_type in gateways:
                    for gw in gateways[gw_type]:
                        if len(gw) > 1:
                            interface_names.add(gw[1])
            for iface_name in interface_names:
                try:
                    addrs = netifaces.ifaddresses(iface_name)
                    if netifaces.AF_INET in addrs:
                        for addr in addrs[netifaces.AF_INET]:
                            if 'addr' in addr and 'netmask' in addr:
                                interfaces.append((addr['addr'], addr['netmask']))
                except ValueError:
                    continue
        except ImportError:
            # netifaces 不可用，回退到单 IP
            logger.debug("netifaces not available, using single IP")

        # 如果没有找到接口，使用默认 IP
        if not interfaces:
            interfaces.append((self._ip, '255.255.255.0'))

        # Windows: 尝试添加 ICS 热点网段 (192.168.137.1)
        # 只有在该地址实际可用时才添加
        if sys.platform == 'win32':
            has_ics = any(ip == '192.168.137.1' for ip, _ in interfaces)
            if not has_ics:
                try:
                    # 验证 IP 是否可用（检查是否绑定了该接口）
                    test_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                    test_sock.bind(('192.168.137.1', 0))
                    test_sock.close()
                    interfaces.append(('192.168.137.1', '255.255.255.0'))
                    logger.info("ICS interface 192.168.137.1 available")
                except (socket.error, OSError):
                    logger.debug("ICS interface 192.168.137.1 not available, skip")

        # 过滤掉无效的接口（验证每个 IP 是否可以绑定）
        valid_interfaces = []
        for ip, mask in interfaces:
            try:
                test_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                test_sock.bind((ip, 0))
                test_sock.close()
                valid_interfaces.append((ip, mask))
            except (socket.error, OSError):
                logger.warning(f"Interface {ip} not available, skipping")

        if not valid_interfaces:
            # 所有接口都无效，使用默认 IP
            valid_interfaces.append((self._ip, '255.255.255.0'))

        # 用户选定了网络接口：只保留该接口（组播/广播/应答都走它）
        if self.preferred_ip:
            selected = [(ip, mask)
                        for ip, mask in valid_interfaces
                        if ip == self.preferred_ip]
            if selected:
                logger.info(f"SSDP 使用选定接口 {self.preferred_ip}")
                return selected
            logger.warning(
                f"选定接口 {self.preferred_ip} 不可用，回退到全部接口")

        logger.info(f"SSDP interfaces: {valid_interfaces}")
        return valid_interfaces

    def set_callbacks(self, on_device_found=None, on_device_lost=None,
                      should_ignore_device=None):
        self._on_device_found = on_device_found
        self._on_device_lost = on_device_lost
        self._should_ignore_device = should_ignore_device

    def start(self):
        if self._running:
            logger.warning("SSDP already running")
            return
        self._running = True
        self._gen += 1
        self._register()

        # 启动 SSDP 监听线程
        self._ssdp_thread = threading.Thread(
            target=self._run_ssdp, name="SSDP_THREAD", daemon=True)
        self._ssdp_thread.start()

        # 等待 SSDP 线程初始化
        time.sleep(0.5)

        # 启动 NOTIFY 线程 (每 30 秒广播一次，与 CACHE-CONTROL max-age=66 匹配)
        self._notify_thread = threading.Thread(
            target=self._notify_loop, name="SSDP_NOTIFY_THREAD",
            daemon=True)
        self._notify_thread.start()

        # 扫描其他设备
        self.scan()

        logger.info(
            f"SSDP started: {self._friendly_name} ({self._ip}:{self._port})")
        logger.info(f"SSDP interfaces: {self.ip_list}")
        logger.info(f"SSDP sock_list count: {len(self.sock_list)}")

    def stop(self):
        self._running = False
        self._do_byebye()

    def restart(self):
        """重新绑定网络接口后重启服务（停止旧线程→重新注册→重新监听）"""
        with self._restart_lock:
            self.stop()
            for t in (self._ssdp_thread, self._notify_thread):
                if t and t.is_alive():
                    t.join(timeout=6)
            self._ssdp_thread = None
            self._notify_thread = None
            self._ip = self._get_local_ip()
            self.start()

    def _register(self):
        """注册 6 个 SSDP 条目"""
        location = 'http://{{}}:{}/description.xml'.format(self._port)
        usn = self._usn  # e.g. "uuid:82b37828-..."

        devices = [
            f'{usn}::upnp:rootdevice',
            f'{usn}',
            f'{usn}::urn:schemas-upnp-org:device:MediaRenderer:1',
            f'{usn}::urn:schemas-upnp-org:service:RenderingControl:1',
            f'{usn}::urn:schemas-upnp-org:service:ConnectionManager:1',
            f'{usn}::urn:schemas-upnp-org:service:AVTransport:1',
        ]

        self._known = {}
        for device_usn in devices:
            # ST = USN 第 43 个字符之后的内容，如果是空则用完整 USN
            st = device_usn[43:] if len(device_usn) > 43 and device_usn[43:] != '' else device_usn
            self._known[device_usn] = {
                'USN': device_usn,
                'LOCATION': location,
                'ST': st,
                'EXT': '',
                'SERVER': self._server_info,
                'CACHE-CONTROL': 'max-age=66',
            }

        logger.info(f"Registered as DLNA Renderer: {self._usn}")

    def _run_ssdp(self):
        """SSDP 主循环"""
        my_gen = self._gen
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 0)

        if sys.platform == 'win32':
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        elif sys.platform == 'darwin':
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        elif hasattr(socket, "SO_REUSEPORT"):
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except socket.error:
                pass
        elif hasattr(socket, "SO_REUSEADDR"):
            try:
                self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            except socket.error:
                pass

        # 获取本机所有网络接口 IP 列表
        self.ip_list = self._get_all_interfaces()
        self.sock_list = []
        for ip, mask in self.ip_list:
            try:
                mreq = socket.inet_aton(SSDP_ADDR) + socket.inet_aton(ip)
                self.sock.setsockopt(
                    socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
                self.sock_list.append(Sock(ip))
                logger.info(f"Added multicast membership for {ip}")
            except Exception as e:
                logger.warning(f"Failed to add membership for {ip}: {e}")

        if not self.sock_list:
            logger.error("No valid network interfaces found!")
            self.sock.close()
            self.sock = None
            return

        try:
            self.sock.bind(('0.0.0.0', SSDP_PORT))
            logger.info(f"SSDP bound to port {SSDP_PORT}")
        except Exception as e:
            logger.error(f"SSDP bind error: {e}")
            # 失败也要释放已建立的成员关系与 socket，否则 1900 端口被
            # 漏掉的 socket 占住，后续 restart 持续失败
            for s in self.sock_list:
                s.close()
            self.sock_list = []
            self.sock.close()
            self.sock = None
            return

        self.sock.settimeout(1)
        logger.info(f"SSDP listening on port {SSDP_PORT}")
        logger.info(f"SSDP multicast memberships: {len(self.ip_list)}")

        while self._running:
            try:
                data, addr = self.sock.recvfrom(1024)
                self._datagram_received(data, addr)
            except socket.timeout:
                continue
            except Exception as e:
                logger.debug(f"SSDP receive error: {e}")

        # 清理（仅当代际未被 restart 推进：旧线程不得关闭新实例的 socket）
        if self._gen != my_gen:
            logger.debug("Stale SSDP thread exiting; skip cleanup")
            return
        self._do_byebye()
        for ip, mask in self.ip_list:
            mreq = socket.inet_aton(SSDP_ADDR) + socket.inet_aton(ip)
            try:
                self.sock.setsockopt(
                    socket.IPPROTO_IP, socket.IP_DROP_MEMBERSHIP, mreq)
            except Exception:
                continue
        self.sock.close()
        self.sock = None
        # 关闭每接口发送 socket（restart 时避免泄漏）
        for s in self.sock_list:
            s.close()
        self.sock_list = []

    def _datagram_received(self, data, host_port):
        """处理收到的 SSDP 数据报"""
        (host, port) = host_port

        try:
            header = data.decode().split('\r\n\r\n')[0]
        except ValueError as err:
            logger.error(err)
            return
        if len(header) == 0:
            return

        lines = header.split('\r\n')
        cmd = lines[0].split(' ')
        lines = map(lambda x: x.replace(': ', ':', 1), lines[1:])
        lines = filter(lambda x: len(x) > 0, lines)

        headers = [x.split(':', 1) for x in lines]
        headers = dict(map(lambda x: (x[0].lower(), x[1]), headers))

        if cmd[0] != 'NOTIFY':
            logger.debug('SSDP command %s %s - from %s:%d' %
                         (cmd[0], cmd[1], host, port))

        if cmd[0] == 'M-SEARCH' and cmd[1] == '*':
            self._discovery_request(headers, (host, port))
        elif cmd[0] == 'NOTIFY' and cmd[1] == '*':
            # 处理设备上线通知
            self._handle_notify(headers, (host, port))

    def _discovery_request(self, headers, host_port):
        """处理 M-SEARCH 请求"""
        (host, port) = host_port
        st = headers.get('st', '')

        # 广播回退通道会把自家发出的 M-SEARCH 送回自己的 1900 监听口，
        # 此时源地址是广播地址（或本机网卡 IP）。对它应答必然失败
        # （应答套接字未开 SO_BROADCAST ⇒ Errno 49），每轮扫描白刷十几条 ERROR
        if self._is_self_request(host):
            logger.debug('Ignoring self-origin M-SEARCH from %s:%d' %
                         (host, port))
            return

        logger.debug('Discovery request from (%s,%d) for %s' %
                     (host, port, st))

        for i in self._known.values():
            if i['ST'] == st or st == 'ssdp:all':
                response = ['HTTP/1.1 200 OK']

                usn = None
                for k, v in i.items():
                    if k == 'USN':
                        usn = v
                    response.append('%s: %s' % (k, v))

                if usn:
                    response.append('DATE: %s' % formatdate(
                        timeval=None, localtime=False, usegmt=True))
                    response.extend(('', ''))

                    destination = (host, port)

                    # 通过匹配子网的接口发送响应
                    for ip, mask in self.ip_list:
                        if self._get_subnet_ip(ip, mask) == self._get_subnet_ip(host, mask):
                            try:
                                data = '\r\n'.join(response).format(ip).encode()
                                send_sock = socket.socket(
                                    socket.AF_INET, socket.SOCK_DGRAM)
                                # 必须绑定到匹配的那个网卡：
                                # 多网卡机器上不绑定的话，内核会按路由表选出口，
                                # 响应可能从另一个网段发出，请求方收不到（或
                                # 收到源地址与 LOCATION 不符的包而被丢弃）。
                                try:
                                    send_sock.bind((ip, 0))
                                except OSError as e:
                                    logger.debug(
                                        f"Cannot bind M-SEARCH reply socket to {ip}: {e}")
                                    send_sock.close()
                                    continue
                                send_sock.sendto(data, destination)
                                send_sock.close()
                                logger.debug(
                                    "M-SEARCH response sent to %s:%d for %s (%d bytes)"
                                    % (destination[0], destination[1], st, len(data)))
                            except Exception as e:
                                logger.error(
                                    f"Failed to send M-SEARCH response: {e}")
                            break

    def _get_subnet_ip(self, ip, mask):
        a = [int(n) for n in mask.split('.')]
        b = [int(n) for n in ip.split('.')]
        return [a[i] & b[i] for i in range(4)]

    def _get_broadcast_ip(self, ip, mask):
        """由接口地址与掩码算出子网广播地址（组播不可用时的回退目标）。"""
        try:
            a = [int(n) for n in mask.split('.')]
            b = [int(n) for n in ip.split('.')]
            if len(a) != 4 or len(b) != 4:
                return None
            # 掩码 255.255.255.255（点对点隧道）无子网广播概念
            if a == [255, 255, 255, 255]:
                return None
            parts = [str(b[i] | (255 - a[i])) for i in range(4)]
            return '.'.join(parts)
        except (ValueError, AttributeError):
            return None

    def _is_self_request(self, host: str) -> bool:
        """判断 M-SEARCH 是否源自本机（含自己广播的自环回包）。"""
        if host in ("255.255.255.255", SSDP_ADDR):
            return True
        own = {ip for ip, _ in self.ip_list}
        if host in own:
            return True
        for ip, mask in self.ip_list:
            if host == self._get_broadcast_ip(ip, mask):
                return True
        return False

    def _is_own_location(self, location: str) -> bool:
        """LOCATION 的主机部分是否就是本机自己的网卡地址。

        广播兜底通道会把自家发出的 NOTIFY 送回自己的 1900 监听口，于是 sidecar
        去抓自己通告的描述。真实网卡（192.168.1.5）能连通、只是白跑一趟，但
        VMware/UTM 一类虚拟网卡挂的是网段地址（192.168.215.0、192.168.148.0），
        connect 直接 Errno 49，结果是设备列表里根本没有这个东西，日志却被
        「抓取设备描述失败」每轮刷满。"""
        try:
            from urllib.parse import urlparse
            host = urlparse(location).hostname
        except Exception:
            return False
        if not host:
            return False
        return host in {ip for ip, _ in self.ip_list}

    def _handle_notify(self, headers, host_port):
        """处理 NOTIFY 消息 — 设备上线/离线通知"""
        (host, port) = host_port
        nts = headers.get('nts', '')

        # 设备主动告别（关机/断网时多数实现会发一条）
        if nts == 'ssdp:byebye':
            self._handle_byebye(headers, host)
            return

        # 只处理 ssdp:alive 通知（设备上线）
        if nts != 'ssdp:alive':
            return

        location = headers.get('location', '')
        if not location:
            return

        if self._is_own_location(location):
            logger.debug('Ignoring self-announce NOTIFY for %s' % location)
            return

        # 听到 alive 本身就是「它还在线」的证据，即便这次不重新抓描述
        self._touch_by_location(location)

        # 去重：同一 location 在去重窗口内不重复抓取
        with self._fetched_locations_lock:
            now = time.time()
            if location in self._fetched_locations:
                if now - self._fetched_locations[location] < self.fetch_dedup_window:
                    return
            self._fetched_locations[location] = now

        logger.debug(f"NOTIFY alive from {host}:{port}, location: {location}")

        # 异步获取设备描述
        threading.Thread(
            target=self._fetch_device_from_notify,
            args=(location, host),
            daemon=True
        ).start()

    def _fetch_device_from_notify(self, location, ip):
        """从 NOTIFY 的 LOCATION 获取设备描述"""
        try:
            from urllib.parse import urlparse
            parsed = urlparse(location)
            device_port = parsed.port or 80

            resp = requests.get(location, timeout=5, proxies=_NO_PROXY)
            resp.encoding = resp.apparent_encoding or 'utf-8'
            device = self._parse_device_description(
                resp.text, ip, port=device_port, location=location)
            self._fetch_failures.pop(location, None)
            if device is None:
                # 重抓无意义；不在这里断言原因——解析侧已按 location 记过精确理由
                # （不是 UPnP 描述 / 缺 UDN / 不提供 AVTransport 投屏服务）
                return
            self._register_found_device(device, "NOTIFY")
        except Exception as e:
            # 抓取失败原本只记 DEBUG，且 location 已在去重表里 —— 一次瞬断
            # 或一次防火墙拦截就把设备藏满整个去重窗口，用户看到的现象就是
            # 「macOS 端发现不了 Windows 设备」，日志里还一行线索都没有。
            # 失败改为放开去重（下轮 alive 重试），并首次/每 10 次告警。
            n = self._fetch_failures.get(location, 0) + 1
            self._fetch_failures[location] = n
            self._fetched_locations[location] = 0.0
            if n == 1 or n % 10 == 0:
                logger.warning(
                    "抓取设备描述失败 第%d次: %s（%s: %s）— 对端可能已离线，"
                    "或其防火墙拦截了入站 HTTP（Windows 需在防火墙弹窗允许 "
                    "HanCast 的专用/公用网络）", n, location,
                    e.__class__.__name__, e)
            else:
                logger.debug("抓取设备描述重试中 第%d次: %s: %s", n, location, e)

    def _register_found_device(self, device: Device, via: str = "NOTIFY") -> None:
        """把发现到的设备并入设备表（保留用户改名，只在新增时通知前端）"""
        if self._should_ignore_device and self._should_ignore_device(device.id):
            logger.debug(f"Ignoring hidden device: {device.name} ({device.id})")
            return
        self._mark_seen(device.id)
        with self._lock:
            old = self._devices.get(device.id)
            if old is None:
                self._devices[device.id] = device
                logger.info(f"Found device via {via}: "
                            f"{device.name} ({device.ip}:{device.port})")
                if self._on_device_found:
                    self._on_device_found(device)
            else:
                # 更新已有设备（保留用户自定义名称），不重复触发发现事件
                if old.custom_name:
                    device.custom_name = old.custom_name
                self._devices[device.id] = device

    def _mark_seen(self, udn: str) -> None:
        """记下「刚刚还确认它存在」，下线判定以它为基准"""
        self._last_seen[udn] = time.monotonic()
        self._probe_failures.pop(udn, None)

    def _touch_by_location(self, location: str) -> None:
        """收到某台设备任意一条 alive 就刷新其存活时间（含被去重跳过的那些）"""
        with self._lock:
            for device in self._devices.values():
                if device.location == location:
                    self._mark_seen(device.id)
                    return

    def _forget_device(self, device: Device, reason: str) -> None:
        """从设备表摘掉一台设备并通知前端（前端在 device_lost 上只做过滤）"""
        with self._lock:
            self._devices.pop(device.id, None)
        self._last_seen.pop(device.id, None)
        self._probe_failures.pop(device.id, None)
        if device.location:
            # 清掉抓取记录：关机后马上重开的设备，下一轮 alive 就得重新抓描述
            with self._fetched_locations_lock:
                self._fetched_locations.pop(device.location, None)
                self._fetch_failures.pop(device.location, None)
        logger.info(f"设备已下线（{reason}）: {device.name} ({device.ip}:{device.port})")
        if self._on_device_lost:
            self._on_device_lost(device.id)

    def _handle_byebye(self, headers, host: str) -> None:
        """ssdp:byebye：设备主动告别，立刻摘除，不必再等探活

        USN 形如 ``uuid:xxx::urn:schemas-upnp-org:device:MediaRenderer:1``，
        但有些实现只发 ``NT: upnp:rootdevice``（不带 uuid）——那就按 IP 认。
        """
        usn = (headers.get('usn') or headers.get('nt') or '').strip()
        udn = usn.split('::')[0].strip() if usn.startswith('uuid:') else ''
        with self._lock:
            hit = self._devices.get(udn) if udn else None
            if hit is None:
                hit = next((d for d in self._devices.values() if d.ip == host), None)
        if hit is not None:
            self._forget_device(hit, "收到 byebye")

    def _sweep_stale_devices(self) -> None:
        """静默超时的设备先问一次，别把「不心跳」的电视误删

        不少渲染器只在开机时通告一次（CACHE-AGE 能写到几小时），之后
        NOTIFY 也可能被交换机/组播吞掉。「没再听到」因此只是嫌疑，
        真正的判定依据是它自己的描述地址：连问两次都不应答才摘除。
        """
        stale_s = float(param("discovery.device_stale_s"))
        now = time.monotonic()
        with self._lock:
            candidates = [d for d in self._devices.values()
                          if now - self._last_seen.get(d.id, now) >= stale_s]
        if candidates:
            threading.Thread(target=self._probe_stale_devices,
                             args=(candidates,), daemon=True).start()

    def _probe_stale_devices(self, candidates: List[Device]) -> None:
        for device in candidates:
            url = device.location or f"http://{device.ip}:{device.port}/description.xml"
            try:
                # 有任何 HTTP 应答就说明那台机器还在；404 也算活着 ——
                # 手工添加的设备未必知道描述的确切路径
                requests.get(url, timeout=5, proxies=_NO_PROXY)
            except Exception as e:
                logger.debug(f"探活无应答 {device.name} {url}: {e}")
                fails = self._probe_failures.get(device.id, 0) + 1
                self._probe_failures[device.id] = fails
                if fails >= 2:
                    self._forget_device(device, f"连续 {fails} 次探活无应答")
                else:
                    logger.info(f"设备 {device.name} 已静默且探活无应答，"
                                f"下轮仍无应答就移除（第 {fails} 次）")
                continue
            self._mark_seen(device.id)

    def _notify_loop(self):
        """每 30 秒发送 NOTIFY 广播（UPnP 规范允许最大 255 秒间隔）"""
        round_no = 0
        while self._running:
            try:
                self._do_notify()
                self._sweep_stale_devices()
                round_no += 1
                if round_no == 1 or round_no % 20 == 0:
                    # 心跳：排查「对端发现不到本机」时，这是 alive 到底有没有
                    # 发出去、走的组播还是广播兜底的唯一现场证据
                    logger.info(
                        "NOTIFY alive 心跳: 本轮 %d 条, 网卡 %s, 通道 %s",
                        self._last_notify_count,
                        ",".join(ip for ip, _ in self.ip_list) or "无",
                        self._notify_channel)
                for _ in range(30):
                    if not self._running:
                        return
                    time.sleep(1)
            except Exception as e:
                logger.error(f"NOTIFY error: {e}")
                time.sleep(5)

    def _do_notify(self):
        """发送 NOTIFY 广播（每个网卡每条 USN 仅发送一次）"""
        sent_total = 0
        self._notify_channel = 'multicast'
        for usn in self._known:
            logger.debug('Sending alive notification for %s' % usn)
            if usn not in self._known:
                continue

            resp = [
                'NOTIFY * HTTP/1.1',
                'HOST: %s:%d' % (SSDP_ADDR, SSDP_PORT),
                'NTS: ssdp:alive',
            ]
            stcpy = dict(self._known[usn].items())
            stcpy['NT'] = stcpy['ST']
            del stcpy['ST']

            resp.extend(map(lambda x: ': '.join(x), stcpy.items()))
            resp.extend(('', ''))

            try:
                sent = False
                for s in self.sock_list:
                    if s.send_it('\r\n'.join(resp), (SSDP_ADDR, SSDP_PORT)):
                        sent = True
                        sent_total += 1
                if not sent:
                    # 组播通道全挂（系统策略阻断多播）：本机 renderer 对
                    # 手机侧不可见，退到子网广播把 alive 发出去
                    self._notify_channel = 'broadcast'
                    sent_total += self._notify_broadcast('\r\n'.join(resp))
                else:
                    logger.debug('NOTIFY sent for %s via %d interfaces' %
                                 (usn, len(self.sock_list)))
            except (AttributeError, socket.error) as msg:
                logger.warning(
                    "failure sending out alive notification: %r" % msg)
        self._last_notify_count = sent_total

    def _notify_broadcast(self, template: str) -> int:
        """NOTIFY 的组播兜底：向各接口子网广播地址发送同一份 alive 报文。

        与 M-SEARCH 的广播回退对称。HOST 头仍按 SSDP 规范写组播地址，
        多数控制点按 NT/NTS/USN 解析，不校验链路层地址。
        返回本轮实际发出的条数（供心跳统计）。
        """
        if self._notify_bcast_dead:
            return 0
        sock = None
        sent_to = []
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL,
                            int(param("discovery.multicast_ttl")))
            for ip, mask in self.ip_list:
                bcast = self._get_broadcast_ip(ip, mask)
                if not bcast:
                    continue
                try:
                    sock.sendto(template.format(ip).encode(),
                                (bcast, SSDP_PORT))
                    sent_to.append(bcast)
                except OSError as e:
                    logger.debug("broadcast NOTIFY to %s failed: %s", bcast, e)
            if sent_to and not self._notify_bcast_logged:
                # 只记一次日志：报文本身每轮仍要发，否则对端按
                # CACHE-CONTROL max-age 超时后会把我们剔除
                self._notify_bcast_logged = True
                logger.info(
                    "已通过子网广播发送 NOTIFY: %s（组播被系统阻断，"
                    "后续同类广播不再记录日志）", ", ".join(sent_to))
            elif not sent_to:
                self._notify_bcast_dead = True
        except OSError as e:
            self._notify_bcast_dead = True
            logger.warning("子网广播 NOTIFY 不可用: %s", e)
        finally:
            if sock:
                sock.close()
        return len(sent_to)

    def _do_byebye(self):
        """发送 byebye"""
        for usn in self._known:
            logger.debug('Sending byebye notification for %s' % usn)
            resp = [
                'NOTIFY * HTTP/1.1',
                'HOST: %s:%d' % (SSDP_ADDR, SSDP_PORT),
                'NTS: ssdp:byebye',
            ]
            try:
                stcpy = dict(self._known[usn].items())
                stcpy['NT'] = stcpy['ST']
                del stcpy['ST']

                resp.extend(map(lambda x: ': '.join(x), stcpy.items()))
                resp.extend(('', ''))
                if self.sock:
                    try:
                        for s in self.sock_list:
                            s.send_it(
                                '\r\n'.join(resp), (SSDP_ADDR, SSDP_PORT))
                    except (AttributeError, socket.error) as msg:
                        logger.error(
                            "error sending byebye notification: %r" % msg)
            except KeyError as msg:
                logger.error(
                    "error building byebye notification: %r" % msg)

    def scan(self):
        # 同一时刻只允许一个扫描线程：若上一次 M-SEARCH 监听窗口
        # （约 discovery.ssdp_mx + 1 秒）尚未结束，直接复用，避免线程堆积
        if not self._scan_lock.acquire(blocking=False):
            logger.debug("M-SEARCH scan already in progress, skipping")
            return
        try:
            threading.Thread(target=self._send_msearch, daemon=True).start()
        finally:
            # 注意：锁由 _send_msearch 结束时释放，这里不释放
            pass

    def _send_msearch(self):
        # 搜索多种设备类型以提高发现率
        search_types = [
            "urn:schemas-upnp-org:device:MediaRenderer:1",
            "urn:schemas-upnp-org:device:MediaRenderer:2",
            "ssdp:all",
        ]

        sock = None
        try:
            mx = int(param("discovery.ssdp_mx"))
            ttl = int(param("discovery.multicast_ttl"))
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
            # 允许发子网广播（组播被阻断时的回退通道）
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            # 选定接口时绑定该网卡：多接口机器（VPN/热点）不绑定会按路由表
            # 选出口，M-SEARCH 可能从错误网段发出
            if self.preferred_ip:
                try:
                    sock.bind((self.preferred_ip, 0))
                except OSError as e:
                    logger.debug(
                        f"Cannot bind M-SEARCH socket to {self.preferred_ip}: {e}")
            sock.settimeout(mx + 1)

            def _build(st: str) -> bytes:
                return (
                    "M-SEARCH * HTTP/1.1\r\n"
                    f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
                    "MAN: \"ssdp:discover\"\r\n"
                    f"MX: {mx}\r\n"
                    f"ST: {st}\r\n"
                    "\r\n"
                ).encode()

            # 先试标准组播。部分系统（VPN 接管路由、NECP 策略等）会禁止
            # UDP 组播，此时内核返回 EHOSTUNREACH/ENETUNREACH。
            multicast_ok = True
            try:
                for st in search_types:
                    logger.debug(f"Sending M-SEARCH for {st}")
                    sock.sendto(_build(st), (SSDP_ADDR, SSDP_PORT))
            except OSError as e:
                multicast_ok = False
                if not self._msearch_warned:
                    self._msearch_warned = True
                    logger.warning(
                        "SSDP 组播不可用（%s），回退到子网广播发现设备。"
                        "部分设备可能不响应广播的 M-SEARCH。", e)

            # 组播不通时，用各接口的子网广播地址重发（SSDP 兼容做法）
            if not multicast_ok:
                self._discovery_via = "broadcast"
                sent_to = []
                for ip, mask in self.ip_list:
                    bcast = self._get_broadcast_ip(ip, mask)
                    if not bcast:
                        continue
                    for st in search_types:
                        try:
                            sock.sendto(_build(st), (bcast, SSDP_PORT))
                        except OSError as e:
                            logger.debug(
                                "broadcast M-SEARCH to %s failed: %s", bcast, e)
                    sent_to.append(bcast)
                if sent_to:
                    logger.info(
                        "已通过子网广播发送 M-SEARCH: %s（组播被系统阻断，"
                        "广播发现的设备能力与客户端软件一致）", ", ".join(sent_to))
            else:
                self._discovery_via = "multicast"

            # 组播可用则复位，便于下次真断时还能再报
            if multicast_ok:
                self._msearch_warned = False

            while self._running:
                try:
                    data, addr = sock.recvfrom(4096)
                    self._parse_response(data.decode(), addr)
                except socket.timeout:
                    break
        except Exception as e:
            logger.error(f"M-SEARCH error: {e}")
        finally:
            if sock is not None:
                sock.close()
            self._scan_lock.release()

    def _parse_response(self, data: str, addr: tuple):
        headers = {}
        for line in data.split("\r\n"):
            if ":" in line:
                key, _, value = line.partition(":")
                headers[key.strip().upper()] = value.strip()

        location = headers.get("LOCATION")
        if not location:
            return

        if self._is_own_location(location):
            logger.debug('Ignoring self-announce response for %s' % location)
            return

        # 去重：与 NOTIFY 路径共用缓存，避免同一设备被重复抓取描述
        with self._fetched_locations_lock:
            now = time.time()
            if location in self._fetched_locations:
                if now - self._fetched_locations[location] < self.fetch_dedup_window:
                    return
            self._fetched_locations[location] = now

        try:
            from urllib.parse import urlparse
            parsed = urlparse(location)
            device_port = parsed.port or 80
        except Exception:
            device_port = 80

        try:
            resp = requests.get(location, timeout=5, proxies=_NO_PROXY)
            resp.encoding = resp.apparent_encoding or 'utf-8'
            device = self._parse_device_description(
                resp.text, addr[0], port=device_port, location=location)
            self._fetch_failures.pop(location, None)
            if device is None:
                # 同上：理由由解析侧按 location 记一次，避免每轮刷新重复刷屏
                return
            self._register_found_device(device, "M-SEARCH")
        except Exception as e:
            # 同 NOTIFY 路径：失败必须放开去重，否则一次拦截就让设备
            # 在接下来几轮刷新里持续缺席
            n = self._fetch_failures.get(location, 0) + 1
            self._fetch_failures[location] = n
            self._fetched_locations[location] = 0.0
            if n == 1 or n % 10 == 0:
                logger.warning(
                    "抓取设备描述失败 第%d次: %s（%s: %s）— 对端可能已离线，"
                    "或其防火墙拦截了入站 HTTP（Windows 需在防火墙弹窗允许 "
                    "HanCast 的专用/公用网络）", n, location,
                    e.__class__.__name__, e)
            else:
                logger.debug("抓取设备描述重试中 第%d次: %s: %s", n, location, e)

    # 投屏目标的准入条件：描述里必须有 AVTransport 服务。
    # 控制端能做的所有事（SetAVTransportURI / Play / Pause / Stop / Position）都走
    # 这一个服务，deviceType 只是给人看的标签——路由器的 InternetGatewayDevice、
    # NAS 的 MediaServer 都能被 UPnP 栈正常解析，但对投屏毫无用处。以前「有 UDN
    # 就登记」，路由器就混进了投屏列表（2.3.12 本机日志：`荣耀路由X1 增强版`，2026-10-15）。
    # 大小写不敏感比对：URN 规范写法是 AVTransport，但山寨描述里 a/v 大小写混乱的不少，
    # 宁可放宽匹配也别把真能投屏的设备挡在门外。
    _CAST_SERVICE = "urn:schemas-upnp-org:service:avtransport:"

    @classmethod
    def _provides_cast_service(cls, root) -> bool:
        """在整份描述里找 AVTransport 服务（含内嵌设备，命名空间无关）"""
        for node in root.iter():
            tag = node.tag
            if not isinstance(tag, str):
                # 注释与处理指令节点的 tag 是个可调用对象（lxml 里不是字符串），
                # 而设备描述带 XML 注释很常见——这里炸掉会让整份好描述被判成坏数据
                continue
            # node.tag 可能是 '{urn:...}serviceType' 或 'serviceType'，两种都取 local-name
            if tag.rpartition('}')[2] != "serviceType":
                continue
            if (node.text or "").strip().lower().startswith(cls._CAST_SERVICE):
                return True
        return False

    def _note_rejected_target(self, where: str, detail: str,
                              warn: bool = False) -> None:
        """登记「这份描述不能当投屏目标」的理由；同一 location 只在首次露一行"""
        n = self._rejected_targets.get(where, 0) + 1
        self._rejected_targets[where] = n
        if n > 1:
            logger.debug("不列入投屏目标：%s — %s", detail, where)
            return
        if warn:
            logger.warning("不列入投屏目标：%s — %s", detail, where)
        else:
            logger.info("不列入投屏目标：%s — %s", detail, where)

    def _parse_device_description(self, xml_text: str, ip: str,
                                  port: int = 8080,
                                  location: str = "") -> Optional[Device]:
        where = location or f"{ip}:{port}"
        try:
            # 确保 XML 文本是字符串，然后编码为字节
            if isinstance(xml_text, bytes):
                xml_bytes = xml_text
            else:
                xml_bytes = xml_text.encode('utf-8', errors='replace')
            root = etree.fromstring(xml_bytes)

            # 尝试多种命名空间格式
            ns_list = [
                {"upnp": "urn:schemas-upnp-org:device-1-0"},
                {"upnp": "urn:schemas-upnp-org:device-1-1"},
                {},  # 无命名空间
            ]

            device_elem = None
            active_ns = None
            for ns in ns_list:
                device_elem = root.find(".//upnp:device", ns) if ns else root.find(".//device")
                if device_elem is not None:
                    active_ns = ns
                    break

            if device_elem is None:
                # 尝试直接查找 deviceType 来判断是否是设备描述
                device_type_elem = root.find(".//*[local-name()='deviceType']")
                if device_type_elem is not None:
                    device_elem = device_type_elem.getparent()
                    active_ns = {}
                else:
                    self._note_rejected_target(
                        where, "抓回来的不是 UPnP 设备描述（没有 device 元素）",
                        warn=True)
                    return None

            def find_text(elem, tag, default=""):
                """查找元素文本，支持带命名空间和不带命名空间"""
                result = elem.findtext(f"upnp:{tag}", None, active_ns) if active_ns else None
                if result is None:
                    result = elem.findtext(tag, default)
                return result or default

            friendly_name = find_text(device_elem, "friendlyName")
            model_name = find_text(device_elem, "modelName")
            udn = find_text(device_elem, "UDN")

            if not udn:
                self._note_rejected_target(
                    where, "设备描述缺少 UDN，无法建立身份", warn=True)
                return None

            device_type = "unknown"
            type_str = find_text(device_elem, "deviceType")
            if "MediaRenderer" in type_str:
                device_type = "tv"
            elif "Speaker" in type_str or "Audio" in type_str:
                device_type = "speaker"
            elif "MediaServer" in type_str:
                device_type = "server"

            # 准入看服务，不看标签：deviceType 五花八门的设备只有提供 AVTransport
            # 才可能真的被投屏控制。
            if not self._provides_cast_service(root):
                self._note_rejected_target(
                    where, f"{friendly_name or '无名设备'} 不提供 AVTransport 投屏服务"
                           f"（deviceType={type_str or '未声明'}）")
                return None

            self._rejected_targets.pop(where, None)
            logger.debug(f"Parsed device: {friendly_name} ({device_type}) at {ip}:{port}"
                         f" [via {self._discovery_via}]")

            return Device(
                id=udn, name=friendly_name, device_type=device_type,
                ip=ip, port=port, status="online",
                model_name=model_name, udn=udn, location=location,
            )
        except Exception as e:
            self._note_rejected_target(
                where, f"描述 XML 解析失败（{e.__class__.__name__}: {e}）",
                warn=True)
            return None

    def get_devices(self) -> List[Device]:
        with self._lock:
            return list(self._devices.values())

    def get_device(self, device_id: str) -> Optional[Device]:
        with self._lock:
            return self._devices.get(device_id)

    def rename_device(self, device_id: str, name: str):
        with self._lock:
            if device_id in self._devices:
                self._devices[device_id].custom_name = name

    def remove_device(self, device_id: str):
        with self._lock:
            self._devices.pop(device_id, None)

    # ── 手动添加（多播被防火墙/安全软件拦截时的兜底通道）──

    # 常见 DLNA 服务器端口（MediaRenderer / DMS）
    _MANUAL_PORTS = (8080, 16514, 58880, 8200, 8888, 32400)
    # 常见设备描述路径
    _MANUAL_PATHS = ("/description.xml", "/root.xml", "/")
    _MANUAL_TIMEOUT = 2.5

    def add_device_by_target(self, target: str) -> Device:
        """按 IP / IP:端口 / 完整描述地址直接抓取设备并登记

        组播被本机防火墙（如 macOS Little Snitch）按应用拦截时，SSDP 永远
        收不到 NOTIFY，但出站 TCP 是通的——手动添加走的就是这条路。
        """
        target = (target or "").strip()
        if not target:
            raise ValueError("请输入设备 IP 或描述地址")

        location, host, port = self._resolve_manual_target(target)
        xml_text, used_port, used_url = self._fetch_description(location, host, port)
        device = self._parse_device_description(
            xml_text, host, port=used_port, location=used_url)
        if not device:
            # 文案要点名准入条件：用户手输的很可能是路由器/NAS 的后台地址，
            # 那些机器确实有 UPnP 描述，说「不是有效的 DLNA 设备描述」会让人以为填错了格式
            raise ValueError("该地址不能投屏：设备描述里必须声明 AVTransport 服务"
                             "（电视/盒子/带投屏的音箱才有，路由器与 NAS 没有）")

        if (not device.ip or device.ip in ("127.0.0.1", "0.0.0.0")) and host:
            device.ip = host
        if self._should_ignore_device and self._should_ignore_device(device.id):
            raise ValueError(f"设备 {device.display_name} 已被隐藏，请先取消隐藏")

        new_device = False
        self._mark_seen(device.id)
        with self._lock:
            old = self._devices.get(device.id)
            if old is None:
                self._devices[device.id] = device
                new_device = True
            else:
                if old.custom_name:
                    device.custom_name = old.custom_name
                self._devices[device.id] = device
        if new_device and self._on_device_found:
            self._on_device_found(device)
        logger.info(f"Manually added device: {device.display_name} ({device.ip})")
        return device

    def _resolve_manual_target(self, target: str):
        """返回 (location 或 None, host, port)"""
        import re as _re
        from urllib.parse import urlparse
        if target.lower().startswith(("http://", "https://")):
            parsed = urlparse(target)
            return (target, parsed.hostname or "", parsed.port or 80)
        m = _re.fullmatch(r"(?P<host>[0-9A-Za-z_.\-]+)(?::(?P<port>\d{1,5}))?",
                         target)
        if not m:
            raise ValueError(f"无法识别的地址：{target}")
        return (None, m.group("host"),
                int(m.group("port")) if m.group("port") else None)

    def _fetch_description(self, location: Optional[str], host: str,
                           port: Optional[int]):
        """抓取设备描述 XML，返回 (xml, 端口, 命中的 URL)

        未直接命中时并发穷举常见端口×路径：串行穷举最坏 12 次 × 超时，
        会顶穿前端 60 秒命令超时，并发后上限就是一个探测超时。
        命中的 URL 要随 Device 传下去——控制地址是相对它解析的。
        """
        from urllib.parse import urlparse
        from concurrent.futures import ThreadPoolExecutor, as_completed
        if location:
            candidates = [location]
        else:
            ports = (port,) if port else self._MANUAL_PORTS
            candidates = [f"http://{host}:{p}{path}"
                          for p in ports for path in self._MANUAL_PATHS]

        def _probe(url):
            try:
                resp = requests.get(url, timeout=self._MANUAL_TIMEOUT,
                                    proxies=_NO_PROXY)
                if resp.status_code != 200:
                    return url, None, f"HTTP {resp.status_code}"
                resp.encoding = resp.apparent_encoding or "utf-8"
                if "<device" not in resp.text.lower():
                    return url, None, "响应中没有 UPNP 设备描述"
                return url, resp.text, None
            except Exception as e:
                return url, None, str(e)

        errors = []
        with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as ex:
            futures = [ex.submit(_probe, u) for u in candidates]
            for fut in as_completed(futures):
                url, text, err = fut.result()
                if text is not None:
                    for other in futures:
                        other.cancel()
                    return text, (urlparse(url).port or 80), url
                errors.append(f"{url}: {err}")
        raise ValueError(
            "读取设备描述失败：" + (errors[0] if errors else "无响应"))

    def get_usn(self) -> str:
        return self._usn

    def get_ip(self) -> str:
        return self._ip

    def get_port(self) -> int:
        return self._port
