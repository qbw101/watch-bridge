"""往抖音会话里发东西的原子操作：一条文字、一张原生表情。

只做「发出去并确认真的发出去了」，不含任何调度或批量逻辑 —— 什么时候发、
发给谁，由调用方（`bridge/session.py` 响应手表请求）决定。
"""

from __future__ import annotations

import asyncio
import contextvars
import secrets
import time

from playwright.async_api import Locator, Page

from app.douyin import DouyinChat, PageOperationError, StickerOutOfSyncError, first_visible
from app.models import Sticker
from app.selectors import (
    MESSAGE_INPUTS,
    STICKER_BUTTONS,
    STICKER_PANELS,
    STICKER_TAB_ITEMS,
    STICKER_TABS,
    sticker_resource_key,
)


def _monotonic() -> float:
    """Monotonic clock for the send-state deadline.

    Indirected (rather than calling ``asyncio.get_running_loop().time()``
    inline) so tests can fast-forward the clock without real wall-clock waits.
    """
    return asyncio.get_running_loop().time()


# 发送耗时拆解。手表上「发一条要等好几秒」是反复出现的抱怨，而这条链路上有七八
# 个各自都可能慢的步骤（开面板、切分类栏、找项、点击、等确认、回读列表）——
# 只报一个总数等于没法判断该动哪一处。这里把每一步记下来，最后由调用方拼成
# **一行** INFO 打出去：一条消息一行日志，grep 一次就能看出瓶颈搬到哪去了。
_send_profile: contextvars.ContextVar[list[tuple[str, float]] | None] = contextvars.ContextVar(
    "send_profile", default=None
)
_last_profile: list[tuple[str, float]] = []


class _Step:
    """给一个步骤计时并记进当次发送的耗时拆解。"""

    def __init__(self, label: str) -> None:
        self.label = label

    async def __aenter__(self) -> "_Step":
        self.started = time.perf_counter()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        _note_step(self.label, time.perf_counter() - self.started)


def _note_step(label: str, seconds: float) -> None:
    profile = _send_profile.get()
    if profile is not None:
        profile.append((label, seconds))


def format_send_profile() -> str:
    """把上一次发送的耗时拆解拼成一行，形如「标记 0.05 开面板 0.42 …」。"""
    if not _last_profile:
        return ""
    return " ".join(f"{label} {seconds:.2f}s" for label, seconds in _last_profile)


def _reset_send_profile() -> None:
    profile: list[tuple[str, float]] = []
    _send_profile.set(profile)
    global _last_profile
    _last_profile = profile


# 对外只暴露这两个名字：调用方（bridge/session）不该看见下划线实现，
# 也不该自己拼计时（否则「重置」和「记一步」的顺序迟早会在某一处写反）。
send_step = _Step
reset_send_profile = _reset_send_profile


SEND_BUTTONS = (
    '[class*="messageMsgInputpublishBtn"]',
    '.e2e-send-msg-bt',
    'button[aria-label*="发送"]',
    '[role="button"][aria-label*="发送"]',
)


async def _trigger_send(page: Page) -> None:
    button = None
    for selector in SEND_BUTTONS:
        candidate = page.locator(selector).first
        try:
            if await candidate.count() and await candidate.is_visible():
                button = candidate
                break
        except Exception:
            continue
    if button is not None:
        await button.click()
    else:
        await page.keyboard.press("Enter")


async def _publish_ready(page: Page) -> bool:
    for selector in SEND_BUTTONS:
        candidate = page.locator(selector).first
        try:
            if await candidate.count() and await candidate.is_visible():
                return True
        except Exception:
            continue
    return False


LATEST_OUTGOING_MESSAGE = (
    '.messageMessageListlist [data-index="0"] '
    '.messageMessageBoxmessageBox:has(.messageMessageBoxcontentBox.messageMessageBoxisFromMe)'
)
MESSAGE_CONFIRM_ANCHOR = "data-douyin-sender-anchor"

