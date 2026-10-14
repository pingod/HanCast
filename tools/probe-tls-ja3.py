#!/usr/bin/env python3
"""量 TLS 客户端指纹（JA3）：本机起一个只读 ClientHello 的裸 TLS 服务

为什么不用公网指纹站：判据必须是「同一台机器、同一个网络出口，换客户端前后 JA3
有没有变」，把它交给一个第三方站点的缓存与实现细节，测出来的差异说不清是谁的。
这里服务端不做握手，收到 ClientHello 就解析并断开，因此不需要证书，也不出网。

用法（用后台 venv 的 python，那里装了 curl_cffi）：
    hancast-backend/.venv/bin/python tools/probe-tls-ja3.py
    hancast-backend/.venv/bin/python tools/probe-tls-ja3.py --target chrome124
    hancast-backend/.venv/bin/python tools/probe-tls-ja3.py --client requests

判据：
  - requests(urllib3/OpenSSL) 与 curl_cffi(impersonate) 的 JA3 必须不同；
  - curl_cffi 的 ClientHello 里应出现 GREASE（0x?a?a 模式）——urllib3 永远不发，
    这是 Chrome 伪装的硬特征，也是风控最看重的一项。
"""

import argparse
import hashlib
import os
import socket
import ssl
import struct
import sys
import threading


# ────────────────────────────── ClientHello 解析 ──────────────────────────────

def _grease(v: int) -> bool:
    """GREASE 值形如 0x?a?a（高字节与低字节相等且 nibble 相同）"""
    hi, lo = (v >> 8) & 0xFF, v & 0xFF
    return hi == lo and (hi & 0x0F) == 0x0A


def parse_client_hello(buf: bytes) -> dict:
    """从 TLS 记录里取**最后一个** ClientHello（TLS 1.3 会有 HRR 后的第二个）"""
    hellos = []
    off = 0
    while off + 5 <= len(buf):
        rtype = buf[off]
        rlen = struct.unpack("!H", buf[off + 3:off + 5])[0]
        body = buf[off + 5:off + 5 + rlen]
        if len(body) < rlen:
            break
        if rtype == 0x16:
            # 一个记录里可能串多个 handshake 消息
            p = 0
            while p + 4 <= len(body):
                htype = body[p]
                hlen = struct.unpack("!I", b"\x00" + body[p + 1:p + 4])[0]
                hbody = body[p + 4:p + 4 + hlen]
                if htype == 0x01 and len(hbody) >= 43:
                    hellos.append(hbody)
                p += 4 + hlen
        off += 5 + rlen
    if not hellos:
        raise ValueError("缓冲里没有找到 ClientHello")
    h = hellos[-1]

    # client_version 是 2 字节（0x0303 = 771，JA3 的第一项），别按 1 字节读
    client_version = struct.unpack("!H", h[0:2])[0]
    pos = 2 + 32  # 版本 + random
    sid_len = h[pos]
    pos += 1 + sid_len
    cs_len = struct.unpack("!H", h[pos:pos + 2])[0]
    pos += 2
    cipher_suites = [struct.unpack("!H", h[pos + i:pos + i + 2])[0]
                     for i in range(0, cs_len, 2)]
    pos += cs_len
    comp_len = h[pos]
    pos += 1 + comp_len

    exts, alpn, supported_versions = {}, [], []
    if pos + 2 <= len(h):
        ext_total = struct.unpack("!H", h[pos:pos + 2])[0]
        pos += 2
        end = pos + ext_total
        while pos + 4 <= min(end, len(h)):
            etype, elen = struct.unpack("!HH", h[pos:pos + 4])
            edata = h[pos + 4:pos + 4 + elen]
            exts[etype] = edata
            if etype == 0x002B:  # supported_versions（43）
                supported_versions = [struct.unpack("!H", edata[i:i + 2])[0]
                                      for i in range(1, len(edata), 2)]
            elif etype == 0x0010:  # ALPN（16）：2 字节列表长度 + 若干「1 字节长度 + 协议名」
                p2 = 2
                while p2 + 1 <= len(edata):
                    ln = edata[p2]
                    alpn.append(edata[p2 + 1:p2 + 1 + ln].decode("latin-1"))
                    p2 += 1 + ln
            pos += 4 + elen

    curves, formats = [], []
    if 0x000A in exts:  # supported_groups（含 GREASE）
        data = exts[0x000A]
        ln = struct.unpack("!H", data[0:2])[0]
        curves = [struct.unpack("!H", data[2 + i:4 + i])[0]
                  for i in range(0, ln, 2)]
    if 0x000B in exts:  # ec_point_formats
        data = exts[0x000B]
        ln = data[0]
        formats = list(data[1:1 + ln])

    return {
        "client_version": client_version,
        "cipher_suites": cipher_suites,
        "extensions": sorted(exts),
        "curves": curves,
        "formats": formats,
        "grease_ciphers": sum(1 for c in cipher_suites if _grease(c)),
        "grease_exts": sum(1 for e in exts if _grease(e)),
        "grease_curves": sum(1 for c in curves if _grease(c)),
        "alpn": alpn,
        "supported_versions": supported_versions,
        "sni": exts.get(0x0000, b"") and _sni(exts[0x0000]),
    }


