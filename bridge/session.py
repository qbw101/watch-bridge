"""常驻抖音会话：一个可见浏览器窗口 + 串行化的读写命令。

设计要点：
- 整个进程只开一个 Chromium（可见模式，符合项目稳定运行约定），
  通过 asyncio.Lock 串行执行所有页面操作，避免并发导致页面状态错乱。
- 复用 app/ 里已验证的逻辑：open_douyin / verify_login / DouyinChat / send_text。
- 用 artifacts/run.lock 串行化：同一时刻只允许一个服务持有浏览器会话。

性能优化（2026-09-18，针对手表端「打开慢、消息有延迟」）：
- 切换会话优先点左侧会话列表（bridge/fastopen.py），失败才回退抖音搜索；
- 切换后不再固定等 2 秒，改为轮询等消息列表挂载稳定；
- 表情/图片二进制做内存 LRU 缓存并计算 ETag，手表重复加载几乎零成本；
- 发送接口直接回传最新消息列表，手表端省掉一次完整往返；
- 每个耗时操作都写日志，方便定位卡顿。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from app.browser import AuthenticationError, RiskControlError, open_douyin, open_private_messages, verify_login
from app.config import load_settings
from app.douyin import DouyinChat, PageOperationError, RefreshYielded, StickerOutOfSyncError
from app.lockfile import AlreadyRunningError, run_lock
from app.models import Settings, Sticker
from app.sender import format_send_profile, reset_send_profile, send_douyin_sticker, send_step, send_text

from bridge import sticker_thumbs, watch_friends
from bridge.fastopen import open_via_conversation_list
from bridge.reader import read_all_conversations, read_conversations, read_messages, wait_for_message_list
from bridge.sticker_store import LIBRARY_FILENAME, LibraryItem, StickerLibrary

LOGGER = logging.getLogger("douyin_watch")

MAX_TEXT_LENGTH = 500
DEFAULT_READ_LIMIT = 30

# status 里 `presets` 最多给几个 —— 手表头一次连上时拿它给「快捷短语」播种。
# 给太多等于替用户塞满一屏，他得先删一轮才能用。
PRESET_LIMIT = 12

# 表情/图片代理只允许抖音自有 CDN，防止把服务变成任意 URL 的开放代理（SSRF）。
ALLOWED_MEDIA_HOSTS = (
    ".douyin.com",
    ".douyinpic.com",
    ".douyinstatic.com",
    ".byteimg.com",
    ".zjcdn.com",
    ".ibytedtos.com",
    ".ixigua.com",
    ".bytedance.com",
)

# 表情包很小（实测 7-27KB/张），整段会话的图片加起来通常不到 1MB。
# 缓存上限给得很宽，足以让手表来回切换会话时全部命中。
MEDIA_CACHE_MAX_ITEMS = 300
MEDIA_CACHE_MAX_BYTES = 48 * 1024 * 1024

# 本地缩略图后缀 → Content-Type（扫描落盘时按抖音返回的 content-type 选后缀）
MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


class BridgeError(RuntimeError):
    """桥接服务对外暴露的错误，message 会直接显示在手表上。"""


class DouyinBridge:
    def __init__(self) -> None:
        self.settings: Settings | None = None
        # 手表上要显示哪些会话（`config.json` 的 `friends`）。启动时读一次，
        # 改完文件要重启服务才生效。
        self.friends: list[str] = []
        self._session_cm = None
        self._page = None
        self._context = None
        self._chat: DouyinChat | None = None
        self._lock = asyncio.Lock()
        self._lock_cm = None
        self._current_chat: str | None = None
        self._ready = False
        # 会话已经不可挽回地坏了（典型的是用户把浏览器窗口叉掉了）。这类故障自己
        # 恢复不了，只能由用户重启服务，所以单独记着 —— 不记的话 ready 会一直是
        # True，手表以为只是某一个会话有问题，于是反复重试同一个必然失败的操作。
        self._session_error: str | None = None
        self._media_cache: OrderedDict[str, tuple[str, bytes, str]] = OrderedDict()
        self._media_cache_bytes = 0
        self._media_hits = 0
        self._media_misses = 0
        # 手表端的表情库（watch_stickers.json，由 scripts/scan_stickers.py 扫出来）。
        self.stickers_lib: StickerLibrary | None = None
        self._library_mtime: float | None = None

    # ------------------------------------------------------------------ 生命周期

    def _load_friends(self) -> list[str]:
        """读 `config.json` 的 `friends`。

        早失败：名单读不出来就直接起不来、把原因摆在手表上，比「服务在跑但一个
        会话都列不出来」好判断得多。
        """
        assert self.settings is not None
        try:
            return watch_friends.load_friend_names(self.settings.config_path)
        except watch_friends.WatchFriendsError as exc:
            raise BridgeError(str(exc)) from exc

    async def start(self) -> None:
        if self.settings is None:
            self.settings = load_settings()
        if not self.friends:
            self.friends = self._load_friends()
        if self.stickers_lib is None:
            self.load_sticker_library()

        if self.settings.headless:
            LOGGER.warning(
                "检测到 HEADLESS=true：无头模式容易被抖音识别导致页面不响应，"
                "建议在 .env 中设置 HEADLESS=false"
            )

        # 与定时任务互斥：进程存活期间持有锁；被强杀后可用 scripts/clear_stale_lock.py 清理。
        self._lock_cm = run_lock(self.settings.artifacts_dir / "run.lock")
        try:
            self._lock_cm.__enter__()
        except AlreadyRunningError as exc:
            raise BridgeError(str(exc)) from exc

        started = time.perf_counter()
        try:
            self._session_cm = open_douyin(self.settings)
            session = await self._session_cm.__aenter__()
            self._page = session.page
            self._context = session.context
            self._chat = DouyinChat(self._page)
            await open_private_messages(self._page)
            await verify_login(self._page)
        except Exception:
            await self.stop()
            raise
        self._ready = True
        self._session_error = None
        LOGGER.info("抖音会话已就绪（可见浏览器窗口已打开，耗时 %.2fs）", time.perf_counter() - started)

    async def stop(self) -> None:
        self._ready = False
        self._current_chat = None
        if self._session_cm is not None:
            try:
                await self._session_cm.__aexit__(None, None, None)
            except Exception:
                LOGGER.exception("关闭浏览器失败")
            self._session_cm = None
        self._page = None
        self._chat = None
        self._context = None
        self._media_cache.clear()
        self._media_cache_bytes = 0
        if self._lock_cm is not None:
            try:
                self._lock_cm.__exit__(None, None, None)
            except Exception:
                LOGGER.exception("释放 run.lock 失败")
            self._lock_cm = None

    @property
    def ready(self) -> bool:
        return self._ready and self._page is not None and self._session_error is None

    @property
    def session_error(self) -> str | None:
        """会话级故障的说明；为 None 表示会话本身没问题。"""
        return self._session_error

    def _mark_session_broken(self, reason: str) -> BridgeError:
        """把会话标记成不可用，并返回一个可以直接显示在手表上的错误。

        只在「自己恢复不了」的故障上用：观众是手表上的用户，他要的是
        「去电脑上重启服务」，而不是一个英文的 Playwright 异常名。
        """
        if self._session_error is None:
            LOGGER.error("会话已不可用：%s（%s）", reason, self._current_chat)
        self._session_error = reason
        self._ready = False
        self._current_chat = None
        return BridgeError(reason)

    def note_page_failure(self, exc: BaseException) -> BridgeError | None:
        """页面操作抛异常时的兜底翻译。

        返回 None 表示这不是「页面已经没了」类的故障，调用方按自己原有的方式处理；
        返回 BridgeError 则说明会话已经废了，直接抛出去即可。
        """
        reason = closed_page_message(exc)
        if reason is None:
            return None
        return self._mark_session_broken(reason)

    # ------------------------------------------------------------------ 基础操作

    async def status(self) -> dict[str, Any]:
        watch_stickers: list[dict[str, Any]] = []
        try:
            library = self.library()
            watch_stickers = [
                item.to_watch(self.sticker_payload_bytes(library, item))
                for item in library.enabled()
            ]
        except BridgeError:
            # 表情库读不出来不该让整个状态接口失败：手表端退化成只有快捷表情。
            LOGGER.exception("读取表情库失败")
        return {
            "ready": self.ready,
            # 会话起不来／已经坏了时把原因带出去。手表端据此显示「哪里不对」，
            # 而不是停在「抖音会话正在启动，请稍候」上无限等下去。
            "error": self._session_error,
            "current_chat": self._current_chat,
            "friends": self.list_friends(),
            "presets": self.presets(),
            "watch_stickers": watch_stickers,
            # 全量拉一遍表情图标要多少流量。累加上面逐项报出的数字，
            # 而不是另算一份 —— 两个数不一致的话，手表会按一个数做决策、按另一个数付费。
            "sticker_bytes": sum(item.get("thumbBytes") or 0 for item in watch_stickers),
        }

    def sticker_payload_bytes(self, library: StickerLibrary, item: LibraryItem) -> int:
        """这个表情真正会传到手表上的字节数。

        手表上显示的是派生小图（几 KB），不是 artifacts/stickers/ 里的原图
        （动图能有 1MB+）。手表拿这个数字判断「要不要懒加载」，
        报原图大小会让它以为一屏几十兆，白白多等一轮。
        """
        path = library.thumb_file(item)
        if path is None:
            return 0
        try:
            small = sticker_thumbs.derived_path(
                path, self.sticker_cache_dir(), sticker_thumbs.WATCH_PX
            )
            if small.is_file():
                return small.stat().st_size
        except (BridgeError, OSError):
            pass
        return library.thumb_bytes(item)

    def list_friends(self) -> list[str]:
        return list(self.friends)

    def presets(self) -> list[str]:
        """手表端「快捷短语」的初始内容。

        手表头一次连上时会拿它把短语列表播一遍种，省得用户从零开始敲字。取的是
        表情库里名字靠前的几项 —— 都是这台电脑上扫过的、手表本来就会显示的表情，
        不依赖任何手工配置。
        """
        try:
            items = self.library().enabled()
        except BridgeError:
            return []
        names: list[str] = []
        for item in items:
            label = item.label or item.name
            if label and label not in names:
                names.append(label)
            if len(names) >= PRESET_LIMIT:
                break
        return names

    async def ensure_chat(self, name: str) -> None:
        if self._session_error:
            raise BridgeError(self._session_error)
        if not self.ready:
            raise BridgeError("抖音会话未就绪，请稍后重试")
        if self._current_chat == name:
            return
        assert self._chat is not None and self._page is not None

        started = time.perf_counter()
        opened = False
        try:
            opened = await open_via_conversation_list(self._chat, name)
        except Exception:
            LOGGER.debug("快速切换会话异常，回退搜索", exc_info=True)
        if opened:
            LOGGER.info("会话切换：点列表直达「%s」（%.2fs）", name, time.perf_counter() - started)
        else:
            await self._chat.open_target(name)
            LOGGER.info("会话切换：搜索打开「%s」（%.2fs）", name, time.perf_counter() - started)

        # 会话切换后消息列表是异步渲染的：轮询等它挂载稳定，就绪即返回，
        # 不再固定睡 2 秒（实测通常 0.3-0.6 秒即可读完）。
        stable = await wait_for_message_list(self._page, timeout_ms=3_000)
        if not stable:
            LOGGER.warning("会话「%s」的消息列表未在超时前稳定，仍继续读取", name)
        self._current_chat = name
        LOGGER.info("会话「%s」就绪，总计 %.2fs", name, time.perf_counter() - started)

    # ------------------------------------------------------------------ 对外能力

    async def read_chat(self, name: str, limit: int = DEFAULT_READ_LIMIT) -> dict[str, Any]:
        started = time.perf_counter()
        async with self._lock:
            try:
                await self.ensure_chat(name)
                assert self._page is not None
                data = await read_messages(self._page, limit=limit)
            except (PageOperationError, AuthenticationError, RiskControlError) as exc:
                self._current_chat = None
                raise BridgeError(_friendly_error(exc)) from exc
            except Exception as exc:  # noqa: BLE001
                # 浏览器窗口被关掉之后抛的是 playwright 自己的异常，不属于上面那三类。
                # 认出来就把会话标记成不可用，否则 status() 会一直报 ready=True，
                # 手表那边看起来就是「一切正常，但读什么都是错的」。
                broken = self.note_page_failure(exc)
                if broken is None:
                    raise
                raise broken from exc
            if not data.get("found"):
                raise BridgeError("未找到消息列表，页面结构可能已变化")
        messages = data["messages"]
        LOGGER.info(
            "读会话「%s」：%d 条，%.2fs", name, len(messages), time.perf_counter() - started
        )
        return {"friend": name, "rev": _revision(messages), "messages": messages}

    async def read_conversations(self, limit: int = 30) -> list[dict[str, Any]]:
        async with self._lock:
            if not self.ready:
                raise BridgeError("抖音会话未就绪")
            assert self._page is not None
            return await read_conversations(self._page, limit=limit)

    async def read_all_conversations(
        self,
        limit: int = 200,
        *,
        should_stop: Callable[[], bool] | None = None,
    ) -> list[dict[str, Any]]:
        """滚到底读会话列表 —— 配置页拿它让用户勾选「手表上要显示谁」。

        与 `read_conversations` 的区别：那个只看当前一屏（20~30 个）。这个会一边滚
        一边收集，所以会占着浏览器几秒到十几秒。调用方（配置页）把它放在后台线程里
        跑、页面轮询看进度，因此不阻塞别的命令。
        """
        async with self._lock:
            if not self.ready:
                raise BridgeError("抖音会话未就绪")
            assert self._page is not None
            return await read_all_conversations(
                self._page, limit=limit, should_stop=should_stop, log=LOGGER.info
            )

    async def send_message(self, name: str, text: str, sticker: str | None = None) -> dict[str, Any]:
        text = (text or "").strip()
        if sticker:
            return await self._deliver(name, text="", sticker_name=sticker)
        if not text:
            raise BridgeError("消息内容不能为空")
        if len(text) > MAX_TEXT_LENGTH:
            raise BridgeError(f"消息过长，最多 {MAX_TEXT_LENGTH} 字")
        return await self._deliver(name, text=text, sticker_name=None)

    async def _deliver(self, name: str, text: str, sticker_name: str | None) -> dict[str, Any]:
        """发送文字或原生表情，成功后顺手回传最新消息列表。

        回传列表让手表端不必再发一次 /api/messages，发送后的画面更新是即时的。
        """
        started = time.perf_counter()
        async with self._lock:
            try:
                # 每次发送都重开一份耗时拆解：上一次的步骤不能串到这一次的日志里
                reset_send_profile()
                async with send_step("切会话"):
                    await self.ensure_chat(name)
                async with send_step("发送"):
                    if sticker_name is not None:
                        await self._send_sticker_with_resync(sticker_name)
                    else:
                        assert self._chat is not None
                        await send_text(self._chat, text)
                async with send_step("回读"):
                    snapshot = await self._snapshot()
            except (PageOperationError, AuthenticationError, RiskControlError) as exc:
                self._current_chat = None
                raise BridgeError(_friendly_error(exc)) from exc

        result: dict[str, Any] = {"friend": name, "ok": True}
        if sticker_name is not None:
            result["sticker"] = sticker_name
        else:
            result["text"] = text
        if snapshot is not None:
            result["rev"] = snapshot["rev"]
            result["messages"] = snapshot["messages"]
        # 一条消息一行、带上每一步的耗时。手表上「发一条要等好几秒」是反复出现的
        # 抱怨，而这条链路上七八个步骤各自都可能慢 —— 只报总数等于没法判断该动哪。
        profile = format_send_profile()
        LOGGER.info(
            "发送给「%s」完成：%s，%.2fs%s",
            name,
            sticker_name or text,
            time.perf_counter() - started,
            f"（{profile}）" if profile else "",
        )
        return result

    async def _send_sticker_with_resync(self, sticker_name: str) -> None:
        """发表情；遇到「面板与库对不上」就先重扫一次再发。

        重试是安全的：`StickerOutOfSyncError` 只在**还没点下去**时抛出
        （见 `app/sender.send_douyin_sticker`），不存在把同一张图发两遍的风险。
        什么时候会对不上：用户在手机上收藏了新表情 → 面板里的项整体顺移 →
        库里那张图的位置找不到了。重扫一次就把库对齐到当前面板，然后照发。
        """
        assert self._page is not None
        try:
            await send_douyin_sticker(self._page, self._sticker(sticker_name))
            return
        except StickerOutOfSyncError:
            LOGGER.info("表情面板与库对不上，重扫后重试：%s", sticker_name)
        async with send_step("重扫"):
            await self.refresh_stickers()
        assert self._page is not None
        await send_douyin_sticker(self._page, self._sticker(sticker_name))

    async def _snapshot(self) -> dict[str, Any] | None:
        """读取当前会话快照；失败不影响已经成功的发送。"""
        if self._page is None:
            return None
        try:
            data = await read_messages(self._page, limit=DEFAULT_READ_LIMIT)
        except Exception:
            LOGGER.debug("发送后回读消息列表失败", exc_info=True)
            return None
        if not data.get("found"):
            return None
        messages = data["messages"]
        return {"rev": _revision(messages), "messages": messages}

    async def fetch_media(self, url: str) -> tuple[str, bytes, str]:
        """代理获取表情/图片二进制（借用浏览器的登录态与请求上下文）。

        只允许抖音自有 CDN，避免被当成任意 URL 的开放代理。
        返回 (content-type, body, etag)，命中内存缓存时不会再次访问网络。
        `etag` 供 HTTP 层做 304 协商，让手表很少重复下载同一张图。
        """
        _ensure_allowed_media_url(url)
        cached = self._media_cache.get(url)
        if cached is not None:
            self._media_cache.move_to_end(url)
            self._media_hits += 1
            return cached
        if self._session_error:
            raise BridgeError(self._session_error)
        if not self.ready or self._context is None:
            raise BridgeError("抖音会话未就绪，无法获取图片")
        self._media_misses += 1
        try:
            response = await self._context.request.get(
                url,
                headers={"Referer": "https://www.douyin.com/", "Accept": "image/*,*/*;q=0.8"},
                timeout=20_000,
            )
        except Exception as exc:  # noqa: BLE001 - 网络异常统一转成前端可读错误
            raise BridgeError("图片获取超时或失败") from exc
        if not response.ok:
            raise BridgeError(f"图片获取失败（HTTP {response.status}）")
        body = await response.body()
        content_type = (response.headers or {}).get("content-type", "image/jpeg").split(";")[0].strip()
        if not content_type.startswith("image/"):
            raise BridgeError("返回内容不是图片")
        entry = (content_type, body, hashlib.sha1(body).hexdigest())
        self._cache_media(url, entry)
        return entry

    def _cache_media(self, url: str, entry: tuple[str, bytes, str]) -> None:
        self._media_cache[url] = entry
        self._media_cache_bytes += len(entry[1])
        self._media_cache.move_to_end(url)
        while self._media_cache and (
            len(self._media_cache) > MEDIA_CACHE_MAX_ITEMS
            or self._media_cache_bytes > MEDIA_CACHE_MAX_BYTES
        ):
            _, evicted = self._media_cache.popitem(last=False)
            self._media_cache_bytes -= len(evicted[1])

    def media_stats(self) -> dict[str, int]:
        return {
            "items": len(self._media_cache),
            "bytes": self._media_cache_bytes,
            "hits": self._media_hits,
            "misses": self._media_misses,
        }

    async def diagnose_send_path(self) -> dict[str, Any]:
        """量一下发送确认链路上各步骤的真实往返耗时（只读，不发消息）。

        发送确认要在同一个气泡上反复查标记，老实现每个 selector 两次 CDP 往返，
        一轮 10 个标记就是 20 次，而 2 秒观察窗口里要跑 7 轮 ≈ 140 次 —— 这正是
        从手表发一条消息要 10 秒的主因。这里把「批量 JS 查询」（当前实现）和
        「逐个 selector 查询」（参考实现）的单轮耗时都量出来作对比。
        """
        if not self.ready or self._page is None:
            raise BridgeError("抖音会话未就绪")
        from app.sender import (
            LATEST_OUTGOING_MESSAGE,
            SEND_FAILURE_MARKERS,
            SEND_PENDING_MARKERS,
            _mark_latest_outgoing_message,
            _marker_visible,
            _publish_ready,
        )

        page = self._page
        async with self._lock:
            started = time.perf_counter()
            scope = page.locator(LATEST_OUTGOING_MESSAGE).first
            if not await scope.count():
                raise BridgeError("当前会话没有「已发送」消息，请先打开一个有聊天记录的会话")
            resolve_ms = (time.perf_counter() - started) * 1000

            started = time.perf_counter()
            await _mark_latest_outgoing_message(page)
            mark_ms = (time.perf_counter() - started) * 1000

            started = time.perf_counter()
            await _publish_ready(page)
            publish_ms = (time.perf_counter() - started) * 1000

            rounds = 4
            started = time.perf_counter()
            for _ in range(rounds):
                await _marker_visible(scope, SEND_FAILURE_MARKERS)
                await _marker_visible(scope, SEND_PENDING_MARKERS)
            bulk_ms = (time.perf_counter() - started) * 1000 / rounds

            started = time.perf_counter()
            for _ in range(rounds):
                await _marker_visible_reference(scope, SEND_FAILURE_MARKERS)
                await _marker_visible_reference(scope, SEND_PENDING_MARKERS)
            reference_ms = (time.perf_counter() - started) * 1000 / rounds

            # 诊断会留下确认锚点属性，清掉，避免影响后续判断
            try:
                await page.evaluate(
                    "() => { document.querySelectorAll('[data-douyin-sender-anchor]')"
                    ".forEach((el) => el.removeAttribute('data-douyin-sender-anchor')); }"
                )
            except Exception:
                LOGGER.debug("清理诊断锚点失败", exc_info=True)

        polls = 2000 // 300 + 1
        return {
            "current_chat": self._current_chat,
            "resolve_scope_ms": round(resolve_ms, 1),
            "mark_latest_ms": round(mark_ms, 1),
            "publish_ready_ms": round(publish_ms, 1),
            "marker_bulk_ms_per_round": round(bulk_ms, 1),
            "marker_reference_ms_per_round": round(reference_ms, 1),
            "marker_polls_per_send": polls,
            "projected_bulk_ms": round(bulk_ms * polls),
            "projected_reference_ms": round(reference_ms * polls),
        }

    def _sticker(self, name: str) -> Sticker:
        # 手表上显示的就是库里那一项（带缩略图），发送必须用同一项，否则会出现
        # 「点的是这个、发出去的是那个」。所以表情库是唯一的来源。
        if self.settings is not None:
            item = self.library().by_ref(name)
            if item is not None and item.enabled:
                return item.to_sticker()
        raise BridgeError(f"表情库里没有这个表情: {name}（在手表上刷新一次表情面板再试）")

    # ------------------------------------------------------------------ 手表端表情库

    def library(self) -> StickerLibrary:
        """拿到表情库。

        每次都比对一下文件 mtime：扫描脚本写完 watch_stickers.json 之后，
        手表刷新一下就能看到新表情，不必重启服务。
        """
        if self.settings is None:
            raise BridgeError("配置未加载，无法读取表情库")
        if self.stickers_lib is None:
            return self.load_sticker_library()
        try:
            mtime: float | None = self._library_path().stat().st_mtime
        except OSError:
            mtime = None
        if mtime != self._library_mtime:
            return self.load_sticker_library()
        return self.stickers_lib

    def library_mtime(self) -> float | None:
        """表情库文件的 mtime；读不到按 None。

        推送线程拿它判断「库里有没有新东西」，不触发重载 —— 真正的重载
        还是交给 library() 的 mtime 比对。
        """
        try:
            return self._library_path().stat().st_mtime
        except OSError:
            return None

    async def refresh_stickers(self, *, should_yield=None) -> dict[str, Any]:
        """在常驻会话里重扫表情面板，把新收藏的表情补进手表表情库。

        为什么要由服务自己来做：`scripts/scan_stickers.py` 是离线扫描，必须独占运行锁，
        跑之前得先把服务停掉 —— 于是「刚收藏的表情」要等很久才可能出现在手表上。
        服务这边本来就有一个已登录的常驻页面，顺手刷一下就行，不用停服务、也不用第二次登录
        （少一次风控暴露面）。刷新后库文件 mtime 变化，推送循环会自动把新状态推给手表。

        整段走 `self._lock`，与发送/读消息互斥 —— 扫描期间会点开表情面板并切分类栏，
        绝不能和一次正在进行的发送交错。

        `should_yield` 是给刷新用的让路探针：这一整段会独占页面十几秒，而它常常是被
        「用户刚打开表情面板」踢起来的，用户下一步很可能就是发消息。探针说「有人等了」
        时抛 `RefreshYielded`（本次还没落盘，取消是干净的），由调用方另择时机重排。

        耗时参考：库没变时 5~10 秒（切分类栏为主，缩略图只在有新增时才下载）。
        """
        async with self._lock:
            if self._page is None or self._context is None:
                raise BridgeError("抖音会话未就绪，无法刷新表情")
            if self.settings is None:
                raise BridgeError("配置未加载，无法刷新表情")

            # 必须先有一个打开的会话：表情按钮长在输入区里，而输入区只在选中会话后出现。
            # 服务刚启动时停在会话列表页，直接去找按钮就会「找不到页面元素」
            # （这个坑是端到端测出来的，离线扫描脚本不会遇到 —— 它开头就自己打开了会话）。
            # 已经打开着会话就沿用它：不为刷新去切走用户正在看的对话。
            if not self._current_chat:
                friends = self.list_friends()
                if not friends:
                    raise BridgeError("config.json 里没有 friends，无法打开会话去取表情面板")
                await self.ensure_chat(friends[0])

            from bridge.sticker_refresh import refresh_stickers as _refresh

            started = time.perf_counter()
            try:
                result = await _refresh(
                    page=self._page,
                    context=self._context,
                    artifacts_dir=self.settings.artifacts_dir,
                    log=LOGGER.info,
                    should_yield=should_yield,
                )
            except RefreshYielded:
                # 让路不是失败：什么都不算改过，调用方会另挑一个空闲时机再排一次。
                # 必须排在下面那个 RuntimeError 之前 —— RefreshYielded 也是 RuntimeError，
                # 被那条吃掉的话就会显示成「刷新表情失败」的警告，而其实什么也没坏。
                raise
            except RuntimeError as exc:
                # 面板没出现 / 消息指纹变了 —— 都是「这次没刷成」，不是会话坏了，
                # 不该把整个会话标成故障，下次再试。
                raise BridgeError(f"刷新表情失败：{exc}") from exc
            # 库文件刚被改写，把内存里那份丢掉，下次 library() 重新读盘。
            self.stickers_lib = None
            self._library_mtime = None
            LOGGER.info(
                "表情库刷新完成：%s 项（新增 %s），%.1fs",
                result.get("items"),
                result.get("added"),
                time.perf_counter() - started,
            )
            return result

    def _library_path(self) -> Path:
        assert self.settings is not None
        return self.settings.config_path.resolve().parent / LIBRARY_FILENAME

    def load_sticker_library(self) -> StickerLibrary:
        """载入 watch_stickers.json。文件缺失是正常情况（还没扫描过），按空库处理。"""
        if self.settings is None:
            raise BridgeError("配置未加载，无法载入表情库")
        path = self._library_path()
        library = StickerLibrary.load(path.parent, self.settings.artifacts_dir.resolve())
        self.stickers_lib = library
        try:
            self._library_mtime = path.stat().st_mtime
        except OSError:
            self._library_mtime = None
        LOGGER.info("手表端表情库已载入: %s（%s）", library.stats(), library.path)
        return library

    def sticker_library_payload(self) -> dict[str, Any]:
        library = self.library()
        enabled = library.enabled()
        return {
            "updated_at": library.path.stat().st_mtime if library.path.is_file() else None,
            "stats": library.stats(),
            # 和 status() 用同一个算法算 thumbBytes —— 两个接口报同一个字段却给不同的数，
            # 客户端按哪个做决策就全看它先读到谁了。
            "items": [item.to_watch(self.sticker_payload_bytes(library, item)) for item in enabled],
        }

    def sticker_cache_dir(self) -> Path:
        """表情小图缓存目录（artifacts/sticker_thumbs/）。

        独立于原图目录，纯缓存，可以整个删掉重建。
        """
        if self.settings is None:
            raise BridgeError("配置未加载，无法定位表情小图缓存")
        return sticker_thumbs.cache_dir(self.settings.artifacts_dir)

    def sticker_thumb(self, ref: str, px: int = 0) -> tuple[str, bytes, str]:
        """读表情库的缩略图，返回 (content-type, body, etag)。

        `px` 是客户端要的最长边（0 = 要原图）。

        图在扫描时就落到 artifacts/stickers/ 了，这里不访问网络 ——
        抖音的表情图 URL 带签名会过期，落盘才能长期显示；
        顺带的好处是「抖音会话没就绪」时表情图标照样看得见。

        走派生小图（artifacts/sticker_thumbs/）：原图里有 1MB+ 的动图，每次现场
        解码再缩放要几十毫秒（实测平均 35ms、最慢 985ms），而这个方法是同步跑在
        请求线程里的。派生图按客户端要的尺寸预生成一次并落盘，之后服务器只要读个
        几 KB 的小文件，而且尺寸正好，连缩放都能跳过。服务重启也还在。
        只在「不比原图更大」时走这条路：客户端要的比 MAX_PX 还大，说明它想要大图，
        这时派生图帮倒忙（还得放大），直接给原图更清楚。
        """
        library = self.library()
        item = library.by_ref(ref)
        if item is None:
            raise BridgeError("表情库里没有这一项")
        path = library.thumb_file(item)
        if path is None:
            raise BridgeError("这个表情没有本地缩略图，重新扫描一次试试")
        if 0 < px <= sticker_thumbs.MAX_PX:
            try:
                cache = self.sticker_cache_dir()
            except BridgeError:
                cache = None
            if cache is not None:
                small = sticker_thumbs.ensure(path, cache, px)[0]
                if small.is_file():
                    path = small
        try:
            body = path.read_bytes()
        except OSError as exc:
            raise BridgeError("缩略图读取失败") from exc
        content_type = MIME_BY_SUFFIX.get(path.suffix.lower(), "image/png")
        return content_type, body, hashlib.sha1(body).hexdigest()


async def _marker_visible_reference(scope, selectors) -> bool:
    """旧版逐 selector 查询的等价实现，仅供诊断对比耗时使用。"""
    for selector in selectors:
        marker = scope.locator(selector).first
        try:
            if await marker.count() and await marker.is_visible():
                return True
        except Exception:
            continue
    return False


def _revision(messages: list[dict[str, Any]]) -> str:
    """给一份消息列表算内容指纹。

    刻意不包含 `data-index`：抖音的 DOM 序号在虚拟列表滑动时会重排，但内容没变，
    用它算指纹会造成大量「假变化」，让手表做无意义的整屏重绘。
    """
    digest = hashlib.sha1()
    for message in messages:
        digest.update(
            "|".join(
                (
                    str(message.get("side") or ""),
                    str(message.get("type") or ""),
                    str(message.get("time") or ""),
                    str(message.get("text") or ""),
                    str(message.get("media") or ""),
                )
            ).encode("utf-8")
        )
        digest.update(b"\n")
    return digest.hexdigest()[:16]


def _ensure_allowed_media_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise BridgeError("只允许 https 图片地址")
    host = (parsed.hostname or "").lower()
    if not any(host.endswith(suffix) for suffix in ALLOWED_MEDIA_HOSTS):
        raise BridgeError("该图片域名不在允许列表内")


def _friendly_error(exc: Exception) -> str:
    if isinstance(exc, AuthenticationError):
        return "抖音登录状态已失效，请在电脑上重新扫码登录（scripts/login_auto.py）"
    if isinstance(exc, RiskControlError):
        return "抖音要求安全验证，请在电脑上的浏览器窗口里完成验证"
    closed = closed_page_message(exc)
    if closed is not None:
        return closed
    message = str(exc) or exc.__class__.__name__
    if "搜索不到目标好友" in message:
        return "在抖音里找不到这个好友，请检查昵称是否与搜索结果一致"
    if "找不到页面元素" in message:
        return "页面元素未就绪（可能是网络慢或页面结构变化），请重试"
    return message


# Playwright 在「页面/上下文/浏览器已经没了」时抛的异常类名。
#
# 按类名匹配而不是 import：这些类型在 playwright 的私有模块
# （playwright._impl._errors）里，公开的 async_api 并没有把它们导出来。
# 版本一升级，import 路径就可能失效，而类名从 1.x 起一直没变过。
_CLOSED_ERROR_NAMES = ("TargetClosedError", "BrowserClosedError", "DriverClosedError")


def closed_page_message(exc: BaseException) -> str | None:
    """如果这个异常是「浏览器窗口已经不在了」，返回给用户看的说明；否则 None。

    这个场景在实机上很常见：用户习惯性地把 Chromium 窗口叉掉。此时 self._page
    还指着一个死掉的 Page，`ready` 也还是 True，于是每一次读会话都抛
    TargetClosedError —— 它的文本是英文的运行时描述（"Target page, context or
    browser has been closed"），摆在手表上既看不懂也指导不了操作。
    """
    for cls in type(exc).__mro__:
        if cls.__name__ in _CLOSED_ERROR_NAMES:
            return "抖音浏览器窗口已被关闭，请在电脑上重启桥接服务"
    return None


__all__ = ["BridgeError", "DouyinBridge", "closed_page_message"]
