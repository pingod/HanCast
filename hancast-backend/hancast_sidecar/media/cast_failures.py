"""投屏失败台账：解出来了、但没投出去，断在哪一步留一份能回看的清单

解析失败台账（`media/failures.py`）管「粘了但解不出来」；这份管「解出来了、也发出去了，
但电视没动静」。COMP-005（无后缀直链电视不拉流）之所以查了大半天，就是因为出站整条
链路只有 DEBUG 级日志，面板默认 INFO 一个字都看不到——投屏失败（出站）过去无处留痕。

每条记：设备、阶段、错误原文、目标地址、时间。阶段取值见 `STAGES`。
只写本机配置目录，不外发、不进日志（里面是用户投过的地址）。
"""

import json
import logging
import os
import threading
import time

from ..utils.config import SETTING_DIR

logger = logging.getLogger("hancast.cast_failures")

FILE_NAME = "cast_failures.json"

STAGES = {
    "connect": "控制地址不可达",
    "set_uri": "SetAVTransportURI 被拒",
    "play": "Play 被拒",
    "stop": "Stop 未送达",
    "media_ready": "媒体加载超时（电视没开始缓冲）",
    "cast": "其他投屏异常",
}

_MAX_ENTRIES = 100
_MAX_ERROR_LEN = 400
_MAX_URL_LEN = 500
_RETENTION_SECONDS = 14 * 86400.0

_lock = threading.RLock()


def path() -> str:
    return os.path.join(SETTING_DIR, FILE_NAME)


def _load() -> list:
    try:
        with open(path(), "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return []
    entries = raw.get("entries") if isinstance(raw, dict) else None
    return entries if isinstance(entries, list) else []


def _save(entries: list) -> None:
    doc = {"entries": entries, "updated_at": int(time.time())}
    try:
        os.makedirs(SETTING_DIR, exist_ok=True)
        tmp = path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
        os.replace(tmp, path())
    except OSError as e:
        logger.warning("投屏失败台账写不下去（不影响投屏）: %s", e)


def _prune(entries: list, now: float) -> list:
    keep = [e for e in entries
            if isinstance(e, dict) and now - float(e.get("at") or 0) <= _RETENTION_SECONDS]
    return keep[-_MAX_ENTRIES:]


def record(device_id: str = "", device_name: str = "", stage: str = "cast",
           error: object = "", url: str = "") -> None:
    """记一条投屏失败。stage 见 STAGES；同设备同阶段同错误在 5 秒内合并计数，避免刷屏"""
    now = time.time()
    text = str(error or "").strip()[:_MAX_ERROR_LEN]
    did = str(device_id or "")
    key_url = str(url or "")[:_MAX_URL_LEN]
    with _lock:
        entries = _prune(_load(), now)
        if entries:
            last = entries[-1]
            if (last.get("device_id") == did and last.get("stage") == stage
                    and last.get("error") == text and now - float(last.get("at") or 0) < 5):
                last["count"] = int(last.get("count") or 1) + 1
                last["at"] = now
                _save(entries)
                return
        entries.append({
            "at": now,
            "device_id": did,
            "device_name": str(device_name or ""),
            "stage": stage,
            "error": text,
            "url": key_url,
            "count": 1,
        })
        _save(entries[-_MAX_ENTRIES:])


def snapshot() -> dict:
    """给界面看的台账：按时间倒序（最近的在最前）"""
    now = time.time()
    with _lock:
        entries = _prune(_load(), now)
        if entries:
            _save(entries)          # 顺手把过期的清掉，别让文件只涨不消
    items = []
    for e in reversed(entries):
        items.append({
            "at": float(e.get("at") or 0),
            "device_id": str(e.get("device_id") or ""),
            "device_name": str(e.get("device_name") or ""),
            "stage": str(e.get("stage") or ""),
            "stage_label": STAGES.get(str(e.get("stage") or ""), ""),
            "error": str(e.get("error") or ""),
            "url": str(e.get("url") or ""),
            "count": int(e.get("count") or 1),
        })
    return {"path": path(), "entries": items,
            "total": sum(i["count"] for i in items)}


def clear() -> int:
    with _lock:
        entries = _load()
        total = sum(int(e.get("count") or 1) for e in entries if isinstance(e, dict))
        try:
            os.remove(path())
        except OSError:
            pass
        return total
