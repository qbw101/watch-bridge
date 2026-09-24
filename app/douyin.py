from __future__ import annotations

import asyncio
import re

from playwright.async_api import Locator, Page

from app.selectors import CHAT_PANEL_MARKERS, MESSAGE_INPUTS, SEARCH_INPUTS


class PageOperationError(RuntimeError):
    pass


class StickerOutOfSyncError(PageOperationError):
    """表情面板里找不到库里记的那张图 —— 多半是刚收藏了表情、顺序顺移了。

    单独一个类型是为了让上层能「重扫一次再试」，而不是把失败直接甩给用户：
    订阅号式的重试在这里是安全的，因为我们**还没点下去**，没有重复发送的风险。
    """
    pass


class RefreshYielded(RuntimeError):
    """扫描/刷新被「有用户操作在等浏览器」打断，主动让路。

    表情库刷新要独占浏览器十几秒（切分类栏、比对消息指纹）。而它恰恰是**用户
    打开表情面板时**被触发的 —— 如果就这么占着不放，用户接着点一下发送，那一帧
    就得排在十几秒后面，表现成「打开表情面板之后发消息特别慢」。

    所以刷新在每一步之间瞄一眼「有没有界面请求在等」，有就抬腿走人、把这个异常
    抛出去，由服务端过一会儿再排一次。抛出它的前提是**本次什么都还没落盘**，
    取消是干净的。
    """
    pass


RETRY_DELAY_MS = 3_000


