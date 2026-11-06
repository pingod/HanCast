"""手机镜像会话：设备枚举、adb 无线连接、scrcpy 进程编排。

生命周期契约：
- 同一时刻只允许一个镜像会话（``start`` 在已有会话时直接报错）；
- ``start`` 返回时进程已启动并被观察过 ~1 秒：scrcpy 立刻退出（没设备、
  没授权、server 推不上去）会在返回里带 error，而不是留给用户一个空窗口；
- 会话中途退出由监视线程发 ``mirror_state_changed`` 事件，错误原因取
  stderr 最后几行——scrcpy 把 90% 的失败原因都打在 stderr 上。

窗口不由本模块创建：scrcpy 自己开一个 SDL 窗口。这样触控、声音、旋转、
自动重连都由 scrcpy 处理；HanCast 只负责「找工具、选设备、传参数、收尸」。
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from typing import Any, Optional

from ..utils import protocol_io
from . import tools

logger = logging.getLogger("hancast.mirror")

KEY_ADB = "mirror_adb_path"
KEY_SCRCPY = "mirror_scrcpy_path"

# scrcpy 启动后观察这么久再回报成功：绝大多数「立刻失败」（设备离线、
# server 版本不匹配、没有视频编码器）都在这段时间内退出。
_START_GRACE_SECONDS = 1.0
_STOP_GRACE_SECONDS = 5.0


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _truthy(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(value)


def build_scrcpy_args(scrcpy_path: str, params: dict, audio_supported: bool = True) -> list[str]:
    """把前端参数翻译成 scrcpy 命令行（纯函数，便于单测）。

    只传当前 scrcpy 版本认识的参数：2.0 之前没有音频转发，传 ``--no-audio``
    会直接报「unknown option」把会话打死，所以老版本一个音频参数都不加。
    """
    serial = str(params.get("serial") or "").strip()
    if not serial:
        raise ValueError("缺少设备序列号")

    args = [scrcpy_path, "--serial", serial]

    max_size = _as_int(params.get("max_size"), 0)
    if max_size > 0:
        args += ["--max-size", str(min(max_size, 4096))]

    max_fps = _as_int(params.get("max_fps"), 0)
    if max_fps > 0:
        args += ["--max-fps", str(min(max_fps, 240))]

    # 前端单位是 Mbps；scrcpy 要的是 "8M" 这种带单位的值
    bitrate = _as_int(params.get("video_bitrate"), 0)
    if bitrate > 0:
        args += ["--video-bit-rate", f"{min(bitrate, 200)}M"]

    if audio_supported and not _truthy(params.get("audio"), default=True):
        args.append("--no-audio")
    if not _truthy(params.get("control"), default=True):
        args.append("--no-control")
    if _truthy(params.get("turn_screen_off"), default=False):
        args.append("--turn-screen-off")
    if _truthy(params.get("stay_awake"), default=False):
        args.append("--stay-awake")
    if _truthy(params.get("always_on_top"), default=False):
        args.append("--always-on-top")

    title = str(params.get("window_title") or "").strip()
    if title:
        args += ["--window-title", title]
    return args


class MirrorManager:
    def __init__(self, config):
        self._config = config
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._monitor: Optional[threading.Thread] = None
        self._stderr: deque[str] = deque(maxlen=40)
        self._serial: Optional[str] = None
        self._model: Optional[str] = None
        self._started_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._stopping = False

    # ── 工具链 ──

    def _manual_paths(self) -> dict[str, Optional[str]]:
        return {
            "adb": self._config.get(KEY_ADB) or None,
            "scrcpy": self._config.get(KEY_SCRCPY) or None,
        }

    def tool_report(self, refresh: bool = False) -> dict:
        return tools.tool_status(self._manual_paths(), refresh=refresh)

    def status(self, refresh: bool = False) -> dict:
        report = self.tool_report(refresh=refresh)
        with self._lock:
            state = self._state_locked()
        state.update(
            {
                "ok": report["ok"],
                "platform": report["platform"],
                "tools": report["tools"],
                "install_commands": report["install_commands"],
                "scrcpy_audio_supported": report["scrcpy_audio_supported"],
                "scrcpy_audio_min_version": report["scrcpy_audio_min_version"],
            }
        )
        return state

    def set_tool_path(self, params: dict) -> dict:
        tool = str(params.get("tool") or "").strip().lower()
        if tool not in tools.TOOLS:
            return {"ok": False, "error": f"未知工具：{tool}"}
        raw = str(params.get("path") or "").strip()
        if raw:
            import os

            path = os.path.expanduser(raw)
            if not os.path.isfile(path):
                return {"ok": False, "error": f"文件不存在：{path}"}
            if sys.platform != "win32" and not os.access(path, os.X_OK):
                return {"ok": False, "error": f"没有执行权限：{path}"}
            self._config.set(KEY_ADB if tool == "adb" else KEY_SCRCPY, path)
        else:
            # 空路径 = 恢复自动检测（用户想清掉之前选错的文件）
            self._config.set(KEY_ADB if tool == "adb" else KEY_SCRCPY, "")
        self._config.save()
        tools.clear_cache()
        return {"ok": True, **self.status(refresh=True)}

    # ── 设备 ──

    def _adb(self) -> tuple[Optional[str], dict]:
        report = self.tool_report()
        adb = next(t for t in report["tools"] if t["key"] == "adb")
        return adb["path"] if adb["found"] else None, report

    def devices(self) -> dict:
        adb, report = self._adb()
        if not adb:
            return {
                "devices": [],
                "error": "未找到 adb：请安装 Android 平台工具，或在面板里指定 adb 路径",
                "tools": report["tools"],
            }
        code, out, err = tools.run_tool(adb, ["devices", "-l"], timeout=15.0)
        parsed = tools.parse_adb_devices(out + "\n" + err)
        if not parsed and code != 0:
            tail = (err or out).strip().splitlines()
            return {
                "devices": [],
                "error": tail[0] if tail else f"adb devices 失败（退出码 {code}）",
                "tools": report["tools"],
            }
        return {"devices": parsed, "error": None, "tools": report["tools"]}

    def connect(self, params: dict) -> dict:
        """`adb connect host:port` —— 无线调试已经配对成功过才有效。

        首次配对需要 `adb pair`（Android 11+ 的六位配对码），本面板不做——
        配对码是手机屏幕上的一次性信息，填进桌面弹窗不如让用户按系统提示操作。
        """
        adb, _report = self._adb()
        if not adb:
            return {"ok": False, "error": "未找到 adb，无法建立无线连接"}
        address = str(params.get("address") or "").strip()
        if not re.match(r"^([A-Za-z0-9._\-]+|\[[0-9a-fA-F:]+\]):\d{1,5}$", address):
            return {"ok": False, "error": "地址格式应为 IP:端口（例如 192.168.1.20:5555）"}
        code, out, err = tools.run_tool(adb, ["connect", address], timeout=15.0)
        text = (out + "\n" + err).strip()
        lower = text.lower()
        if code != 0 or ("connected to" not in lower and "already connected" not in lower):
            tail = text.splitlines()
            return {"ok": False, "error": tail[0] if tail else f"adb connect 失败（退出码 {code}）"}
        listing = self.devices()
        device = next((d for d in listing["devices"] if d["serial"] == address), None)
        return {"ok": True, "address": address, "device": device, "message": text}

    # ── 会话 ──

    def start(self, params: dict) -> dict:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                # 顺序要紧：state 里也有 error 字段，先铺 state 再写本次的失败原因
                return {**self._state_locked(), "ok": False, "error": "已有镜像会话在进行中，请先停止"}

            adb, report = self._adb()
            scrcpy = next(t for t in report["tools"] if t["key"] == "scrcpy")
            if not adb or not scrcpy["found"]:
                missing = []
                if not adb:
                    missing.append("adb")
                if not scrcpy["found"]:
                    missing.append("scrcpy")
                return {
                    "ok": False,
                    "error": f"缺少 {' / '.join(missing)}：请先安装，或在面板里指定可执行文件路径",
                    "tools": report["tools"],
                }

            serial = str(params.get("serial") or "").strip()
            if not serial:
                return {"ok": False, "error": "请先选择一台设备"}

            code, out, err = tools.run_tool(adb, ["devices", "-l"], timeout=15.0)
            devices = tools.parse_adb_devices(out + "\n" + err)
            device = next((d for d in devices if d["serial"] == serial), None)
            if device is None:
                return {"ok": False, "error": f"找不到设备 {serial}：请确认 USB 连接或无线调试已开启"}
            if not device["online"]:
                hint = {
                    "unauthorized": "手机还没授权 USB 调试：请在手机屏幕上点「允许」",
                    "offline": "设备离线：重新插拔 USB，或重连无线调试",
                }.get(device["state"], f"设备状态为 {device['state']}，暂时不可用")
                return {"ok": False, "error": hint, "device": device}

            try:
                args = build_scrcpy_args(
                    scrcpy["path"],
                    {**params, "serial": serial},
                    audio_supported=report["scrcpy_audio_supported"],
                )
            except ValueError as exc:
                return {"ok": False, "error": str(exc)}

            self._stderr.clear()
            self._last_error = None
            self._serial = serial
            self._model = device.get("model") or serial
            self._started_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self._stopping = False

            kwargs: dict = dict(
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            if sys.platform == "win32":
                kwargs["creationflags"] = (
                    getattr(subprocess, "CREATE_NO_WINDOW", 0)
                    | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                )
            else:
                # 独立会话组：sidecar 收到 SIGTERM 时不会把 scrcpy 一起带走，
                # 退出路径由 cleanup() 显式 terminate，避免留下孤儿窗口。
                kwargs["start_new_session"] = True

            logger.info("[Mirror] scrcpy %s", " ".join(args[1:]))
            try:
                proc = subprocess.Popen(args, **kwargs)
            except OSError as exc:
                self._reset_locked()
                return {"ok": False, "error": f"启动 scrcpy 失败：{exc}"}

            self._proc = proc
            reader = threading.Thread(
                target=self._drain_stderr, args=(proc,), daemon=True, name="mirror-stderr"
            )
            reader.start()

            # 等一小会儿：真出错（server 推不上去 / 设备掉线）这会儿已经退出了
            deadline = time.monotonic() + _START_GRACE_SECONDS
            while time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.05)
            if proc.poll() is not None:
                reader.join(timeout=1.0)
                tail = list(self._stderr)[-3:]
                self._reset_locked()
                message = tail[-1] if tail else f"scrcpy 启动后立即退出（退出码 {proc.returncode}）"
                self._last_error = message
                return {"ok": False, "error": message}

            self._monitor = threading.Thread(
                target=self._watch, args=(proc,), daemon=True, name="mirror-monitor"
            )
            self._monitor.start()
            state = self._state_locked()
            logger.info("[Mirror] 会话已启动: serial=%s model=%s pid=%s", serial, self._model, proc.pid)

        self._emit(state)
        return {"ok": True, **state}

    def stop(self) -> dict:
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                self._reset_locked()
                state = self._state_locked()
                return {"ok": True, "stopped": False, **state}

            self._stopping = True
            try:
                proc.terminate()
                proc.wait(timeout=_STOP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                logger.warning("[Mirror] scrcpy 未在 %.0fs 内退出，强制结束", _STOP_GRACE_SECONDS)
                proc.kill()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass
            except OSError as exc:
                logger.warning("[Mirror] 结束 scrcpy 失败: %s", exc)

            self._reset_locked()
            state = self._state_locked()
            logger.info("[Mirror] 会话已停止")

        self._emit(state)
        return {"ok": True, "stopped": True, **state}

    def cleanup(self) -> None:
        """退出路径：停掉会话，幂等。"""
        try:
            self.stop()
        except Exception:
            logger.exception("[Mirror] cleanup 失败")

    # ── 内部 ──

    def _reset_locked(self) -> None:
        self._proc = None
        self._monitor = None
        self._serial = None
        self._model = None
        self._started_at = None
        self._stopping = False

    def _state_locked(self) -> dict:
        alive = self._proc is not None and self._proc.poll() is None
        return {
            "running": alive,
            "serial": self._serial if alive else None,
            "model": self._model if alive else None,
            "started_at": self._started_at if alive else None,
            "pid": self._proc.pid if alive else None,
            "window": "external",
            "error": self._last_error,
        }

    def _drain_stderr(self, proc: subprocess.Popen) -> None:
        stream = proc.stderr
        if stream is None:
            return
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", errors="replace").strip()
            if line:
                self._stderr.append(line)
                logger.debug("[scrcpy] %s", line)
        try:
            stream.close()
        except OSError:
            pass

    def _watch(self, proc: subprocess.Popen) -> None:
        code = proc.wait()
        with self._lock:
            if self._proc is not proc:
                # stop() 已经收过尸，这里只可能是重复上报
                return
            tail = list(self._stderr)[-3:]
            intentional = self._stopping
            self._reset_locked()
            if not intentional and code != 0:
                self._last_error = (
                    tail[-1] if tail else f"scrcpy 已退出（退出码 {code}）"
                )
                logger.warning("[Mirror] scrcpy 异常退出: code=%s tail=%s", code, tail[-1:])
            else:
                logger.info("[Mirror] scrcpy 已退出: code=%s", code)
            state = self._state_locked()
        self._emit(state)

    def _emit(self, state: dict) -> None:
        try:
            protocol_io.emit_event("mirror_state_changed", state)
        except Exception:
            logger.exception("[Mirror] 事件推送失败")