# Send-status markers, scoped to the single outgoing message being confirmed.
#
# Douyin renders an outgoing bubble *before* its send status is resolved: the
# bubble appears, a spinner sits beside it, and only later does it clear
# (success) or flip to a retry marker. We must wait for a terminal state
# rather than assume a visible bubble means success (Issue #11).
#
# Failure: the red `!` retry control. `ContentSideSendStatusretry` is the real
# Douyin class from Issue #11; `SendStatusretry` is a stable fallback. The bare
# `SendStatusicon` class is intentionally excluded -- it is shared by other
# send states and would cause false failures.
SEND_FAILURE_MARKERS = (
    "text=发送失败",
    '[aria-label*="重试"]',
    '[title*="重试"]',
    '[class*="sendFailed"]',
    '[class*="SendFailed"]',
    '[class*="ContentSideSendStatusretry"]',
    '[class*="SendStatusretry"]',
)
# Pending: the in-flight spinner beside the bubble. These are checked only on
# the scoped outgoing message, never page-wide, so unrelated loading spinners
# cannot produce a false pending state.
SEND_PENDING_MARKERS = (
    ".semi-spin",
    '[class*="im-saas-message-spin"]',
    '[data-icon="spin"]',
)
SEND_RETRY_MARKERS = (
    '[aria-label*="重试"]',
    '[title*="重试"]',
    '[class*="ContentSideSendStatusretry"]',
    '[class*="SendStatusretry"]',
)

# Overall budget for confirming a single message reaches a terminal state. A
# stuck spinner past this is treated as failure/uncertain, never success.
SEND_CONFIRM_TIMEOUT_MS = 15_000
# Poll interval for re-checking pending/failure state.
#
# 150ms 而不是 300ms：这一轮轮询是「查一次标记」的本地 CDP 往返 —— 我们
# 新写的那条批量 JS 查询把一整组标记压成一次往返（见 diagnose_send_path），
# 单轮成本已经降到几毫秒，所以可以查得更密。密一点的价值在于：spinner 一出现
# 就能立刻进第 2 阶段，而不是最多白等 300ms 才发现它。
SEND_POLL_INTERVAL_MS = 150
# Short grace after pending clears: the retry marker can mount a tick after the
# spinner disappears, so require a stable non-pending frame before success.
SEND_STABLE_INTERVAL_MS = 500
# Initial-clean grace window. A freshly matched bubble with no spinner and no
# failure marker is NOT yet success -- Douyin mounts the spinner/retry *after*
# the bubble. We must keep observing until either a state appears or the whole
# grace window stays clean (Issue #11 E2E regression: late-mounting retry was
# missed because the old single 500ms check ended before the retry mounted).
# This is a continuous observation window, NOT a single sleep: the loop polls
# every SEND_POLL_INTERVAL_MS throughout it.
#
# 1600ms（原 2000ms）：这一段是「干净通过」那一路的固定成本，也就是每一次
# 顺利发送都要付的等待 —— 手表上就是「发送中…」多挂 2 秒。窗口本身必须留着
# （理由见上），但可以配着更密的轮询收窄一点：窗口内现在是 10 次检查而不是 6 次，
# 检测粒度反而比原来更细。1600ms 相对历史上出问题的「单次 500ms 检查」仍有
# 3 倍余量。再往下压就要面对「把失败当成功」的风险了 —— 那比慢几秒糟得多。
SEND_INITIAL_CLEAN_GRACE_MS = 1_600


async def send_text(chat: DouyinChat, content: str) -> None:
    editor = await chat.message_input()
    page = editor.page
    await editor.click()
    await page.keyboard.insert_text(content)
    try:
        await page.wait_for_function(
            """([txt]) => {
                const es = [...document.querySelectorAll('[class*=messageEditor] [contenteditable=true], .messageEditorinputArea')];
                return es.some(e => (e.innerText || '').includes(txt));
            }""",
            arg=[content],
            timeout=5_000,
        )
    except Exception as exc:
        raise PageOperationError("文字未能写入聊天输入框") from exc

    before = await _mark_latest_outgoing_message(page)
    await page.wait_for_timeout(300)
    await _trigger_send(page)
    await _confirm_outgoing_message(page, before, label="文字", expected_text=content)


