"""
Linux（PipeWire / PulseAudio）音频输入枚举

系统声音为什么能拿到：PipeWire 给每个输出节点（sink）自带一条 `<sink>.monitor`
源，抓它就等于抓「此刻真正出声的那条总线」——和 Windows 的 WASAPI 环回、macOS
的 Audio Tap 是同一件事，但不需要装任何虚拟声卡。

数据源与优先级（都是本机 CLI，不引新依赖）：
    1. `pactl --format=json list sources`  —— 权威的设备清单（PA 与 PW 的 pulse
       兼容层都吃这套）；PipeWire 上 description 一律是 "(null)"，所以名字得自己拼。
    2. `pw-dump` —— 拿 `node.description` 这种人能看懂的名字（"Ryzen HD Audio
       Controller 模拟立体声"），monitor 用对应 sink 的描述加「系统声音」前缀。
    3. `pactl info` —— 默认 source/sink，用来在列表里标出「（默认）」：只有默认
       输出的 monitor 才真的在出声，选错闲置的那块照样是「投屏正常但没声音」。

⚠️ pactl 在 PipeWire 上会往 stderr 打 `Invalid ASCII character: ...`（它自己
的 JSON 转义不认多字节），这是噪音不是失败——解析只吃 stdout，别把两路并一起。
"""

import json
import logging
import shutil
import subprocess
import sys
from typing import Dict, List, Optional

logger = logging.getLogger("hancast.screencast.pulse")

_TIMEOUT = 6


def _pulse_session() -> bool:
    return sys.platform == "linux" and bool(shutil.which("pactl"))


def _json_cmd(argv: List[str]) -> Optional[object]:
    """跑一条命令拿 JSON（stderr 单独留着，只用于日志）"""
    try:
        r = subprocess.run(argv, capture_output=True, timeout=_TIMEOUT,
                           text=True, encoding="utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001 - 探测失败不能拖垮枚举
        logger.info(f"[ScreenCast] {' '.join(argv[:2])} 失败: {type(e).__name__}: {e}")
        return None
    if r.returncode != 0:
        logger.info(f"[ScreenCast] {' '.join(argv[:2])} 退出码 {r.returncode}: "
                    f"{(r.stderr or '').strip()[:160]}")
        return None
    try:
        return json.loads(r.stdout or "")
    except Exception as e:  # noqa: BLE001
        logger.info(f"[ScreenCast] {' '.join(argv[:2])} 输出不是合法 JSON: {e}")
        return None


def _defaults() -> Dict[str, str]:
    """pactl info 里的 Default Source / Default Sink"""
    out: Dict[str, str] = {}
    try:
        r = subprocess.run(["pactl", "info"], capture_output=True,
                           timeout=_TIMEOUT, text=True,
                           encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return out
    for line in (r.stdout or "").splitlines():
        key, _, val = line.partition(":")
        key = key.strip().lower()
        if key == "default source":
            out["source"] = val.strip()
        elif key == "default sink":
            out["sink"] = val.strip()
    return out


def _pretty_names() -> Dict[str, str]:
    """node.name → node.description（PipeWire 才有的可读名字）

    monitor 源在 pw-dump 里不是独立节点，所以这里同时把 sink 的描述按
    `<sink>.monitor` 这个键位补进去。
    """
    names: Dict[str, str] = {}
    if not shutil.which("pw-dump"):
        return names
    data = _json_cmd(["pw-dump"])
    if not isinstance(data, list):
        return names
    for obj in data:
        props = (obj.get("info") or {}).get("props") or {}
        cls = str(props.get("media.class") or "")
        name = props.get("node.name")
        desc = props.get("node.description") or props.get("node.nick")
        if not name or not desc:
            continue
        if cls == "Audio/Sink":
            names[str(name)] = str(desc)
            names[f"{name}.monitor"] = str(desc)
        elif cls.startswith("Audio/Source"):
            names.setdefault(str(name), str(desc))
    return names


def _strip_alsa(name: str) -> str:
    """alsa_output.pci-0000_0d_00.6.analog-stereo → pci-0000_0d_00.6 / analog-stereo"""
    parts = str(name).split(".")
    if parts and parts[0] in ("alsa_output", "alsa_input"):
        parts = parts[1:]
    if parts and parts[-1] == "monitor":
        parts = parts[:-1]
    return " · ".join(p for p in parts if p)


def list_input_devices() -> List[Dict]:
    """返回 [{"name", "kind", "title", "default"}]

    kind: mic = 物理输入；system = 某条输出的 monitor（抓它等于抓系统声音）。
    name 是 ffmpeg `-f pulse -i <name>` 直接吃的目标串，title 是给人看的名字，
    default 标记「此刻真正在出声/在收音」的那一条——闲置输出的 monitor 采得到
    数据但永远是静音，不标出来用户就只能自己试。
    """
    if not _pulse_session():
        return []
    sources = _json_cmd(["pactl", "--format=json", "list", "sources"])
    if not isinstance(sources, list):
        return []
    pretty = _pretty_names()
    defaults = _defaults()
    out: List[Dict] = []
    for s in sources:
        name = str(s.get("name") or "").strip()
        if not name:
            continue
        # PipeWire 在 pactl JSON 里把 description 写成字符串 "(null)"
        raw_desc = str(s.get("description") or "").strip()
        desc = "" if raw_desc in ("", "(null)") else raw_desc
        is_monitor = name.endswith(".monitor")
        label = pretty.get(name) or desc or _strip_alsa(name)
        entry = {
            "name": name,
            "kind": "system" if is_monitor else "mic",
            "title": label,
            "default": name == defaults.get("source") if not is_monitor
            else name == f"{defaults.get('sink')}.monitor",
        }
        if is_monitor:
            entry["monitor"] = True
        out.append(entry)
    # 默认那条排前面：列表长的时候用户第一眼看的就是它
    out.sort(key=lambda d: (not d["default"], d["kind"] != "system"))
    return out