def _sni(data: bytes) -> str:
    if len(data) < 5:
        return ""
    ln = struct.unpack("!H", data[3:5])[0]
    return data[5:5 + ln].decode("latin-1")


def ja3(hello: dict) -> str:
    """标准 JA3 串：版本,密钥套件,扩展,椭圆曲线,点格式（GREASE 剔除，按原顺序）"""
    cs = ",".join(f"{c:04x}" for c in hello["cipher_suites"] if not _grease(c))
    ext = ",".join(str(e) for e in hello["extensions"] if not _grease(e))
    cv = ",".join(str(c) for c in hello["curves"] if not _grease(c))
    pf = ",".join(str(f) for f in hello["formats"])
    return f"{hello['client_version']},{cs},{ext},{cv},{pf}"


# ────────────────────────────── 取证服务 ──────────────────────────────

class HelloCatcher:
    """只听一个端口，每来一个连接读够 ClientHello 就解析、断开（不做握手）"""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(4)
        self.port = self.sock.getsockname()[1]
        self.results = []
        self._stop = threading.Event()
        self._done = threading.Event()
        self._n_expected = 0
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def start(self, expected: int):
        self._n_expected = expected
        self._thread.start()

    def _loop(self):
        self.sock.settimeout(0.5)
        served = 0
        while served < self._n_expected and not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            buf = b""
            conn.settimeout(3.0)
            try:
                while True:
                    try:
                        chunk = conn.recv(65536)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    buf += chunk
                    # 读够一个完整记录即可（ClientHello 是第一个记录）
                    if len(buf) >= 5:
                        rlen = struct.unpack("!H", buf[3:5])[0]
                        if len(buf) >= 5 + rlen:
                            break
                    if len(buf) > 200 * 1024:
                        break
            except OSError:
                pass
            finally:
                served += 1
                try:
                    if buf:
                        self.results.append(parse_client_hello(buf))
                    else:
                        self.results.append(None)
                except Exception as e:
                    self.results.append(f"解析失败: {e}（捕获 {len(buf)} 字节，"
                                        f"已存 /tmp/ja3-fail-{served}.bin）")
                    try:
                        open(f"/tmp/ja3-fail-{served}.bin", "wb").write(buf)
                    except OSError:
                        pass
                try:
                    conn.close()
                except OSError:
                    pass
        self._done.set()

    def wait(self, timeout: float) -> list:
        self._done.wait(timeout)
        self.stop()
        return self.results

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


# ────────────────────────────── 客户端 ──────────────────────────────

def probe_requests(port: int, ua: str) -> str:
    import requests
    try:
        requests.get(f"https://127.0.0.1:{port}/hello", timeout=6,
                     verify=False, headers={"User-Agent": ua},
                     proxies={"http": None, "https": None})
    except Exception as e:
        return f"{type(e).__name__}"
    return "ok"


def probe_curl_cffi(port: int, ua: str, target: str) -> str:
    from curl_cffi import requests as cf
    try:
        cf.get(f"https://127.0.0.1:{port}/hello", timeout=6, verify=False,
               impersonate=target, headers={"User-Agent": ua},
               proxies={"http": None, "https": None})
    except Exception as e:
        return f"{type(e).__name__}"
    return "ok"