async def _restore_composer(page: Page, timeout_ms: int = 10_000) -> None:
    """把输入框找回来（关掉面板、焦点交回输入区），为下一次操作做准备。

    三步各自计时：这一步实测要 4.3 秒、占掉整条发送链路六成多，而它做的是
    「按 Esc、找输入框、点一下」这种本该几十毫秒的事 —— 不拆开就永远不知道
    该动哪一步。
    """
    async with _Step("Esc"):
        try:
            await page.keyboard.press("Escape")
        except Exception:
            pass
    async with _Step("找输入框"):
        try:
            editor = await first_visible(page, MESSAGE_INPUTS, timeout_ms)
        except Exception:
            return
    async with _Step("点输入框"):
        try:
            await editor.click(timeout=timeout_ms)
            await editor.focus()
        except Exception:
            pass


# 按资源名在面板里找「第几个」。自建/收藏表情没有名字，但每张图有稳定的资源名
# （`app/selectors.sticker_resource_key`），认图比认序号可靠得多：
# 用户一收藏新表情，后面所有项的序号就整体顺移，缓存的 fallback_index 于是指向
# 邻居 —— 「预览是 A、发出去是 B」就是这么来的。图本身不会顺移。
#
# 归一化规则必须和 `app/selectors.sticker_resource_key` 完全一致：只取 path 最后
# 一段、截到 `~` 为止，问号后面的签名参数不算。懒加载的项 `src` 可能为空，
# 所以要一并看 `data-src`（扫描端也是这么读的）。
FIND_STICKER_INDEX_JS = """([selector, resourceKey]) => {
  const normalize = (raw) => {
    if (!raw) return '';
    const path = String(raw).split('?')[0].split('#')[0];
    const last = path.substring(path.lastIndexOf('/') + 1);
    return last.split('~')[0].trim();
  };
  const nodes = Array.from(document.querySelectorAll(selector));
  for (let index = 0; index < nodes.length; index += 1) {
    const node = nodes[index];
    const img = node.tagName === 'IMG' ? node : node.querySelector('img');
    if (!img) continue;
    const src = img.getAttribute('src') || img.getAttribute('data-src') || img.src || '';
    if (normalize(src) === resourceKey) return index;
  }
  return -1;
}"""


async def _find_sticker_index_by_resource(page: Page, resource_key: str) -> int:
    """按资源名在**当前已打开的分类栏**里找表情项的序号；找不到返回 -1。"""
    if not resource_key:
        return -1
    try:
        raw = await page.evaluate(FIND_STICKER_INDEX_JS, [STICKER_TAB_ITEMS, resource_key])
    except Exception:
        return -1
    try:
        return int(raw)
    except (TypeError, ValueError):
        return -1


async def _open_sticker_tab(page: Page, index: int) -> bool:
    """点开表情面板底部的第 index 个分类栏。

    分类栏只是切页、不会发消息 —— `scripts/scan_stickers.py --click-tabs` 实测过：
    点分类栏前后聊天区的消息框数量不变。切完等它变成选中态再返回，
    否则紧接着的定位会落在上一栏的内容上（点中的就是另一个表情了）。
    """
    tabs = page.locator(STICKER_TABS)
    if await tabs.count() <= index:
        return False
    tab = tabs.nth(index)
    cls = (await tab.get_attribute("class")) or ""
    if "disabled" in cls:
        return False
    await tab.click(force=True)
    for _ in range(12):
        await page.wait_for_timeout(120)
        cls = (await tab.get_attribute("class")) or ""
        if "active" in cls.lower():
            return True
    return True


