"""把抖音私信列表里的会话（好友）全捞出来，给服务端配置页勾选用。

为什么做成「后台线程 + 状态机」，而不是让 HTTP 请求直接等：

    滚动读一遍会话列表要占着浏览器几秒到十几秒（见 `bridge/reader.py::read_all_conversations`）。
    让请求挂在那儿等，页面只能干转圈，还容易撞上 HTTP 超时；而配置页本来就是
    2 秒轮询一次状态的。所以这里跟 `credentials.AccountStore` 一个套路：起个线程跑，
    进度留在对象里，页面轮询着看。

「取消」是**合作式**的：只置一个标志，滚动循环每轮检查一次（`should_stop`）。
    所以点完「取消」要等当前那一轮跑完才停 —— 这是刻意的：中途硬杀线程会把
    Playwright 的异步调用留在半路，比多等半秒糟糕得多。

浏览器是**复用正在跑的那个**（桥接进程里已经开着的抖音窗口），不会另起一个：
    同一账号再开一个浏览器既费资源又容易触发风控。代价是这个功能只在服务
    跑起来之后可用 —— 首次启动向导阶段读到的是「还没接上抖音会话」。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from app.names import normalize_name

LOGGER = logging.getLogger("douyin_watch")

STATE_IDLE = "idle"
STATE_SCANNING = "scanning"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"

# 手表上真列 200 个人也没法用，这个上限只是防着滚动停不下来。
MAX_ITEMS = 200

# reader 的签名：给一个「要不要停」的回调，返回 [{"name", "preview"}, …]
ReaderFn = Callable[[Callable[[], bool]], list[dict[str, Any]]]


class FriendScanError(RuntimeError):
    """页面上说人话的错误。"""


def _stamp(ts: float | None) -> str | None:
    return time.strftime("%H:%M:%S", time.localtime(ts)) if ts else None


def _friendly(exc: BaseException) -> str:
    text = str(exc).strip()
    if isinstance(exc, TimeoutError):
        return "等待抖音响应超时 —— 该浏览器窗口可能已卡住，或正被其他操作占用。请稍后重试。"
    if text:
        return text
    return f"{type(exc).__name__}（没给出原因）"


def _clean(raw: Any, max_items: int) -> list[dict[str, Any]]:
    """只留名字非空、按名字去重、保序（最近聊过的在前面）。

    名字走 `app.names.normalize_name`（不是 `.strip()`）—— 会话行里昵称和时间
    挤在同一段文本里（`innerText` 会插换行），光去首尾会把时间一起带进名单。
    """
    if not isinstance(raw, list):
        return []
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in raw:
        if isinstance(entry, str):
            name, preview = normalize_name(entry), ""
        elif isinstance(entry, dict):
            name = normalize_name(entry.get("name"))
            preview = str(entry.get("preview") or "").strip()
        else:
            continue
        if not name or name in seen:
            continue
        seen.add(name)
        items.append({"name": name, "preview": preview})
        if len(items) >= max_items:
            break
    return items


class FriendScanner:
    """一个会花几秒的浏览器动作：把会话列表滚到底，收集名字。

    `reader` 为 None 表示这个服务没接上抖音会话（比如向导阶段、或 --no-setup-web
    之外的独立测试）—— 那时页面会显示「读不了」而不是给一个点了没反应的按钮。
    """

    def __init__(self, reader: ReaderFn | None, *, max_items: int = MAX_ITEMS) -> None:
        self._reader = reader
        self._max_items = max_items
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._state = STATE_IDLE
        self._items: list[dict[str, Any]] = []
        self._message = ""
        self._started: float | None = None
        self._finished: float | None = None

    # ------------------------------------------------------------------ 读

    def state(self) -> dict[str, Any]:
        with self._lock:
            items = [dict(item) for item in self._items]
            state = self._state
            message = self._message
            started, finished = self._started, self._finished
            stop = self._stop.is_set()
        running = state == STATE_SCANNING
        return {
            "state": state,
            "running": running,
            "cancelling": running and stop,
            "available": self._reader is not None,
            "items": items,
            "count": len(items),
            "message": message,
            "scanned_at": _stamp(finished),
            "elapsed": round(finished - started, 1) if (started and finished) else None,
        }

    # ------------------------------------------------------------------ 动

    def start(self) -> dict[str, Any]:
        """起一次读取。已经在读就报错，不会叠第二次。"""
        with self._lock:
            if self._state == STATE_SCANNING:
                raise FriendScanError("上一次读取尚未结束 —— 请等待其完成，或点击「取消」。")
            if self._reader is None:
                raise FriendScanError(
                    "本服务尚未接入抖音会话，无法读取好友列表。"
                    "请等待服务启动完成（浏览器窗口出现）后重试。"
                )
            self._stop = threading.Event()
            self._state = STATE_SCANNING
            self._message = "正在滚动抖音私信列表…"
            self._started = time.time()
            self._finished = None
            thread = threading.Thread(target=self._run, name="friend-scan", daemon=True)
        thread.start()
        LOGGER.info("开始读取抖音会话列表")
        return self.state()

    def cancel(self) -> dict[str, Any]:
        """请求停止。合作式的 —— 当前那一轮滚动会跑完才停。"""
        with self._lock:
            if self._state == STATE_SCANNING:
                self._stop.set()
                self._message = "正在取消（等待本轮滚动结束）…"
                LOGGER.info("用户取消了会话列表读取")
        return self.state()

    # ------------------------------------------------------------------ 内部

    def _run(self) -> None:
        reader = self._reader
        assert reader is not None
        try:
            raw = reader(self._stop.is_set)
            items = _clean(raw, self._max_items)
        except Exception as exc:  # noqa: BLE001 - 任何失败都变成页面上一句话
            cancelled = self._stop.is_set()
            LOGGER.log(logging.DEBUG if cancelled else logging.WARNING,
                       "读取抖音会话列表失败：%s", exc, exc_info=not cancelled)
            with self._lock:
                self._finished = time.time()
                self._state = STATE_CANCELLED if cancelled else STATE_FAILED
                self._message = "已取消，名单未发生变化。" if cancelled else _friendly(exc)
            return
        with self._lock:
            self._finished = time.time()
            if self._stop.is_set():
                self._state = STATE_CANCELLED
                self._message = "已取消，名单未发生变化。"
                return
            self._items = items
            self._state = STATE_DONE
            self._message = (
                f"读取到 {len(items)} 个会话。"
                if items
                else "未读取到任何会话 —— 抖音窗口中的私信列表为空？"
            )
        LOGGER.info("读取抖音会话列表完成：%d 个", len(items))