def probe_hancast(port: int, ua: str) -> str:
    """走 HanCast 真实代码路径（utils.httpclient.make_session）而不是直接调库

    这条才是验收点：解析链路换客户端后，指纹确实按 UA 换掉了 ——
    直接 cf.get(impersonate=...) 只证明库能用，证明不了我们的接线。
    """
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    "hancast-backend"))
    from hancast_sidecar.utils import httpclient as hc
    session = hc.make_session()
    kwargs = {"timeout": 6, "verify": False, "headers": {"User-Agent": ua}}
    # 两种会话「不走代理」的写法不一样（requests 认 None，curl 认空串）
    kwargs["proxies"] = (dict(hc._NO_PROXY)
                         if isinstance(session, hc.CurlSession)
                         else {"http": None, "https": None})
    try:
        session.get(f"https://127.0.0.1:{port}/hello", **kwargs).close()
    except Exception as e:
        return f"{type(e).__name__}"
    return f"ok（会话 {type(session).__name__}，指纹 " \
           f"{hc.fingerprint_for(ua) or '未伪装'}）"


def probe_urllib_raw(port: int) -> str:
    """直接 ssl.wrap 一个 socket：证明「Python 原生 TLS」这一路就是 OpenSSL 指纹"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        raw = socket.create_connection(("127.0.0.1", port), timeout=6)
        s = ctx.wrap_socket(raw, server_hostname="localhost")
        s.do_handshake()
        s.close()
    except Exception as e:
        return f"{type(e).__name__}"
    return "ok"


def report(name: str, hello, base: dict) -> None:
    if isinstance(hello, str) or hello is None:
        print(f"  {name:22} → 没抓到 ClientHello（{hello}）")
        return
    s = ja3(hello)
    h = hashlib.md5(s.encode()).hexdigest()
    print(f"  {name:22} JA3={h}")
    print(f"  {'':22}   版本字段={hello['client_version']} "
          f"supported_versions={hello['supported_versions']} "
          f"ALPN={hello['alpn']}")
    print(f"  {'':22}   密码套件 {len(hello['cipher_suites'])} 个"
          f"（GREASE {hello['grease_ciphers']}）/ "
          f"扩展 {len(hello['extensions'])} 个（GREASE {hello['grease_exts']}）/ "
          f"曲线 {len(hello['curves'])} 个（GREASE {hello['grease_curves']}）")
    if base is not None:
        print(f"  {'':22}   与 {base[0]} 不同: "
              f"{'是' if base[1] != s else '否（指纹没换！）'}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default="chrome124",
                    help="curl_cffi 的 impersonate 目标（如 chrome124/safari17_0）")
    ap.add_argument("--client", default="both",
                    choices=["both", "requests", "curl_cffi", "hancast"])
    ap.add_argument("--ua", default="")
    args = ap.parse_args()

    ua = args.ua or ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

    modes = []
    if args.client in ("both", "requests"):
        modes.append(("requests(urllib3)", lambda p: probe_requests(p, ua)))
        modes.append(("ssl 原生握手", lambda p: probe_urllib_raw(p)))
    if args.client in ("both", "curl_cffi"):
        modes.append((f"curl_cffi[{args.target}]",
                      lambda p: probe_curl_cffi(p, ua, args.target)))
    if args.client in ("both", "hancast"):
        modes.append(("HanCast make_session()", lambda p: probe_hancast(p, ua)))

    catcher = HelloCatcher()
    catcher.start(len(modes))
    print(f"取证服务 127.0.0.1:{catcher.port}，逐个客户端探测 {len(modes)} 次：")

    # verify=False 会打印 urllib3 的 InsecureRequestWarning，与结论无关
    import warnings
    warnings.filterwarnings("ignore")

    for name, fn in modes:
        fn(catcher.port)

    res = catcher.wait(timeout=30)
    if len(res) < len(modes):
        print(f"  ! 只抓到 {len(res)}/{len(modes)} 份 ClientHello")

    base = None
    for (name, _), hello in zip(modes, res):
        if isinstance(hello, str) or hello is None:
            print(f"  {name:22} → 未捕获：{hello}")
            continue
        s = ja3(hello)
        report(name, hello, base)
        if base is None:
            base = (name, s)

    hashes = [hashlib.md5(ja3(h).encode()).hexdigest()
              for h in res if isinstance(h, dict)]
    uniq = len(set(hashes))
    if len(modes) < 2:
        # 单探针没有对照物，说「改动无效」是假的——只报本次指纹。
        print(f"\n单探针模式：抓到 {len(hashes)} 份指纹 {hashes[0] if hashes else '（无）'}"
              f"（加 --client both 才能对照）")
    else:
        print(f"\n抓到 {len(hashes)} 份指纹，去重后 {uniq} 种 → "
              f"{'各客户端指纹不同，伪装生效' if uniq > 1 else '所有客户端指纹一致，改动无效'}")
    return 0 if hashes else 1


if __name__ == "__main__":
    sys.exit(main())