async def send_douyin_sticker(page: Page, sticker: Sticker) -> None:
    async with _Step("标记"):
        before = await _mark_latest_outgoing_message(page)
    try:
        async with _Step("开面板"):
            button = await first_visible(page, STICKER_BUTTONS)
            await button.click(force=True)
            panel = await first_visible(page, STICKER_PANELS)
        name = sticker.accessible_name or sticker.name

        # 按分类序号定位。面板底部的分类栏是纯图标（没有文字、也没有 aria-label），
        # 而自建/收藏表情既没有名字也没有 aria-label —— 所以「第几栏 + 该栏第几个」
        # 是这两类表情唯一可靠的定位方式。名字那条路只在序号缺失时才走。
        if sticker.tab_index is not None:
            async with _Step("切分类"):
                opened = await _open_sticker_tab(page, sticker.tab_index)
            if not opened:
                raise PageOperationError(
                    f"表情面板里没有第 {sticker.tab_index} 个分类（或该分类不可用）: {sticker.name}"
                )
            items = page.locator(STICKER_TAB_ITEMS)
            # 先按资源名认图。序号会因为「收藏了一个新表情」整体顺移（新表情插在最前，
            # 后面所有项 +1），缓存的 fallback_index 就指向了邻居 —— 预览是 A、发出去
            # 是 B。资源名认的是图本身，顺序怎么变都找得到。
            async with _Step("找项"):
                found = await _find_sticker_index_by_resource(page, sticker.resource_key or "")
                if found < 0 and sticker.resource_key:
                    # 面板里已经没有这张图了（被删掉 / 这一栏换了一批）。
                    # 此刻还没点下去，让上层重扫一次再试是安全的。
                    raise StickerOutOfSyncError(
                        f"表情面板里找不到「{sticker.name}」这张图，库里的记录可能已过期"
                    )
                if found < 0:
                    # 老库项（还没重扫过、没有资源名）只能退回按序号定位
                    if sticker.fallback_index is None:
                        raise PageOperationError(
                            f"表情「{sticker.name}」记了分类 {sticker.tab_index} 却没记序号，无法定位"
                        )
                    found = sticker.fallback_index
                total = await items.count()
            if total <= found:
                raise PageOperationError(
                    f"分类 {sticker.tab_index} 里只有 {total} 个表情，"
                    f"找不到第 {found} 个: {sticker.name}"
                )
            target = items.nth(found)
            # 面板一屏放不下（自建表情有几十个），排在后面的项在可视区外。
            # `click(force=True)` 跳过可操作性检查、也不会自己滚动，
            # 不先滚进可视区就可能把点击落在别的东西上 —— 那不是「发错了表情」，
            # 而是「在面板外面点了一下」。所以这一步不能省。
            async with _Step("滚动"):
                await _scroll_sticker_into_view(page, found)
            async with _Step("点击确认"):
                await _click_and_confirm_sticker(page, target, before, name)
            return

        if sticker.category:
            category = panel.get_by_text(sticker.category, exact=True)
            if await category.count() and await category.first.is_visible():
                await category.first.click()

        item = panel.locator('.emojiEmojiItememojiItem').filter(has_text=name)
        for index in range(await item.count()):
            candidate = item.nth(index)
            description = candidate.locator('.emojiEmojiItememojiItemDesc')
            if await description.count() and (await description.first.inner_text()).strip() == name:
                await _click_and_confirm_sticker(page, candidate, before, name)
                return

        candidates = (
            panel.get_by_role("img", name=name, exact=True),
            panel.get_by_role("button", name=name, exact=True),
            panel.locator(f'[aria-label="{_css_escape(name)}"]'),
            panel.locator(f'[title="{_css_escape(name)}"]'),
            panel.locator(f'[alt="{_css_escape(name)}"]'),
        )
        for candidate in candidates:
            if await candidate.count() and await candidate.first.is_visible():
                await _click_and_confirm_sticker(page, candidate.first, before, name)
                return

        if sticker.fallback_index is not None:
            items = panel.locator('[role="button"], img, [aria-label], [title]')
            if await items.count() > sticker.fallback_index:
                await _click_and_confirm_sticker(page, items.nth(sticker.fallback_index), before, name)
                return
        raise PageOperationError(f"在抖音表情面板中找不到原生表情: {sticker.name}")
    finally:
        async with _Step("收输入框"):
            await _restore_composer(page)


