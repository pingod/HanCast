"""
日志管理

提供统一的日志初始化:
- setup_logger()    初始化根 'hancast' logger，所有子模块 logger 自动继承
- get_logger(name)  获取子 logger（无需重复配置 handler）
"""

import os
import re
import sys
import logging
from logging.handlers import RotatingFileHandler
from typing import Optional

from ..params import get as param

# 默认日志目录
if sys.platform == 'win32':
    LOG_DIR = os.path.join(os.environ.get('APPDATA', ''), 'HanCast', 'logs')
elif sys.platform == 'darwin':
    LOG_DIR = os.path.join(os.path.expanduser('~'), 'Library', 'Application Support', 'HanCast', 'logs')
else:
    LOG_DIR = os.path.join(os.path.expanduser('~'), '.config', 'hancast', 'logs')

LOG_FILE = os.path.join(LOG_DIR, 'hancast.log')

_initialized = False


def setup_logger(
    name: str = "hancast",
    level: int = logging.INFO,
    log_file: Optional[str] = None,
    max_bytes: int = 5 * 1024 * 1024,  # 5MB per file
    backup_count: int = 3,              # 保留 3 个备份
) -> logging.Logger:
    """
    初始化根 logger，配置控制台 + 文件输出。

    只需调用一次；后续调用会跳过重复初始化。
    所有子 logger（如 hancast.mpv, hancast.ssdp）自动继承 handler。

    Args:
        name:       根 logger 名称（默认 'hancast'）
        level:      日志级别（默认 INFO）
        log_file:   日志文件路径（默认 APPDATA/HanCast/logs/hancast.log）
        max_bytes:  单个日志文件最大字节数（默认 5MB）
        backup_count: 保留的旧日志文件数量（默认 3）
    """
    global _initialized
    if _initialized:
        return logging.getLogger(name)

    log_file = log_file or LOG_FILE
    logger = logging.getLogger(name)
    logger.setLevel(level)
    # 防止日志向上冒泡到 root logger（避免重复输出）
    logger.propagate = False

    formatter = logging.Formatter(
        '[%(asctime)s] %(name)s %(levelname)s: %(message)s'
    )

    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stderr)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 文件处理器（RotatingFileHandler，防止日志文件无限增长）
    try:
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding='utf-8',
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        logger.info(f"Log file: {os.path.abspath(log_file)}")
    except Exception as e:
        logger.warning(f"Failed to create log file handler: {e}")

    _initialized = True
    return logger


def get_logger(name: str) -> logging.Logger:
    """
    获取子 logger。子 logger 自动继承根 logger 的 handler。

    用法:
        from hancast_sidecar.utils.logger import get_logger
        logger = get_logger("hancast.mpv")
    """
    return logging.getLogger(name)


# ── 应用内日志查看（排错用） ──

# 一次最多读取的日志字节数：只看文件尾部，避免大文件把命令循环拖死
# 大小在设置页调（params: logs.tail_mb）
def _tail_bytes() -> int:
    return int(float(param("logs.tail_mb")) * 1024 * 1024)

_LINE_RE = re.compile(
    r"^\[(?P<ts>[^\]]+)\]\s+(?P<module>[\w.]+)\s+(?P<level>[A-Z]+):\s?(?P<msg>.*)$"
)

# 已知模块前缀 → 界面展示名（顺序即下拉框；过滤按前缀匹配）
MODULE_LABELS = [
    ("hancast.ssdp", "设备发现 (SSDP/UPnP)"),
    ("hancast.dlna", "DLNA 服务"),
    ("hancast.commands", "命令处理"),
    ("hancast.session", "投屏会话"),
    ("hancast.renderer", "本机渲染"),
    ("hancast.mpv", "播放器 (MPV)"),
    ("hancast.media", "媒体与链接解析"),
    ("hancast.guard", "投屏确认"),
    ("hancast.screencast", "屏幕投屏"),
    ("hancast.config", "配置"),
    ("hancast.params", "参数配置"),
]


def set_level(level: str):
    """运行时调整日志级别（排错时打开 DEBUG）"""
    lvl = getattr(logging, str(level).upper(), None)
    if not isinstance(lvl, int):
        raise ValueError(f"未知日志级别: {level}")
    root = logging.getLogger("hancast")
    root.setLevel(lvl)
    for h in root.handlers:
        h.setLevel(lvl)
    root.info("Log level set to %s", logging.getLevelName(lvl))
    return logging.getLevelName(lvl)


def get_level() -> str:
    return logging.getLevelName(logging.getLogger("hancast").level)


def _candidate_files():
    """当前日志 + 轮转备份（按新到旧）"""
    files = [LOG_FILE]
    for i in range(1, 4):
        bak = f"{LOG_FILE}.{i}"
        if os.path.isfile(bak):
            files.append(bak)
    return files


def read_logs(limit: int = 400, module: Optional[str] = None,
              level: Optional[str] = None, keyword: Optional[str] = None,
              include_older: bool = False) -> dict:
    """读取日志尾部并按模块/级别/关键字过滤

    module 用前缀匹配：选 "hancast.screencast" 时 manager/httpd/capture 一并命中。
    """
    limit = max(1, min(int(limit or 400), 5000))
    files = _candidate_files() if include_older else [LOG_FILE]
    rows = []
    matched_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
    want_level = level.upper() if level and level.upper() in matched_levels else None
    tail = _tail_bytes()

    for path in reversed(files):          # 旧 → 新，保证最终按时间升序
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size == 0:
            continue
        with open(path, "rb") as f:
            f.seek(max(0, size - tail))
            chunk = f.read()
        text = chunk.decode("utf-8", "replace")
        # 尾部seek可能落在半行上，丢掉第一行
        lines = text.splitlines()
        if size > tail and len(lines) > 1:
            lines = lines[1:]
        for line in lines:
            m = _LINE_RE.match(line)
            if not m:
                # 多行消息（堆栈）挂到上一条
                if rows and m is None and line.strip():
                    rows[-1]["msg"] += "\n" + line
                    rows[-1]["raw"] += "\n" + line
                continue
            mod, lvl = m.group("module"), m.group("level")
            if module and not mod.startswith(module):
                continue
            if want_level and lvl != want_level:
                continue
            if keyword and keyword.lower() not in line.lower():
                continue
            rows.append({
                "ts": m.group("ts"),
                "module": mod,
                "level": lvl,
                "msg": m.group("msg"),
                "raw": line,
            })

    truncated = len(rows) > limit
    return {
        "lines": rows[-limit:],
        "total": len(rows),
        "truncated": truncated,
        "path": LOG_FILE,
        "level": get_level(),
        "modules": available_modules(),
    }


def available_modules() -> list:
    """已知模块 + 日志里实际出现过的模块（去重，带展示名）"""
    out = [{"id": m, "label": label} for m, label in MODULE_LABELS]
    known = {m for m, _ in MODULE_LABELS}
    try:
        size = os.path.getsize(LOG_FILE)
    except OSError:
        return out
    if size:
        with open(LOG_FILE, "rb") as f:
            f.seek(max(0, size - _tail_bytes()))
            text = f.read().decode("utf-8", "replace")
        for line in text.splitlines():
            m = _LINE_RE.match(line)
            if not m:
                continue
            mod = m.group("module")
            if mod in known:
                continue
            known.add(mod)
            out.append({"id": mod, "label": mod})
    return out

