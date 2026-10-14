"""解析失败台账：把「粘了但解不出来」的地址按域名攒下来

要给别人写规则包，先得知道哪些站在失败、失败卡在哪一步。这个台账就是那份清单：
按域名分组，每条记住最后一次的失败原因和累计次数，设置页能看能删。

只写本机配置目录，不外发、不进日志——里面是用户粘贴过的链接。
"""

import json
import logging
import os
import threading
import time
from urllib.parse import urlparse

from ..params import get as _param
from ..utils.config import SETTING_DIR

logger = logging.getLogger("hancast.failures")

FILE_NAME = "parse_failures.json"
_MAX_URL_LEN = 500
_MAX_ERROR_LEN = 300
_MAX_HOSTS = 200          # 域名个数兜底：再宽就把用户配置目录撑大了

# delete(url) 会转调 forget()，两处都要上锁 → 用可重入锁
_lock = threading.RLock()


def path() -> str:
    return os.path.join(SETTING_DIR, FILE_NAME)


def enabled() -> bool:
    try:
        return bool(_param("failures.enabled"))
    except KeyError:
        return True


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except ValueError:
        return ""


def _load() -> dict:
    try:
        with open(path(), "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    hosts = raw.get("hosts") if isinstance(raw, dict) else None
    return hosts if isinstance(hosts, dict) else {}


def _save(hosts: dict) -> None:
    doc = {"hosts": hosts, "updated_at": int(time.time())}
    try:
        os.makedirs(SETTING_DIR, exist_ok=True)
        tmp = path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False)
        os.replace(tmp, path())
    except OSError as e:
        logger.warning("解析失败台账写不下去（不影响解析）: %s", e)


def _max_per_host() -> int:
    try:
        return max(1, int(_param("failures.max_per_host")))
    except (KeyError, TypeError, ValueError):
        return 50


def _retention_seconds() -> float:
    try:
        return max(1, int(_param("failures.retention_days"))) * 86400.0
    except (KeyError, TypeError, ValueError):
        return 30 * 86400.0


def _prune(hosts: dict, now: float, budget: float) -> dict:
    """丢掉超过保留期的地址；整个域名空了就连域名一起丢"""
    out: dict = {}
    for host, bucket in hosts.items():
        if not isinstance(bucket, dict):
            continue
        keep = [e for e in (bucket.get("entries") or [])
                if isinstance(e, dict) and now - float(e.get("last_at") or 0) <= budget]
        if not keep:
            continue
        keep = keep[-_max_per_host():]
        out[host] = {"first_at": min(float(e.get("first_at") or e.get("last_at") or 0)
                                     for e in keep),
                     "last_at": max(float(e.get("last_at") or 0) for e in keep),
                     "count": sum(int(e.get("count") or 1) for e in keep),
                     "entries": keep}
    return out


def record(url: str, error: object) -> None:
    """记一条失败：同一地址只留最后一次原因，但累计次数在涨"""
    if not enabled():
        return
    host = _host_of(url)
    if not host:
        return
    now = time.time()
    text = str(error or "").strip()[:_MAX_ERROR_LEN]
    key = str(url)[:_MAX_URL_LEN]
    with _lock:
        hosts = _prune(_load(), now, _retention_seconds())
        bucket = hosts.get(host) or {"first_at": now, "last_at": now,
                                     "count": 0, "entries": []}
        entries = bucket.get("entries") or []
        hit = next((e for e in entries if str(e.get("url", "")).lower() == key.lower()),
                   None)
        if hit:
            hit["count"] = int(hit.get("count") or 1) + 1
            hit["last_at"] = now
            if text:
                hit["error"] = text
        else:
            entries.append({"url": key, "error": text,
                            "first_at": now, "last_at": now, "count": 1})
        bucket["entries"] = entries[-_max_per_host():]
        bucket["last_at"] = now
        bucket["first_at"] = min(float(bucket.get("first_at") or now), now)
        bucket["count"] = sum(int(e.get("count") or 1) for e in bucket["entries"])
        hosts[host] = bucket
        # 域名太多时淘汰最久没失败的
        if len(hosts) > _MAX_HOSTS:
            for stale in sorted(hosts, key=lambda h: float(
                    hosts[h].get("last_at") or 0))[:len(hosts) - _MAX_HOSTS]:
                hosts.pop(stale, None)
        _save(hosts)


def forget(url: str) -> int:
    """解析成功时把它从台账里摘掉：这条已经不需要人写规则了"""
    host = _host_of(url)
    key = str(url).lower()
    if not host:
        return 0
    with _lock:
        hosts = _load()
        bucket = hosts.get(host)
        if not isinstance(bucket, dict):
            return 0
        entries = bucket.get("entries") or []
        kept = [e for e in entries if str(e.get("url", "")).lower() != key]
        removed = len(entries) - len(kept)
        if not removed:
            return 0
        if kept:
            bucket["entries"] = kept
            bucket["count"] = sum(int(e.get("count") or 1) for e in kept)
            hosts[host] = bucket
        else:
            hosts.pop(host, None)
        _save(hosts)
        return removed


def snapshot() -> dict:
    """给设置页看的台账：按最近失败排序"""
    now = time.time()
    with _lock:
        hosts = _prune(_load(), now, _retention_seconds())
        if hosts:
            _save(hosts)          # 顺手把过期的清掉，别让文件只涨不消
    items = []
    for host, bucket in hosts.items():
        entries = sorted(bucket.get("entries") or [],
                         key=lambda e: -float(e.get("last_at") or 0))
        items.append({
            "host": host,
            "count": int(bucket.get("count") or len(entries)),
            "first_at": float(bucket.get("first_at") or 0),
            "last_at": float(bucket.get("last_at") or 0),
            "entries": [{"url": e.get("url", ""), "error": e.get("error", ""),
                         "count": int(e.get("count") or 1),
                         "last_at": float(e.get("last_at") or 0)}
                        for e in entries],
        })
    items.sort(key=lambda i: -i["last_at"])
    return {"enabled": enabled(), "path": path(), "hosts": items,
            "total": sum(i["count"] for i in items)}


def delete(host: str = "", url: str = "") -> int:
    """删一个域名（含它全部地址）或其中一条地址"""
    with _lock:
        hosts = _load()
        if url:
            return forget(url)
        if not host:
            return 0
        removed = hosts.pop(str(host).lower(), None)
        if removed is None:
            return 0
        _save(hosts)
        return len((removed or {}).get("entries") or [])


def clear() -> int:
    with _lock:
        hosts = _load()
        total = sum(len(b.get("entries") or []) for b in hosts.values()
                    if isinstance(b, dict))
        try:
            os.remove(path())
        except OSError:
            pass
        return total