def _css_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


async def _mark_latest_outgoing_message(page: Page) -> tuple[str, str]:
    anchor = secrets.token_hex(8)
    latest = page.locator(LATEST_OUTGOING_MESSAGE).first
    if not await latest.count():
        return anchor, ""

    content = latest.locator('[data-e2e="msg-item-content"]').first
    before_content = await content.inner_html() if await content.count() else await latest.inner_html()
    await latest.evaluate(
        "(element, value) => element.setAttribute('data-douyin-sender-anchor', value)",
        anchor,
    )
    return anchor, before_content


async def _scroll_sticker_into_view(page: Page, index: int) -> None:
    """把第 index 个表情项滚进可视区。

    Playwright 的 `scroll_into_view_if_needed` 会分步滚动、每步都等布局稳定，
    面板里几十项时实测要几百毫秒；这里一次 `scrollIntoView(block:'nearest')` 就够了 ——
    紧接着是 `click(force=True)`，只要目标在可视区里就能点中。
    """
    try:
        await page.evaluate(
            """([selector, index]) => {
              const node = document.querySelectorAll(selector)[index];
              if (node) node.scrollIntoView({block: 'nearest', inline: 'nearest'});
            }""",
            [STICKER_TAB_ITEMS, index],
        )
    except Exception:
        pass


async def _click_and_confirm_sticker(page: Page, item, before: tuple[str, str], name: str) -> None:
    async with _Step("取资源名"):
        resource_key = await _sticker_resource_key(item)
    async with _Step("点下去"):
        await item.click(force=True)
    try:
        async with _Step("等确认"):
            await _confirm_sticker_sent(page, before, name, resource_key)
    except PageOperationError as exc:
        if "页面提示可以重试" in str(exc) and await _click_retry_on_latest_failed_message(page):
            async with _Step("重试确认"):
                await _confirm_sticker_sent(page, before, name, resource_key)
            return
        if await _publish_ready(page):
            async with _Step("按发送键"):
                await _trigger_send(page)
            async with _Step("重试确认"):
                await _confirm_sticker_sent(page, before, name, resource_key)
        else:
            raise


async def _sticker_resource_key(item) -> str:
    """读取某个表情项的资源名（与库里存的 `source_key` 同一套规则）。

    走 `sticker_resource_key` 而不是就地取 URL 末段：那个规则要截掉 `~` 之后的
    图片处理模板，两处各写一遍迟早分叉，而这里返回值是拿去和**发出去那条消息**
    的图做比对的 —— 规则一旦不一致，就会「明明发对了却报没确认到」。
    """
    src = await item.get_attribute("src")
    if not src:
        image = item.locator("img").first
        if await image.count():
            src = await image.get_attribute("src")
    return sticker_resource_key(src or "")


async def _confirm_sticker_sent(
    page: Page,
    before: tuple[str, str],
    name: str,
    resource_key: str = "",
) -> None:
    await _confirm_outgoing_message(page, before, f"原生表情“{name}”", resource_key=resource_key)


async def _click_retry_on_latest_failed_message(page: Page) -> bool:
    latest = page.locator(LATEST_OUTGOING_MESSAGE).first
    for selector in SEND_RETRY_MARKERS:
        marker = latest.locator(selector).first
        try:
            if await marker.count() and await marker.is_visible():
                await marker.click(force=True)
                return True
        except Exception:
            continue
    return False


