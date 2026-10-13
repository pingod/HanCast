"""macOS 系统声音：CoreAudio Audio Tap（macOS 14.2+）

为什么不是 FFmpeg 直接采：avfoundation 只有「输入设备」，而系统声音是输出。
以前只有两条路——用户自己装 BlackHole 虚拟声卡并在「音频 MIDI 设置」里配多输出，
或者 ScreenCaptureKit（要录屏权限，且 ffmpeg 里没有）。

macOS 14.2 起 CoreAudio 自带 Audio Tap：`AudioHardwareCreateProcessTap` 建一个
「正在播放的其他进程」的环回对象，再用它的 UID 作为一个聚合设备的 taps 组成，
这个聚合设备就是一个普通的音频输入设备，avfoundation / ffmpeg 都能直接选。

用 ctypes 直调 CoreAudio + ObjC 运行时，不引新的 Rust/Python 依赖：设备枚举和
会话生命周期本来就都在 sidecar 这一侧。
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import struct
import time
from typing import Dict, List, Optional

logger = logging.getLogger("hancast.screencast.macos_tap")

# 用户在「系统声音」下拉里看到的名字
TAP_DEVICE_NAME = "HanCast 系统声音"
TAP_DEVICE_UID = "com.hancast.system-audio"

_UTF8 = 0x08000100
_kGlobalScope = struct.unpack(">I", b"glob")[0]
_kTapUID = struct.unpack(">I", b"tuid")[0]


class _Addr(ctypes.Structure):
    _fields_ = [("mSelector", ctypes.c_uint32),
                ("mScope", ctypes.c_uint32),
                ("mElement", ctypes.c_uint32)]


def _fourcc(name: str) -> int:
    return struct.unpack(">I", name.encode("ascii").ljust(4, b"\0"))[0]


def _cc(value: int) -> str:
    return "".join(chr((value >> s) & 255) for s in (24, 16, 8, 0)).strip()


class _Objc:
    """最小 ObjC/CoreFoundation 桥：只用定长选择子（不做变参 msgSend）"""

    def __init__(self):
        self.rt = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
        self.cf = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        self.rt.objc_getClass.restype = ctypes.c_void_p
        self.rt.objc_getClass.argtypes = [ctypes.c_char_p]
        self.rt.sel_registerName.restype = ctypes.c_void_p
        self.rt.sel_registerName.argtypes = [ctypes.c_char_p]
        self.rt.objc_msgSend.restype = ctypes.c_void_p
        self.rt.objc_msgSend.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self.cf.CFRetain.restype = ctypes.c_void_p
        self.cf.CFRetain.argtypes = [ctypes.c_void_p]
        self.cf.CFRelease.argtypes = [ctypes.c_void_p]
        self.cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        self.cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                                      ctypes.c_uint32]
        self.cf.CFStringGetCStringPtr.restype = ctypes.c_char_p
        self.cf.CFStringGetCStringPtr.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self.cf.CFStringGetLength.restype = ctypes.c_long
        self.cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
        self.cf.CFStringGetCString.restype = ctypes.c_bool
        self.cf.CFStringGetCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p,
                                               ctypes.c_long, ctypes.c_uint32]
        self._classes: Dict[bytes, Optional[int]] = {}
        self._sels: Dict[bytes, int] = {}
        self._keep: List[int] = []
        self._msgs: Dict[tuple, object] = {}
        # objc_msgSend 的地址：必须转成裸整数再包 CFUNCTYPE。直接把 ctypes
        # 的函数对象交给 CFUNCTYPE，它会沿用原 argtypes（只有 self/_cmd），
        # 结果每条消息的真实参数全被丢掉——addObject: 收到 nil，initWithInt:
        # 读出垃圾指针，表现成「CoreAudio 桥莫名其妙不工作」。
        self._msg_addr = ctypes.cast(self.rt.objc_msgSend, ctypes.c_void_p).value
        # 必须放在最后一行：alloc/init 依赖 send()，而下面几条例如 [arr description]
        # 返回的是 autorelease 对象，没有池就直接 segfault。
        self._pool = self.send(self.alloc("NSAutoreleasePool"), "init")

    def cls(self, name: str) -> int:
        key = name.encode()
        if key not in self._classes:
            self._classes[key] = self.rt.objc_getClass(key) or None
        return self._classes[key] or 0

    def sel(self, name: str) -> int:
        if name.encode() not in self._sels:
            self._sels[name.encode()] = self.rt.sel_registerName(name.encode())
        return self._sels[name.encode()]

    def send(self, target: int, sel_name: str, *args,
             restype=ctypes.c_void_p, argtypes=None):
        """objc_msgSend 的定长特化：按签名缓存一个 C 函数指针"""
        types = tuple(argtypes or [ctypes.c_void_p] * len(args))
        key = (sel_name, restype, types)
        fn = self._msgs.get(key)
        if fn is None:
            fn = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p,
                                  *types)(self._msg_addr)
            self._msgs[key] = fn
        out = []
        for a, t in zip(args, types):
            out.append(a if t is not ctypes.c_void_p or isinstance(a, int)
                       else ctypes.cast(a, ctypes.c_void_p).value)
        return fn(target, self.sel(sel_name), *out)

    def alloc(self, class_name: str) -> int:
        cls = self.cls(class_name)
        if not cls:
            raise RuntimeError(f"运行时缺少类 {class_name}")
        return self.send(cls, "alloc")

    def string(self, value: str) -> int:
        """CFString（Create 规则）：留一份引用，进程生命周期内不释放"""
        ref = self.cf.CFStringCreateWithCString(None, value.encode("utf-8"), _UTF8)
        if not ref:
            raise RuntimeError("CFString 创建失败")
        self._keep.append(ref)
        return ctypes.cast(ref, ctypes.c_void_p).value

    def text(self, ref: int) -> str:
        p = self.cf.CFStringGetCStringPtr(ctypes.c_void_p(ref), _UTF8)
        if p:
            return p.decode("utf-8", "replace")
        n = self.cf.CFStringGetLength(ctypes.c_void_p(ref))
        buf = ctypes.create_string_buffer(n * 4 + 4)
        ok = self.cf.CFStringGetCString(ctypes.c_void_p(ref), buf,
                                        ctypes.c_long(len(buf)), _UTF8)
        return buf.value.decode("utf-8", "replace") if ok else ""

    def number(self, value: int) -> int:
        num = self.send(self.alloc("NSNumber"), "initWithInt:", value,
                        argtypes=[ctypes.c_int])
        self._keep.append(num)
        return num

    def array(self, items) -> int:
        arr = self.send(self.alloc("NSMutableArray"), "init")
        self._keep.append(arr)
        for it in items:
            self.send(arr, "addObject:", it)
        return arr

    def dictionary(self, pairs: Dict[str, int]) -> int:
        d = self.send(self.alloc("NSMutableDictionary"), "init")
        self._keep.append(d)
        for k, v in pairs.items():
            self.send(d, "setObject:forKey:", v, self.string(k))
        return d


_OC: Optional[_Objc] = None
_CA = None
_state: Dict = {"objc": None, "tap_id": 0, "agg_id": 0, "uid": ""}


def _objc() -> Optional[_Objc]:
    global _OC, _CA
    if _OC is None:
        if platform.system() != "Darwin":
            return None
        try:
            _OC = _Objc()
            _CA = ctypes.cdll.LoadLibrary(
                "/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
        except Exception as e:  # noqa: BLE001 - 框架加载不了就当该功能不存在
            logger.info(f"[ScreenCast] CoreAudio 桥不可用: {e}")
            _OC = False
            return None
    return _OC or None


def supported() -> bool:
    """这台机器有没有 Audio Tap（14.2+ 才导出这个符号）"""
    if not _objc():
        return False
    return hasattr(_CA, "AudioHardwareCreateProcessTap")


def _prop_string(obj_id: int, selector: int) -> str:
    """读一个 CFString 属性。

    不能用「先问大小再取值」的两步写法：HAL 对定长属性在 outData 为 NULL 时
    直接回 'nope'，所以按指针宽度一次性取回。
    """
    oc = _OC
    addr = _Addr(selector, _kGlobalScope, 0)
    size = ctypes.c_uint32(ctypes.sizeof(ctypes.c_void_p))
    out = ctypes.c_void_p(0)
    rc = _CA.AudioObjectGetPropertyData(ctypes.c_uint32(obj_id), ctypes.byref(addr),
                                        ctypes.c_uint32(0), None, ctypes.byref(size),
                                        ctypes.byref(out))
    if rc != 0 or not out.value:
        name = "".join(chr((selector >> s) & 255) for s in (24, 16, 8, 0))
        raise RuntimeError(f"读属性 {name} 失败（OSStatus {rc:#x}）")
    ref = ctypes.cast(out, ctypes.c_void_p).value
    text = oc.text(ref)
    oc.cf.CFRelease(ctypes.c_void_p(ref))   # GetPropertyData 给的是 +1 引用
    return text


def _self_pids() -> List[int]:
    """sidecar 自己 + 拉起它的 App：这两个进程的声音不能被环回进来（否则自己的
    投屏音频会在流里形成回音）"""
    pids = [os.getpid()]
    ppid = os.getppid()
    if ppid > 1:
        pids.append(ppid)
    return pids


def _self_process_objects() -> List[int]:
    """本进程与宿主 App 在 CoreAudio 里的「进程对象 ID」。

    CATapDescription 的排除列表要的是 AudioObjectID（形如 kAudioProcessPropertyPID
    的那个对象），不是 unix pid：传 pid 进去会得到 '!obj'，整个环回直接建不出来。
    没播过声音的进程根本没有这个对象，所以这里天然把 sidecar 自己过滤掉了。
    """
    mine = set(_self_pids())
    out = []
    for obj_id in _prop_array(1, _fourcc("prs#")):
        try:
            if _prop_uint32(int(obj_id), _fourcc("ppid")) in mine:
                out.append(int(obj_id))
        except Exception:  # noqa: BLE001 - 进程刚好退出
            continue
    return out


def _prop_uint32(obj_id: int, selector: int) -> int:
    addr = _Addr(selector, _kGlobalScope, 0)
    size = ctypes.c_uint32(4)
    val = ctypes.c_uint32(0)
    rc = _CA.AudioObjectGetPropertyData(ctypes.c_uint32(obj_id), ctypes.byref(addr),
                                        ctypes.c_uint32(0), None, ctypes.byref(size),
                                        ctypes.byref(val))
    if rc != 0:
        raise RuntimeError(f"读属性 {_cc(selector)} 失败（OSStatus {rc:#x}）")
    return val.value


def _prop_array(obj_id: int, selector: int) -> List[int]:
    """读一个 AudioObjectID 数组属性。

    不能用「先传 NULL 问大小」的常规两步法：这台机器的 HAL 对 outData=NULL 直接
    回 'nope'（非法操作），所以给一个足够大的缓冲区一次读回，读满了再翻倍重试。
    """
    addr = _Addr(selector, _kGlobalScope, 0)
    cap = 64
    while True:
        size = ctypes.c_uint32(cap * 4)
        buf = (ctypes.c_uint32 * cap)()
        rc = _CA.AudioObjectGetPropertyData(
            ctypes.c_uint32(obj_id), ctypes.byref(addr),
            ctypes.c_uint32(0), None, ctypes.byref(size), buf)
        if rc != 0:
            raise RuntimeError(f"读数组属性失败（OSStatus {rc:#x}）")
        n = min(size.value // 4, cap)
        if size.value < cap * 4:
            return list(buf)[:n]
        cap *= 4


def _devices() -> List[int]:
    return _prop_array(1, _fourcc("dev#"))


def _input_streams(obj_id: int) -> int:
    """对象在 input scope 下的流数量（-1 = 读不了）

    环回只有挂成聚合设备的子环回（AudioSubTap）才会表现为输入流；组合字典里
    记了 UID 不代表挂上了，所以判断成败要看这里的数量。
    """
    addr = _Addr(_fourcc("slay"), _fourcc("inpt"), 0)
    size = ctypes.c_uint32(1024)
    buf = (ctypes.c_uint8 * 1024)()
    rc = _CA.AudioObjectGetPropertyData(ctypes.c_uint32(obj_id), ctypes.byref(addr),
                                        ctypes.c_uint32(0), None, ctypes.byref(size),
                                        buf)
    if rc != 0:
        return -1
    return ctypes.cast(buf, ctypes.POINTER(ctypes.c_uint32)).contents.value


def _device_uid(device_id: int) -> str:
    try:
        return _prop_string(device_id, _fourcc("uid "))
    except Exception:  # noqa: BLE001 - 设备正在消失
        return ""


def _stale_aggregate() -> int:
    """上一轮残留的「HanCast 系统声音」。

    聚合设备是系统级的：建它的那个进程退出后设备还留在系统里，而它引用的环回
    已经死了——留下就是一块永远静音的假设备，还会让同 UID 的二次创建直接报
    'nope'。所以每次建之前先把它清掉。
    """
    for dev in _devices():
        if _device_uid(dev) == TAP_DEVICE_UID:
            return dev
    return 0


def _destroy_object(fn_name: str, obj_id: int):
    if not obj_id:
        return
    try:
        fn = getattr(_CA, fn_name)
        fn.restype = ctypes.c_int32
        fn.argtypes = [ctypes.c_uint32]
        rc = fn(ctypes.c_uint32(obj_id))
        if rc != 0:
            logger.debug(f"[ScreenCast] {fn_name} → {rc:#x}")
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[ScreenCast] {fn_name}: {e}")


def _create_tap(oc: _Objc) -> int:
    try:
        exclude = _self_process_objects()
    except Exception as e:  # noqa: BLE001 - 枚举失败就别排除，顶多把自己的声音也收进来
        logger.debug(f"[ScreenCast] 音频进程枚举失败，环回不排除自身: {e}")
        exclude = []
    objs = oc.array([oc.number(p) for p in exclude])
    desc = oc.send(oc.alloc("CATapDescription"),
                   "initStereoGlobalTapButExcludeProcesses:", objs)
    if not desc:
        raise RuntimeError("CATapDescription 建不出来")
    oc.send(desc, "setName:", oc.string(TAP_DEVICE_NAME))
    # 这一句是整个环回能不能被别的过程看见的关键：CATapDescription 默认建出来
    # 的是「私有环回」，只有创建者自己读得到。私有环回挂进聚合设备时，HAL 会把
    # 子环回对象建出来（'atap' 非空），但那台聚合设备只对本进程可见，ffmpeg
    # 是另一个进程，于是 avfoundation 里永远没有它——表现为「设备建好了却选不到」。
    oc.send(desc, "setPrivate:", 0, argtypes=[ctypes.c_int8])  # arm64 的 BOOL 是 int8
    out = ctypes.c_uint32(0)
    fn = _CA.AudioHardwareCreateProcessTap
    fn.restype = ctypes.c_int32
    fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    rc = fn(ctypes.c_void_p(desc), ctypes.byref(out))
    if rc != 0:
        raise RuntimeError(f"AudioHardwareCreateProcessTap → OSStatus {rc:#x}")
    if not out.value:
        raise RuntimeError("AudioHardwareCreateProcessTap 成功但没有返回环回对象")
    return out.value


def _create_aggregate(oc: _Objc, tap_uid: str) -> int:
    desc = oc.dictionary({
        "name": oc.string(TAP_DEVICE_NAME),
        "uid": oc.string(TAP_DEVICE_UID),
        "taps": oc.array([oc.dictionary({"uid": oc.string(tap_uid)})]),
    })
    out = ctypes.c_uint32(0)
    fn = _CA.AudioHardwareCreateAggregateDevice
    fn.restype = ctypes.c_int32
    fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    rc = fn(ctypes.c_void_p(desc), ctypes.byref(out))
    if rc != 0:
        raise RuntimeError(f"AudioHardwareCreateAggregateDevice → OSStatus {rc:#x}")
    return out.value


def _settle_streams(dev: int, seconds: float = 2.0) -> int:
    """等设备把输入流建出来（注册是异步的），返回流数量"""
    deadline = time.monotonic() + seconds
    n = 0
    while time.monotonic() < deadline:
        n = _input_streams(dev)
        if n > 0:
            return n
        time.sleep(0.2)
    return n


def ensure_device() -> bool:
    """确保「HanCast 系统声音」输入设备在位（幂等）。

    True 表示设备已经建好，随后的 avfoundation 枚举就能看到它；False 表示这台
    机器用不了 Audio Tap（14.1 及以下），界面继续走 BlackHole 那条路。
    """
    oc = _objc()
    if not oc or not supported():
        return False
    if _state["agg_id"] and _input_streams(_state["agg_id"]) > 0:
        return True
    try:
        stale = _stale_aggregate()
        if stale:
            _destroy_object("AudioHardwareDestroyAggregateDevice", stale)
        _state["tap_id"] = _create_tap(oc)
        _state["uid"] = _prop_string(_state["tap_id"], _kTapUID)
        _state["agg_id"] = _create_aggregate(oc, _state["uid"])
        # 聚合设备注册是异步的，而且「建成功」不代表环回真的挂上了：只有
        # 子环回实例化出输入流，avfoundation 才会列出这台设备
        n = _settle_streams(_state["agg_id"])
        if n <= 0:
            raise RuntimeError("聚合设备里没有输入流（环回未挂载）")
        logger.info(f"[ScreenCast] 系统声音设备就绪：{TAP_DEVICE_NAME} "
                    f"(tap={_state['tap_id']} agg={_state['agg_id']} "
                    f"streams={n})")
        return True
    except Exception as e:  # noqa: BLE001 - 权限/版本不满足时不能拖垮整个投屏
        logger.info(f"[ScreenCast] CoreAudio 系统声音不可用: {e}")
        destroy_device()
        return False


def destroy_device():
    """销毁聚合设备与环回（sidecar 退出时调用；重复调用无害）"""
    if not (_objc() and _CA):
        _state.update({"tap_id": 0, "agg_id": 0, "uid": ""})
        return
    _destroy_object("AudioHardwareDestroyAggregateDevice", _state["agg_id"])
    _destroy_object("AudioHardwareDestroyProcessTap", _state["tap_id"])
    _state.update({"agg_id": 0, "tap_id": 0, "uid": ""})
    # 兜底：进程被强杀时最后这两行不会执行，残留设备由下一次 ensure_device 清
    try:
        stale = _stale_aggregate()
    except Exception:  # noqa: BLE001
        stale = 0
    if stale:
        _destroy_object("AudioHardwareDestroyAggregateDevice", stale)


def is_tap_device(name: str) -> bool:
    """设备名是不是我们建出来的那一路（用来在枚举结果里标注来源）"""
    return TAP_DEVICE_NAME in (name or "")
