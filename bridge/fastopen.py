"""会话快速切换：优先点击左侧会话列表，失败回退抖音原生搜索。

背景（2026-09-18 实测）：`DouyinChat.open_target` 每次切换好友都要在搜索框里
输入、固定等待 1.5 秒、再逐行扫描搜索结果，单次 5-7 秒。而抖音私信页左侧的
会话列表本身已经渲染了最近联系人，直接点那一行通常 1 秒内就能完成切换。

这里只做“尝试”：任何一步不符合预期就返回 False，由调用方回退到
`open_target`，所以最坏情况只多花一点点时间，不会引入新的失败模式。
"""

from __future__ import annotations

import logging

from app.douyin import DouyinChat

LOGGER = logging.getLogger("douyin_watch")

ROW_SELECTOR = '[class*="conversationConversationItem"]'
MARK_ATTRIBUTE = "data-watch-fastopen"

TITLE_SELECTORS = (
    '[class*="conversationConversationItemtitle"]',
    '[class*="ConversationItemtitle"]',
    '[class*="ConversationItemTitle"]',
    '[class*="conversation-item-title"]',
    '[class*="conversation-item-Title"]',
)

# 会话行的高度通常在 70-100px；限制高度是为了排除「包住所有行的外层容器」。
# 抖音会把同一个 class 前缀同时挂在外层容器和真正的行上，若不加限制，
# 点外层容器会落到它中心位置的另一行上，可能点错好友。
MAX_ROW_HEIGHT = 180
MIN_ROW_WIDTH = 120

# 一次 JS 调用完成「归一化名字 → 挑选最小且可见的行 → 打标记」，
# 避免在 Python 侧对每一行做多次 locator 往返（那正是原路径慢的原因）。
MARK_ROW_JS = """([rowSelector, titleSelectors, name, maxHeight, minWidth]) => {
  const normalize = (value) => (value || '')
    .replace(/[\\s\\u200B\\u200C\\u200D\\uFEFF]+/g, ' ').trim();
  const wanted = normalize(name);
  if (!wanted) return {marked: false, rows: 0};

  document.querySelectorAll('[data-watch-fastopen]').forEach((el) => {
    el.removeAttribute('data-watch-fastopen');
  });

  const rows = Array.from(document.querySelectorAll(rowSelector));
  let best = null;
  for (const row of rows) {
    let matched = false;
    for (const selector of titleSelectors) {
      const node = row.querySelector(selector);
      if (!node) continue;
      if (normalize(node.textContent) === wanted) { matched = true; break; }
    }
    if (!matched) continue;
    const rect = row.getBoundingClientRect();
    if (rect.width < minWidth || rect.height < 8 || rect.height > maxHeight) continue;
    if (rect.bottom <= 0 || rect.top >= window.innerHeight) continue;
    const area = rect.width * rect.height;
    if (best === null || area < best.area) best = {row, area};
  }
  if (best === null) return {marked: false, rows: rows.length};
  best.row.setAttribute('data-watch-fastopen', '1');
  return {marked: true, rows: rows.length};
}"""

CLEAR_MARK_JS = """() => {
  document.querySelectorAll('[data-watch-fastopen]').forEach((el) => {
    el.removeAttribute('data-watch-fastopen');
  });
}"""


async def open_via_conversation_list(chat: DouyinChat, name: str, timeout_ms: int = 2_000) -> bool:
    """尝试点击左侧会话列表直接切换会话。

    返回 True 表示「已确认目标会话处于打开状态」（页头标题正确且输入框已挂载）。
    返回 False 表示调用方应回退到搜索路径。

    这里刻意不做「先看看目标会话是不是已经打开」的预检：
    那是 `DouyinChat._chat_open_error` 的一次**负向**全量扫描 —— 它要遍历每个
    页头容器 × 10 个标题 selector × 每个节点的 inner_text/is_visible，
    而切换会话时它注定是负向的，实测一次要 5-8 秒（2026-09-18 实测：加了预检
    后点列表反而要 8.8 秒，去掉后 1.5 秒）。已打开的情况由 `ensure_chat` 的
    `_current_chat` 缓存负责，不需要在页面层再查一遍。
    """
    page = chat.page

    try:
        marked = await page.evaluate(
            MARK_ROW_JS,
            [ROW_SELECTOR, list(TITLE_SELECTORS), name, MAX_ROW_HEIGHT, MIN_ROW_WIDTH],
        )
    except Exception:
        LOGGER.debug("扫描左侧会话列表失败，回退搜索", exc_info=True)
        return False
    if not marked or not marked.get("marked"):
        return False

    try:
        row = page.locator(f'[{MARK_ATTRIBUTE}="1"]').first
        if not await row.count():
            return False
        await row.click(force=True)
        await chat._confirm_opened(name, timeout_ms=timeout_ms)
    except Exception:
        LOGGER.debug("点击会话列表后未能确认会话打开，回退搜索", exc_info=True)
        return False
    finally:
        try:
            await page.evaluate(CLEAR_MARK_JS)
        except Exception:
            pass
    return True


__all__ = ["open_via_conversation_list"]