# One page-side pass over the whole marker list, mirroring Playwright's
# visibility rule (non-empty box, not visibility:hidden) and its `text=` engine
# (smallest element whose trimmed text contains the needle, case-insensitive).
# `querySelector` throws on Playwright-only syntax such as `text=...`, so each
# selector is guarded individually.
MARKER_BULK_JS = """(el, selectors) => {
  const isVisible = (node) => {
    if (!node) return false;
    const style = getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const rects = node.getClientRects();
    for (const rect of rects) {
      if (rect.width > 0 && rect.height > 0) return true;
    }
    return false;
  };

  for (const selector of selectors) {
    if (selector.startsWith('text=')) {
      const needle = selector.slice(5).trim().toLowerCase();
      if (!needle) continue;
      let smallest = null;
      let smallestLength = Infinity;
      for (const node of el.querySelectorAll('*')) {
        const text = (node.textContent || '').trim().toLowerCase();
        if (!text || !text.includes(needle)) continue;
        if (text.length < smallestLength) {
          smallest = node;
          smallestLength = text.length;
        }
      }
      if (isVisible(smallest)) return true;
      continue;
    }
    let node = null;
    try {
      node = el.querySelector(selector);
    } catch (error) {
      // 有 selector 在本页无法解析（例如新增了 Playwright 专有语法）：
      // 返回 unknown，让 Python 回退到逐 selector 查询，绝不能静默当作「没有」。
      return 'unknown';
    }
    if (isVisible(node)) return true;
  }
  return false;
}"""


async def _marker_visible(scope: Locator, selectors: tuple[str, ...]) -> bool:
    """True if any selector in ``selectors`` resolves to a visible element.

    Scoped to ``scope`` (the single outgoing message) so unrelated page-wide
    spinners cannot influence the verdict.

    Fast path: evaluate the whole selector list in ONE page call. The old
    per-selector loop costs two CDP round-trips per marker (``count`` +
    ``is_visible``); with 10 markers per poll and a 7-poll observation window
    that is ~140 round-trips per send, which dominated send latency (measured
    2026-09-18: a single text send took 10.1s).
    The locator loop is kept as the fallback for scopes that cannot evaluate
    (test doubles) and for the ``'unknown'`` verdict (a selector the page could
    not parse), and it is the reference semantics the fast path mirrors:
    Playwright's ``is_visible`` == non-empty box AND not ``visibility:hidden``.
    tests/test_sender_marker_visibility.py pins the equivalence.
    """
    verdict = "unknown"
    try:
        verdict = await scope.evaluate(MARKER_BULK_JS, list(selectors))
    except Exception:
        verdict = "unknown"
    if verdict is True:
        return True
    if verdict is False:
        return False
    for selector in selectors:
        marker = scope.locator(selector).first
        try:
            if await marker.count() and await marker.is_visible():
                return True
        except Exception:
            continue
    return False