class DouyinChat:
    def __init__(
        self,
        page: Page,
        timeout_ms: int = 15_000,
        confirm_timeout_ms: int = 15_000,
    ) -> None:
        self.page = page
        self.timeout_ms = timeout_ms
        self.confirm_timeout_ms = confirm_timeout_ms

    async def open_target(self, name: str, retries: int = 1) -> None:
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            try:
                await self._open_target_once(name)
                return
            except Exception as exc:
                last_error = exc
                # 「搜索不到目标好友」是确定性结论，不是「页面还没加载好」。
                # 重试一次要多花十几秒，而桥接服务这条链路上这个等待会直接顶到
                # 请求超时 —— 手表端看到的就是「读一个不存在的会话要等一分钟」。
                if "搜索不到目标好友" in str(exc):
                    break
                if attempt < retries:
                    await self.page.wait_for_timeout(RETRY_DELAY_MS)
        if last_error is not None:
            raise last_error
        raise PageOperationError("打开聊天失败")

    async def _open_target_once(self, name: str) -> None:
        search = await first_visible(self.page, SEARCH_INPUTS, self.timeout_ms)
        await search.click()
        await search.fill("")
        await search.fill(name)
        await self.page.wait_for_timeout(1_500)

        result = await self._search_result(name)
        if result is None:
            raise PageOperationError("搜索不到目标好友")
        await result.click(force=True)
        await self._confirm_opened(name)

    async def _search_result(self, name: str) -> Locator | None:
        # Search mode renders a separate SearchPanel. Its "发消息" action is the
        # correct control; clicking the hidden conversation cache does not mount
        # the composer.
        #
        # Identity must be resolved from the per-result name node, never from the
        # collection container: `[class*="SearchPanelitem"]` also matches an outer
        # `SearchPanelitems` wrapper, whose descendants would then contain the
        # target name while `.first` returns another row's button. Scope to result
        # rows and require the matched name node and its button to be visible here.
        search_items = self.page.locator('[class*="SearchPanelitembox"], [class*="SearchPanelitem-box"], [class*="SearchPanelitem_box"]')
        name_selectors = (
            '[class*="SearchPanelitemtitle"]',
            '[class*="SearchPanelitemTitle"]',
            '[class*="SearchPanelitem_title"]',
            '[class*="SearchPanelitem-title"]',
            '[class*="SearchPanelitemname"]',
            '[class*="SearchPanelitemName"]',
            '[class*="SearchPanelitem_name"]',
            '[class*="SearchPanelitem-name"]',
        )

        # Two-phase priority: an exact friend name always wins over a group whose
        # display name happens to start with it. Pass 1 scans every search row for
        # an exact name; only if none is found does pass 2 accept a group member
        # count suffix like "4161(7)" for target "4161". This ordering guarantees
        # "test" never returns "test(7)" or "test1".
        for index in range(await search_items.count()):
            item = search_items.nth(index)
            name_locator = await _visible_exact_text_locator(item, name_selectors, name)
            if name_locator is None:
                continue
            button = item.locator('[class*="SearchPanelitemchat_btn"]').first
            try:
                if await button.count() and await button.is_visible():
                    return button
            except Exception:
                continue

        for index in range(await search_items.count()):
            item = search_items.nth(index)
            name_locator = await _visible_group_text_locator(item, name_selectors, name)
            if name_locator is None:
                continue
            button = item.locator('[class*="SearchPanelitemchat_btn"]').first
            try:
                if await button.count() and await button.is_visible():
                    return button
            except Exception:
                continue

        # The nickname node can be hidden while its conversation row is visible.
        # Locate and click the complete row instead of relying on text visibility.
        row_selectors = (
            '[data-e2e="conversation-item"]',
            '[class*="conversationConversationItem"]',
            '[class*="conversation-item"]',
            '[class*="ConversationItem"]',
        )
        title_selectors = (
            '[class*="conversationConversationItemtitle"]',
            '[class*="ConversationItemtitle"]',
            '[class*="ConversationItemTitle"]',
            '[class*="conversation-item-title"]',
            '[class*="conversation-item-Title"]',
        )
        for selector in row_selectors:
            rows = self.page.locator(selector)
            for index in range(await rows.count()):
                row = rows.nth(index)
                title_locator = await _visible_exact_text_locator(row, title_selectors, name)
                if title_locator is None:
                    continue
                try:
                    if await row.is_visible():
                        return row
                except Exception:
                    continue

        # Second-phase group suffix over conversation rows (same priority rule).
        for selector in row_selectors:
            rows = self.page.locator(selector)
            for index in range(await rows.count()):
                row = rows.nth(index)
                title_locator = await _visible_group_text_locator(row, title_selectors, name)
                if title_locator is None:
                    continue
                try:
                    if await row.is_visible():
                        return row
                except Exception:
                    continue

        # Some Douyin builds render the title itself as hidden, but keep a visible
        # ancestor as the actionable result. Find that ancestor from the hidden title.
        # This hidden-title fallback stays STRICT exact only: a hidden stale name
        # node (group or plain) must never be trusted to resolve the recipient.
        hidden_titles = self.page.locator('[class*="conversationConversationItemtitle"]')
        for index in range(await hidden_titles.count()):
            title = hidden_titles.nth(index)
            if not await _text_equals(title, name):
                continue
            row = title.locator(
                "xpath=ancestor::*[contains(@class, 'conversationConversationItem')][1]"
            )
            if await row.count() and await row.is_visible():
                return row

        return None

    async def message_input(self) -> Locator:
        return await first_visible(self.page, MESSAGE_INPUTS, self.timeout_ms)

    async def _confirm_opened(self, name: str, timeout_ms: int | None = None) -> None:
        timeout = timeout_ms if timeout_ms is not None else self.confirm_timeout_ms
        deadline = asyncio.get_running_loop().time() + timeout / 1000
        while True:
            last_error = await self._chat_open_error(name)
            if last_error is None:
                return
            if asyncio.get_running_loop().time() >= deadline:
                raise last_error
            await self.page.wait_for_timeout(500)

    async def _chat_open_error(self, name: str) -> PageOperationError | None:
        # Confirm the right-side current chat by the authoritative chat title, which
        # must itself be visible. A visible header retaining a hidden stale name node
        # (common during SPA transitions) must not confirm the wrong recipient, and
        # a secondary username/title field must never substitute for the chat title.
        title_selectors = (
            '[class*="RightPanelHeadertitle"]',
            '[class*="RightPanelHeaderTitle"]',
            '[class*="RightPanelHeader_title"]',
            '[class*="RightPanelHeader-title"]',
            '[class*="chatHeadertitle"]',
            '[class*="ChatHeaderTitle"]',
            '[class*="chatHeader_title"]',
            '[class*="ChatHeader-title"]',
            '[class*="name"]',
            '[class*="Name"]',
            '[class*="nickname"]',
            '[class*="Nickname"]',
        )
        for selector in CHAT_PANEL_MARKERS[:3]:
            headers = self.page.locator(selector)
            for index in range(await headers.count()):
                header = headers.nth(index)
                try:
                    if not await header.is_visible():
                        continue
                except Exception:
                    continue
                if await _visible_exact_or_group_text_in(header, title_selectors, name):
                    return None

        composer_visible = await self._composer_visible()
        return PageOperationError(
            f"点击搜索结果后无法确认聊天已打开（输入框: {'有' if composer_visible else '无'}）"
        )

    async def _composer_visible(self) -> bool:
        for selector in MESSAGE_INPUTS:
            locator = self.page.locator(selector).first
            try:
                if await locator.count() and await locator.is_visible():
                    return True
            except Exception:
                continue
        return False


async def _visible_exact_text_in(container: Locator, selectors: tuple[str, ...], expected: str) -> bool:
    return await _visible_exact_text_locator(container, selectors, expected) is not None


async def _visible_exact_text_locator(
    container: Locator, selectors: tuple[str, ...], expected: str
) -> Locator | None:
    for selector in selectors:
        nodes = container.locator(selector)
        for index in range(await nodes.count()):
            node = nodes.nth(index)
            if await _text_equals(node, expected):
                try:
                    if await node.is_visible():
                        return node
                except Exception:
                    continue
    return None


