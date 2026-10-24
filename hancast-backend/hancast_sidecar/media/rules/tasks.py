"""规则包任务：单 worker、零队列；旧同步入口用 synchronous() 共享准入。

取消是协作式的：pending 可表示已请求取消但 worker 尚未退出。requests 的
socket timeout/分块检查不能中断 DNS 或正在执行的 QuickJS；这些等待结束前
不释放执行名额。commit 一旦赢得裁决，就必须报告真实提交结果，而非 cancelled。
"""

import base64
import copy
import logging
import math
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .jsplugins import PluginManager

logger = logging.getLogger("hancast.media.rules.tasks")


class TaskCancelled(RuntimeError):
    """取消已在提交之前胜出（不能被 PluginError 的回退逻辑捕获）。"""


class TaskDeadline(TimeoutError):
    """整个任务的 monotonic 预算已耗尽。"""


class TaskContext:
    def __init__(self, budget: float = 90.0):
        if not math.isfinite(budget) or budget <= 0:
            raise ValueError("任务预算必须是有限正数")
        self.deadline = time.monotonic() + budget
        self._lock = threading.Lock()
        self._cancelled = False
        self._phase = "open"  # open / committing / committed / failed / finished
        self._committed = False

    def _checkpoint_locked(self) -> None:
        if self._phase != "open":
            return
        if self._cancelled:
            raise TaskCancelled("规则包任务已取消")
        if time.monotonic() >= self.deadline:
            raise TaskDeadline("规则包任务超时（总预算已耗尽）")

    def checkpoint(self) -> None:
        with self._lock:
            self._checkpoint_locked()

    def timeout(self, requested: float) -> float:
        """限制本次 socket 等待；不是强制打断阻塞系统调用的总计时器。"""
        with self._lock:
            self._checkpoint_locked()
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TaskDeadline("规则包任务超时（总预算已耗尽）")
            return min(float(requested), remaining)

    def request_cancel(self) -> bool:
        with self._lock:
            if self._phase != "open":
                return False
            self._cancelled = True
            return True

    @contextmanager
    def commit(self):
        # 只锁裁决，不锁磁盘 IO：cancel/close 不会等待 rename 或 QuickJS。
        with self._lock:
            if self._phase != "open":
                raise RuntimeError("任务只能提交一次")
            self._checkpoint_locked()
            self._phase = "committing"
        try:
            yield
        except BaseException:
            with self._lock:
                if not self._committed:
                    self._phase = "failed"
            raise
        else:
            with self._lock:
                # 换代成功的那一刻起，取消就不再等于「撤销成功」
                self._committed = True
                self._phase = "committed"

    @property
    def committed(self) -> bool:
        with self._lock:
            return self._committed

    def _finish(self, value, error: Exception | None) -> dict:
        with self._lock:
            # 与最后一次 cancel 裁决互斥，避免 cancel 胜出后却回 done。
            if not self._committed:
                try:
                    self._checkpoint_locked()
                except (TaskCancelled, TaskDeadline) as exc:
                    error = exc
            self._phase = "finished"
        if error is not None and self._committed:
            # 包已经换完了：这时把取消/超时如实报成失败会谎称撤销成功
            error = None
        if isinstance(error, TaskCancelled):
            return {"status": "cancelled"}
        if error is not None:
            return {"status": "error", "error": str(error)}
        return {"status": "done", "result": value}


@dataclass
class _Task:
    context: TaskContext
    response: dict
    finished_at: float | None = None