async def _await_send_terminal_state(
    page: Page,
    scope: Locator,
    label: str,
    timeout_ms: int = SEND_CONFIRM_TIMEOUT_MS,
) -> None:
    """Wait for ``scope`` (one outgoing message) to reach a terminal state.

    State machine (Issue #11):

        MATCHED
           |
           v
        OBSERVING_INITIAL  (bubble matched, status not yet resolved)
           |  failure visible            -> FAILED
           |  pending visible            -> WAITING_PENDING
           |  clean for the whole grace  -> SUCCESS
           v
        WAITING_PENDING  (spinner visible)
           |  failure visible            -> FAILED
           |  pending gone              -> STABILIZING
           v
        STABILIZING  (spinner just cleared)
           |  failure visible            -> FAILED
           |  pending reappeared         -> WAITING_PENDING
           |  stable clean window held   -> SUCCESS

    Critical correctness rule: a *newly* matched bubble that is clean is treated
    as UNKNOWN/OBSERVING, never as success, because Douyin mounts the spinner or
    retry marker *after* the bubble (Issue #11 E2E regression: the old code
    declared success after a single 500ms check, before a late retry mounted).
    The initial-clean grace window therefore polls continuously; success only
    after the bubble stays clean for the full window (or pending clears + holds).

    ``timeout_ms`` is the overall budget; a stuck spinner past it raises, never
    success. ``page.wait_for_timeout`` advances the injectable monotonic clock,
    so tests run in milliseconds while simulating multi-second windows.
    """
    deadline = _monotonic() + timeout_ms / 1000

    # Phase 1: observe the freshly matched bubble. It is UNKNOWN until either a
    # state marker appears or the initial-clean grace window elapses clean.
    grace_deadline = _monotonic() + SEND_INITIAL_CLEAN_GRACE_MS / 1000
    while _monotonic() < grace_deadline:
        if _monotonic() >= deadline:
            raise PageOperationError(
                f"{label}发送状态未能确认（发送超时或状态不确定），为避免重复不会自动重试"
            )
        if await _marker_visible(scope, SEND_FAILURE_MARKERS):
            raise PageOperationError(f"{label}发送失败，页面提示可以重试")
        if await _marker_visible(scope, SEND_PENDING_MARKERS):
            break  # -> resolve pending in Phase 2
        await page.wait_for_timeout(SEND_POLL_INTERVAL_MS)
    else:
        # Grace window elapsed fully clean -> fast success (normal fast send).
        return

    # Phase 2: a spinner appeared. Wait for it to clear (or flip to failure),
    # then require a stable clean window before success.
    while True:
        if _monotonic() >= deadline:
            raise PageOperationError(
                f"{label}发送状态未能确认（发送超时或状态不确定），为避免重复不会自动重试"
            )
        if await _marker_visible(scope, SEND_FAILURE_MARKERS):
            raise PageOperationError(f"{label}发送失败，页面提示可以重试")
        if not await _marker_visible(scope, SEND_PENDING_MARKERS):
            # Spinner gone. Require it to STAY clear across the stable window --
            # the retry marker can mount a tick after the spinner disappears.
            await page.wait_for_timeout(SEND_STABLE_INTERVAL_MS)
            if await _marker_visible(scope, SEND_FAILURE_MARKERS):
                raise PageOperationError(f"{label}发送失败，页面提示可以重试")
            if not await _marker_visible(scope, SEND_PENDING_MARKERS):
                return  # terminal: success
            # spinner reappeared -> keep waiting
        await page.wait_for_timeout(SEND_POLL_INTERVAL_MS)


async def _confirm_outgoing_message(
    page: Page,
    before: tuple[str, str],
    label: str,
    resource_key: str = "",
    expected_text: str = "",
) -> None:
    anchor, before_content = before
    try:
        await page.wait_for_function(
            """([selector, anchor, previousContent, expectedResource, expectedText]) => {
                const message = document.querySelector(selector);
                if (!message) return false;
                const content = message.querySelector('[data-e2e="msg-item-content"]') || message;
                const isNewMessage =
                    message.getAttribute('data-douyin-sender-anchor') !== anchor ||
                    content.innerHTML !== previousContent;
                if (!isNewMessage) return false;
                if (expectedText) {
                    const normalize = value => (value || '').replace(/[\\s\\u200B\\u200C\\u200D\\uFEFF]+/g, ' ').trim();
                    return normalize(content.innerText).includes(normalize(expectedText));
                }
                if (!expectedResource) return true;
                const images = [...content.querySelectorAll('img')];
                return images.some(image => (image.src || '').includes(expectedResource)) || images.length > 0;
            }""",
            arg=[LATEST_OUTGOING_MESSAGE, anchor, before_content, resource_key, expected_text],
            timeout=15_000,
        )
        # The bubble now matches our payload, but the send may still be in
        # flight or have already failed. Wait for a real terminal state rather
        # than treating a visible bubble as success (Issue #11).
        latest = page.locator(LATEST_OUTGOING_MESSAGE).first
        await _await_send_terminal_state(page, latest, label)
    except PageOperationError:
        raise
    except Exception as exc:
        raise PageOperationError(f"{label}已发送，但没有检测到新的已发送消息") from exc
    finally:
        anchors = page.locator(f"[{MESSAGE_CONFIRM_ANCHOR}]")
        try:
            await anchors.evaluate_all(
                "elements => elements.forEach(element => element.removeAttribute('data-douyin-sender-anchor'))"
            )
        except Exception:
            pass
