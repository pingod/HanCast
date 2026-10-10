"""
Sidecar 协议输出 —— stdout 单写者

Rust 端逐行读取 stdout 并把每一行当作一个完整的 JSON 对象解析。
侧车里有多个线程会主动推送事件（SSDP 设备发现、MPV 状态回调、
Guard 确认请求），它们与主循环的命令响应并发写 stdout。

`print()` 不是原子的：两个线程同时写会产生行内交错，例如

    {"event": "device_found", "data": {"id": "abc{"id": 42, "succ

这一行对 Rust 而言既不是合法事件也不是合法响应，`reader_loop`
解析失败后会静默丢弃，对应的 `send_command` 就永远等不到回包。

因此所有 stdout 写入都必须经过本模块的同一把锁，保证每一行
完整落盘。注意：锁必须在**同一进程内**共享，所以这里是模块级
单例，而不是每个调用点各自创建。
"""

import json
import sys
import threading

# 所有 stdout 写入共用的一把锁。
_write_lock = threading.Lock()


def _write_line(payload: str) -> None:
    """在锁保护下写入一行并立即 flush。

    显式使用 '\\n' 而不是 print()，避免 print 追加第二个换行；
    同时 flush 保证 Rust 端不会因为缓冲而等待。
    """
    with _write_lock:
        sys.stdout.write(payload + '\n')
        sys.stdout.flush()


def emit_response(request_id, success: bool, data=None, error=None) -> None:
    """发送命令响应。

    Args:
        request_id: 请求 id，原样回传
        success: 是否成功
        data: 成功时的数据
        error: 失败时的错误信息
    """
    response = {"id": request_id, "success": success}
    if success:
        response["data"] = data
    else:
        response["error"] = error
    _write_line(json.dumps(response, ensure_ascii=False))


def emit_event(event_name: str, data=None) -> None:
    """发送事件（无请求 id，Rust 端据此区分事件与响应）。"""
    event = {"event": event_name, "data": data or {}}
    _write_line(json.dumps(event, ensure_ascii=False))
