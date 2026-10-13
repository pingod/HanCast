"""原样 dump CGWindowListCopyWindowInfo，用来核对 darwin_windows() 的过滤条件

为什么不直接用 pyobjc：这台机器没装 Quartz 绑定，而打包 sidecar 走的就是
ctypes+CoreFoundation，这里必须复刻它的读取路径，否则差异说明不了问题。
"""

import ctypes
import ctypes.util
import sys

cg = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreGraphics"))
cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))

CFStringRef = ctypes.c_void_p
# 指针型返回值必须显式声明：默认 restype 是 c_int，64 位指针会被截断成垃圾，
# 后面拿它当 CFTypeRef 用就是段错误
cg.CGWindowListCopyWindowInfo.restype = ctypes.c_void_p
cg.CGWindowListCopyWindowInfo.argtypes = [ctypes.c_uint32, ctypes.c_uint64]
cf.CFStringCreateWithCString.restype = CFStringRef
cf.CFStringCreateWithCString.argtypes = [CFStringRef, ctypes.c_char_p, ctypes.c_uint32]
cf.CFArrayGetCount.restype = ctypes.c_long
cf.CFArrayGetCount.argtypes = [CFStringRef]
cf.CFArrayGetValueAtIndex.restype = CFStringRef
cf.CFArrayGetValueAtIndex.argtypes = [CFStringRef, ctypes.c_long]
cf.CFDictionaryGetValue.restype = CFStringRef
cf.CFDictionaryGetValue.argtypes = [CFStringRef, CFStringRef]
cf.CFNumberGetType.restype = ctypes.c_int
cf.CFNumberGetType.argtypes = [CFStringRef]
cf.CFNumberGetValue.restype = ctypes.c_bool
cf.CFNumberGetValue.argtypes = [CFStringRef, ctypes.c_int, ctypes.c_void_p]
cf.CFStringGetCStringPtr.restype = ctypes.c_char_p
cf.CFStringGetCStringPtr.argtypes = [CFStringRef, ctypes.c_uint32]
cf.CFStringGetLength.restype = ctypes.c_long
cf.CFStringGetLength.argtypes = [CFStringRef]
cf.CFStringGetCString.restype = ctypes.c_bool
cf.CFStringGetCString.argtypes = [CFStringRef, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
cf.CFGetTypeID.restype = ctypes.c_ulong
cf.CFGetTypeID.argtypes = [CFStringRef]
cf.CFRelease.argtypes = [CFStringRef]
cf.CFRelease.restype = None
kCFStringEncodingUTF8 = 0x08000100


def s(text):
    return cf.CFStringCreateWithCString(None, text.encode(), kCFStringEncodingUTF8)


def as_str(obj):
    if not obj:
        return None
    p = cf.CFStringGetCStringPtr(obj, kCFStringEncodingUTF8)
    if p:
        return p.decode("utf-8", "replace")
    n = cf.CFStringGetLength(obj)
    buf = ctypes.create_string_buffer(4 * n + 1)
    if not cf.CFStringGetCString(obj, buf, len(buf), kCFStringEncodingUTF8):
        return None
    return buf.value.decode("utf-8", "replace")


def as_num(obj, double=False):
    if not obj:
        return None
    # kCFNumberDoubleType=6 / kCFNumberSInt64Type=4：写 5（SInt32）只会留下
    # 5e-315 这种非规格化垃圾，正是打包 sidecar 里注释记录过的同一个坑
    val = ctypes.c_double() if double else ctypes.c_longlong()
    if not cf.CFNumberGetValue(obj, 6 if double else 4, ctypes.byref(val)):
        return None
    return float(val.value) if double else int(val.value)


keys = {n: ctypes.c_void_p.in_dll(cg, n) for n in
        ("kCGWindowNumber", "kCGWindowOwnerName", "kCGWindowName",
         "kCGWindowBounds", "kCGWindowLayer", "kCGWindowOwnerPID")}
bounds_keys = {k: s(k) for k in ("X", "Y", "Width", "Height")}

arr = cg.CGWindowListCopyWindowInfo(1 | 1 << 4, 0)
count = cf.CFArrayGetCount(arr)
print(f"raw window count = {count}")
for i in range(count):
    info = cf.CFArrayGetValueAtIndex(arr, i)
    num = as_num(cf.CFDictionaryGetValue(info, keys["kCGWindowNumber"]))
    owner = as_str(cf.CFDictionaryGetValue(info, keys["kCGWindowOwnerName"]))
    name = as_str(cf.CFDictionaryGetValue(info, keys["kCGWindowName"]))
    layer = as_num(cf.CFDictionaryGetValue(info, keys["kCGWindowLayer"]))
    b = cf.CFDictionaryGetValue(info, keys["kCGWindowBounds"])
    rect = tuple(as_num(cf.CFDictionaryGetValue(b, bounds_keys[k]), double=True)
                 for k in ("X", "Y", "Width", "Height")) if b else None
    print(f"  num={num:.0f} layer={layer} owner={owner!r} name={name!r} rect={rect}")
cf.CFRelease(arr)
sys.stdout.flush()
