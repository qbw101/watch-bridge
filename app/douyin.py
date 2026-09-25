from __future__ import annotations

import asyncio
import re

from playwright.async_api import Locator, Page

from app.names import normalize_name
from app.selectors import (
    CHAT_PANEL_MARKERS,
    MESSAGE_INPUTS,
    SEARCH_CANCEL_BUTTONS,
    SEARCH_INPUTS,
    SEARCH_PANEL_MARKERS,
)


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

# `_confirm_opened` 里「点击之后面板收起」的宽限期。点下搜索结果到面板关闭、聊天
# 面板挂上是异步的，要几百毫秒；宽限期内不因「面板还在」下结论，超过它仍挂着，
# 就说明那一下点击没生效（多半点到了搜索态下不可见的残留行），此时立刻报错 ——
# 实测等满 15 秒预算只是把「点了没反应」拖成「手表转圈 15 秒」。
PANEL_CLOSE_GRACE_MS = 2_500

# 「搜索面板在不在」的一次性判据（见 `DouyinChat.search_mode_active`）。
# 用 JS 而不是若干次 locator 往返：面板的 class 在 DOM 里有很多层，Python 侧逐个
# selector 查可见性既慢、又可能正好命中隐藏的那一层。
_SEARCH_PANEL_VISIBLE_JS = """(selectors) => {
  for (const selector of selectors) {
    for (const el of document.querySelectorAll(selector)) {
      const rect = el.getBoundingClientRect();
      if (rect.width <= 0 || rect.height <= 0) continue;
      const style = getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden') continue;
      return true;
    }
  }
  return false;
}"""


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
        # 名单里理论上已经是干净名字（落盘前收过一道），这里再收一次：搜索框是
        # 单行输入框，名字里带进换行会被浏览器转成空格，搜的就不是这个人了。
        name = normalize_name(name)
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

        try:
            result = await self._search_result(name)
            if result is None:
                # 带上名字：日志里能一眼看出是哪个没搜到、长什么样，不用回头翻名单。
                raise PageOperationError(f"搜索不到目标好友「{name}」")
            await result.click(force=True)
            await self._confirm_opened(name)
        except Exception:
            # 失败时先把搜索态收干净再抛出。搜索是有状态的，留着它会让**后续**每
            # 一次读会话列表都拿到一份错名单（见 `leave_search_mode` 的说明）。
            # 清场本身失败也不能顶掉手上的原始异常，所以这里不看它返回什么。
            await self.leave_search_mode()
            raise

    async def _search_result(self, name: str) -> Locator | None:
        """找到目标好友那一行、可以点开聊天的控件。搜不到时返回 None。

        搜索结果与左侧会话列表是**两套 DOM**，先决定去哪一套里找 —— 判据就是
        「搜索面板在不在」，理由见 `search_mode_active`。
        """
        if await self.search_mode_active():
            return await self._match_in_search_panel(name)
        return await self._match_in_conversation_rows(name)

    async def search_mode_active(self) -> bool:
        """页面是否停在搜索态（搜索面板还挂着）—— 「搜索到底生效没有」的判据。

        这个判据是 2026-09-25 加的，为的是砍掉一次 13 秒的白跑：搜不到一个名字
        时，`_search_result` 原先不管搜索有没有生效，都会「先在结果面板找一遍，
        再回退把左侧会话列表整个扫两遍」。而搜索一旦生效，左侧列表就被面板盖住
        （实测 `conversationConversationItem` 首行可见性为 False），那两遍扫描注定
        一无所获 —— 实测整段 14.5 秒里有 13.2 秒耗在这里，其中
        `[class*="ConversationItem"]` 一个 selector 就命中 122 行、每行再乘 5 个
        标题 selector，exact 与 group 各跑一遍。而面板里当时早就写着
        「未搜索到相关内容」。

        顺带修掉一个更坏的后果：搜索态下那些残留的会话行**不可见**，可一旦被
        回退路径选中，就会以 `force=True` 点下去 —— 于是进入「点了没反应，再等满
        15 秒 `_confirm_opened`」的路径。所以那条回退路径不只是慢，它还会误命中。

        用一次 JS 判断「有没有任何一个可见的 SearchPanel 元素」，而不是在 Python
        侧逐个 selector 往返：面板的 class 在 DOM 里有很多层，Python 侧的
        `.first` 可能正好命中隐藏的那一层。
        """
        try:
            return bool(
                await self.page.evaluate(_SEARCH_PANEL_VISIBLE_JS, list(SEARCH_PANEL_MARKERS))
            )
        except Exception:
            # 页面正在导航/关闭时 evaluate 会抛错。当作「面板不在」，让调用方走
            # 回退路径 —— 那一路给出的失败信息更具体。
            return False

    async def leave_search_mode(self, timeout_ms: int = 1_500) -> bool:
        """退出搜索态（点搜索框里的「取消」）。返回是否成功。

        搜索是**有状态的**：搜完不退出，SearchPanel 就一直挂着；而它一旦挂上，
        左侧会话列表的 DOM 就被换成搜索相关的节点 —— 实测（2026-09-25）此时
        `conversationConversationItem` 命中 55 个元素且**全部不可见**，读出来的
        「名字」长度是 12/16/11 这种明显不是昵称的杂串，条数也从干净态的 64 掉到
        44。换句话说：留着搜索态，就会把一份错名单交给手表。

        2026-09-25 逐个实测过五种轻量做法，**全都退不出来**（观察 1.2~1.5 秒后
        面板照旧挂着）：清空搜索框、按 Escape、按 Escape 三次、按 Enter、点页面
        空白处；`page.go_back()` 则会直接离开 /chat。只有抖音自己的「取消」按钮
        管用，而且只要 0.14 秒 —— 对比重新加载整个私信页要 16.9 秒。所以这里点它。

        点不动也**不要**在这里兜底重载：调用方（`_open_target_once` 的失败分支）
        手上还有更重要的原始异常，重载的几秒会把它顶掉；读列表那条路另有兜底
        （见 `bridge/session.py::_ensure_out_of_search`）。
        """
        for selector in SEARCH_CANCEL_BUTTONS:
            locator = self.page.locator(selector).first
            try:
                if not await locator.count() or not await locator.is_visible():
                    continue
            except Exception:
                continue
            for force in (False, True):
                try:
                    await locator.click(timeout=timeout_ms, force=force)
                    return True
                except Exception:
                    continue
        return False

    async def _match_in_search_panel(self, name: str) -> Locator | None:
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

        return None

    async def _match_in_conversation_rows(self, name: str) -> Locator | None:
        """回退路径：搜索没生效时（页面还停在会话列表）直接在会话行里找。

        只有 `search_mode_active()` 为假时才会走到这里 —— 见它的说明：搜索面板
        一旦挂上，会话列表就被盖住，扫它既慢又会误命中不可见的残留行。
        """
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
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout / 1000
        # 宽限期：点下去之后抖音要几百毫秒才收起搜索面板、把聊天面板挂上，所以
        # 宽限期内不拿「面板还在」下结论。注意 `fastopen` 传的是 2 秒预算，比宽限
        # 期还短，对它来说这一条不生效、行为与从前一致。
        grace_until = min(deadline, loop.time() + PANEL_CLOSE_GRACE_MS / 1000)
        while True:
            last_error = await self._chat_open_error(name)
            if last_error is None:
                return
            now = loop.time()
            # 搜索面板还挂着 = 那一下点击没生效（多半点到了搜索态下不可见的残留
            # 行），页面不会自己变好。继续等只是把「点了没反应」拖成「手表转圈到
            # 超时」—— 实测会完整耗掉 15 秒预算才抛错。
            if now >= grace_until and await self.search_mode_active():
                raise last_error
            if now >= deadline:
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
        actual = await locator.inner_text(timeout=500)
        # 两边都先收成「名字」（各取第一行，见 `app.names`）：网页上昵称和会话时间
        # 常挤在同一个块级容器里，`innerText` 会在两者之间插一个换行
        # （"昵称\n7.25"、"somebody\n前天"），而名单里存的是收干净的名字 ——
        # 只比整段就会因为多出来的时间那行而失配。
        # 再统一去掉所有空白字符，免得空格 / 换行的差异也算不相等。
        # 放宽的只有「尾部多了时间」这一类：名字本身仍须逐字相同，所以
        # "test" 依旧匹配不上 "test1"。
        actual_key = _strip_all_whitespace(normalize_name(actual))
        expected_key = _strip_all_whitespace(normalize_name(expected))
        return actual_key == expected_key
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