class PluginTaskRunner:
    """每个 manager 集成一个 runner；所有旧同步写入口必须包 synchronous()。

    budget / close_timeout 可供测试注入；终态保留 600 秒，最多 16 条（不含
    pending）。未知/已淘汰 ID、忙、关闭后准入均抛 ValueError。
    """

    def __init__(self, manager: "PluginManager", *, budget: float = 90.0,
                 close_timeout: float = 0.5):
        if not math.isfinite(budget) or budget <= 0:
            raise ValueError("任务预算必须是有限正数")
        if not math.isfinite(close_timeout) or close_timeout < 0:
            raise ValueError("关闭等待必须是有限非负数")
        self.manager = manager
        self.budget = budget
        self.close_timeout = close_timeout
        self._lock = threading.Lock()
        self._tasks: dict[str, _Task] = {}
        self._worker: threading.Thread | None = None
        self._active: _Task | None = None
        self._sync_busy = False
        self._closed = False

    def _admit_locked(self) -> None:
        if self._closed:
            raise ValueError("规则包任务服务已关闭")
        if self._sync_busy or (self._worker and self._worker.is_alive()):
            raise ValueError("规则包操作忙，请稍后重试")

    def _prune_locked(self) -> None:
        now = time.monotonic()
        terminal = sorted((entry.finished_at, task_id)
                          for task_id, entry in self._tasks.items()
                          if entry.finished_at is not None)
        for finished_at, task_id in terminal:
            if now - finished_at >= 600 or len(terminal) > 16:
                del self._tasks[task_id]
                # Count only the remaining terminal records.
                terminal = terminal[1:]
            else:
                break

    def start(self, operation: str, params: dict) -> dict:
        if operation not in ("plugin_check_updates", "plugin_pull", "plugin_import"):
            raise ValueError(f"不支持的规则包任务: {operation}")
        if not isinstance(params, dict):
            raise ValueError("任务 params 必须是对象")
        params = copy.deepcopy(params)
        with self._lock:
            self._admit_locked()
            self._prune_locked()
            task_id = uuid.uuid4().hex
            entry = _Task(TaskContext(self.budget), {"status": "pending"})
            self._tasks[task_id] = entry
            self._active = entry
            worker = threading.Thread(target=self._run,
                                      args=(entry, operation, params),
                                      name=f"PLUGIN_{task_id[:12]}", daemon=True)
            self._worker = worker
            try:
                worker.start()
            except Exception:
                del self._tasks[task_id]
                self._active = None
                self._worker = None
                raise
        return {"task_id": task_id, "status": "pending"}

    def _execute(self, operation: str, params: dict, task: TaskContext):
        mgr = self.manager
        task.checkpoint()
        if operation == "plugin_check_updates":
            mgr.refresh()
            task.checkpoint()
            return mgr.check_updates(str(params.get("index_url") or ""), task=task)
        if operation == "plugin_pull":
            name = str(params.get("name") or "")
            if not name:
                raise ValueError("缺少包名")
            mgr.pull(name, str(params.get("url") or ""), task=task)
        elif params.get("path"):
            # 和旧命令同样的来源优先级，但不先把任意大小本地文件读进内存。
            from .jsplugins import _MAX_ZIP_BYTES
            with open(params["path"], "rb") as source:
                data = source.read(_MAX_ZIP_BYTES + 1)
            task.checkpoint()
            mgr.import_zip_bytes(data, task=task)
        elif params.get("data_base64"):
            from .jsplugins import _MAX_ZIP_BYTES, PluginError
            encoded = params["data_base64"]
            if len(encoded) > ((_MAX_ZIP_BYTES + 2) // 3) * 4:
                raise PluginError("插件包超过 2MB 上限")
            data = base64.b64decode(encoded)
            task.checkpoint()
            mgr.import_zip_bytes(data, task=task)
        elif params.get("url"):
            mgr.import_url(params["url"], task=task)
        else:
            raise ValueError("需要 path / data_base64 / url 之一")
        return {"plugins": mgr.list_info()}

    def _run(self, entry: _Task, operation: str, params: dict) -> None:
        value, error = None, None
        try:
            value = self._execute(operation, params, entry.context)
        except Exception as exc:
            error = exc
        if entry.context.committed and value is None:
            # 换代已完成、只是收尾被取消：回一份真实清单，别给前端 null
            try:
                value = {"plugins": self.manager.list_info()}
            except Exception:
                logger.warning("规则包任务收尾取清单失败", exc_info=True)
                value = {"plugins": []}
        response = entry.context._finish(value, error)
        with self._lock:
            entry.response = response
            entry.finished_at = time.monotonic()
            self._prune_locked()
        # 不在这里释放名额：start 以 Thread.is_alive() 为准，直到真正退出。

    def _entry_locked(self, task_id: str) -> _Task:
        self._prune_locked()
        entry = self._tasks.get(task_id)
        if entry is None:
            raise ValueError("规则包任务不存在或已过期")
        return entry

    def result(self, task_id: str) -> dict:
        with self._lock:
            return copy.deepcopy(self._entry_locked(task_id).response)

    def cancel(self, task_id: str) -> dict:
        with self._lock:
            entry = self._entry_locked(task_id)
            if entry.response["status"] == "pending":
                entry.context.request_cancel()
            return copy.deepcopy(entry.response)

    @contextmanager
    def synchronous(self):
        with self._lock:
            self._admit_locked()
            self._sync_busy = True
        try:
            yield
        finally:
            with self._lock:
                self._sync_busy = False

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._active is not None:
                self._active.context.request_cancel()
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(self.close_timeout)