async def _visible_group_text_locator(
    container: Locator, selectors: tuple[str, ...], expected: str
) -> Locator | None:
    # Second-phase match for group chats: accepts a trailing "(N)"/"（N）"
    # member count. Only reached after the exact pass found nothing, so a bare
    # "test" never reaches here when an exact "test" row exists.
    for selector in selectors:
        nodes = container.locator(selector)
        for index in range(await nodes.count()):
            node = nodes.nth(index)
            if await _group_name_matches(node, expected):
                try:
                    if await node.is_visible():
                        return node
                except Exception:
                    continue
    return None


async def _visible_exact_or_group_text_in(
    container: Locator, selectors: tuple[str, ...], expected: str
) -> bool:
    # Chat-header confirmation: an exact title wins; otherwise a group member
    # count suffix also confirms. The node must be visible in both cases, so a
    # hidden stale title node can never confirm the wrong recipient.
    if await _visible_exact_text_locator(container, selectors, expected) is not None:
        return True
    return await _visible_group_text_locator(container, selectors, expected) is not None


async def _has_exact_text_in(container: Locator, selectors: tuple[str, ...], expected: str) -> bool:
    for selector in selectors:
        if await _has_exact_text(container.locator(selector), expected):
            return True
    return False


async def _has_exact_text(locators: Locator, expected: str) -> bool:
    for index in range(await locators.count()):
        if await _text_equals(locators.nth(index), expected):
            return True
    return False


async def _text_equals(locator: Locator, expected: str) -> bool:
    try:
        actual = (await locator.inner_text(timeout=500)).strip()
        expected = expected.strip()
        # 页面渲染可能把昵称拆成多行（如 "阿华\n7.25"），统一去掉
        # 所有空白字符再比较，避免因空格/换行差异匹配失败。其余字符仍须
        # 完全一致，因此不会把 "test" 误匹配成 "test1"。
        return _strip_all_whitespace(actual) == _strip_all_whitespace(expected)
    except Exception:
        return False


def _strip_all_whitespace(value: str) -> str:
    return re.sub(r"\s+", "", value)


# Matches a group chat display name: the configured target name optionally
# followed by exactly one pair of brackets containing a pure member count,
# e.g. "4161" / "4161(7)" / "4161（123）". This is an INDEPENDENT helper kept
# separate from _text_equals so the friend exact-match semantics stay strict:
# it must never let "test" match "test1" (no trailing brackets to legitimize a
# longer name). re.fullmatch anchors both ends; re.escape makes the name literal.
# Whitespace is normalized on both sides first, so "4161 (7)" matches "4161(7)".


def _group_count_suffix_matches(actual: str, expected: str) -> bool:
    actual = _strip_all_whitespace(actual)
    expected = _strip_all_whitespace(expected)
    if actual == expected:
        return True
    # 归一化后括号可能紧贴名字（如 "4161 (7)" -> "4161(7)"），允许任意空白已被去除
    pattern = re.escape(expected) + r"[\(（]\s*\d+[\)）]"
    return re.fullmatch(pattern, actual) is not None


async def _group_name_matches(locator: Locator, expected: str) -> bool:
    try:
        return _group_count_suffix_matches(
            await locator.inner_text(timeout=500), expected
        )
    except Exception:
        return False


async def first_visible(page: Page, selectors: tuple[str, ...], timeout_ms: int = 15_000) -> Locator:
    """找到第一个「存在且可见」的元素。

    先做一轮**不等待**的快速扫描：绝大多数调用发生时元素已经在页面上了 ——
    发完表情把输入框找回来、开面板、切会话，都是这样。这时一轮 count/is_visible
    就够了，几十毫秒。

    这一步不是微优化，它一度占掉整条发送链路六成的时间：带等待的版本是「逐个
    selector 各等 timeout_ms / N」，而选择器列表是**按新旧写法排序**的，抖音改版
    之后排在前面的大多已经失效，于是每次调用都要白等 (命中位置 - 1) × per_selector。
    实测「发表情之后把输入框找回来」那一步要 4.4 秒（前 3 个 selector 各等 1.43 秒
    才轮到第 4 个命中），而那个输入框自始至终都好好地待在那儿。

    全都不可见才退回带等待的版本 —— 那种情况是「元素还没渲染出来」（刚切完会话、
    面板刚打开），仍然需要等。多花的是每个 selector 两次极快的查询，可以忽略。
    """
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if await locator.count() and await locator.is_visible():
                return locator
        except Exception:  # noqa: BLE001 - 单个 selector 语法/状态问题不该中断整轮
            continue

    per_selector = max(500, timeout_ms // max(1, len(selectors)))
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=per_selector)
            return locator
        except Exception:
            continue
    raise PageOperationError(f"找不到页面元素，已尝试: {', '.join(selectors)}")
