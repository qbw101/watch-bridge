"""读取抖音私信页消息列表。

DOM 结构（2026-09 实测，见 artifacts/probe_chat.txt）：
- 列表容器: [class*="messageMessageListlist"]
- 单条消息: 容器内 [data-index]
- 顺序: DOM 是「新 → 旧」（容器 column-reverse），data-index="0" = 最新一条
- 收发方向: 该条内是否存在 [class*="messageMessageBoxisFromMe"]（在 contentBox 上）
- 文字: [class*="TextMessageTextpureText"]
- 原生表情: [class*="MessageItemEmojiemojiBox"]
- 图片: [class*="commonMyImageimgReal"]
- 时间分隔: [class*="MessageBoxTimetimeLayout"]
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable

from playwright.async_api import Page

from app.names import normalize_name


MESSAGE_LIST_SELECTOR = '[class*="messageMessageListlist"]'

MESSAGE_LIST_STATE_JS = """() => {
  const root = document.querySelector('[class*="messageMessageListlist"]');
  if (!root) return {list: false, count: 0};
  return {list: true, count: root.querySelectorAll('[data-index]').length};
}"""


async def wait_for_message_list(page: Page, timeout_ms: int = 3_000, interval_ms: int = 150) -> bool:
    """等消息列表挂载并稳定，替代切换会话后的固定 2 秒等待。

    判定“稳定”＝连续两次采样到相同的消息节点数量。只看「列表存在」是不够的：
    刚点开会话的瞬间，DOM 里可能还残留上一个会话的消息，只有数量稳定下来才
    认为是新会话渲染完成。返回是否在超时前判定稳定。
    """
    deadline = time.monotonic() + timeout_ms / 1000
    previous = -1
    stable = False
    while True:
        try:
            state = await page.evaluate(MESSAGE_LIST_STATE_JS)
        except Exception:
            state = None
        if state and state.get("list"):
            count = int(state.get("count") or 0)
            if count > 0 and count == previous:
                stable = True
                break
            previous = count
        if time.monotonic() >= deadline:
            break
        await page.wait_for_timeout(interval_ms)
    if stable:
        # 稳定后再留一点余量，让头像/表情图等元素挂载完成。
        await page.wait_for_timeout(150)
    return stable


# 「左侧会话列表里有几个**可见**的会话行」。判据必须是可见性，不能只 count：
# 页面停在搜索态时，`conversationConversationItem` 照样能数到几十个元素，但它们
# 全部不可见（2026-09-25 实测：55 个命中、0 个可见）—— 那些是搜索面板相关的节点。
CONVERSATION_VISIBLE_ROWS_JS = """() => {
  const rows = document.querySelectorAll('[class*="conversationConversationItem"]');
  let visible = 0;
  for (const el of rows) {
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    visible++;
  }
  return visible;
}"""


async def wait_for_conversation_list(
    page: Page, timeout_ms: int = 8_000, interval_ms: int = 200
) -> bool:
    """等左侧会话列表渲染出来并**可见**（重新加载私信页之后用）。

    `open_private_messages` 保证的只是「搜索框挂上了」（那是登录判据），会话列表是
    之后异步渲染的。不等就立刻读会读到 0 条 —— 2026-09-25 实测：重载私信页后立即
    调 `read_conversations` 返回 0。

    返回是否在超时前看到可见的会话行。
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        try:
            visible = int(await page.evaluate(CONVERSATION_VISIBLE_ROWS_JS) or 0)
        except Exception:
            visible = 0
        if visible:
            return True
        if time.monotonic() >= deadline:
            return False
        await page.wait_for_timeout(interval_ms)


READ_MESSAGES_JS = """(limit) => {
  const root = document.querySelector('[class*="messageMessageListlist"]');
  if (!root) return {found: false, items: [], total: 0};

  // 关键：抖音私信列表是 column-reverse 渲染的，DOM 顺序为「新 → 旧」，
  // data-index="0" 恰恰是**最新一条**（app/sender.py 的 LATEST_OUTGOING_MESSAGE
  // 就是按 [data-index="0"] 取最近一条已发送消息来确认发送结果的）。
  //
  // 所以必须按 data-index 升序取前 limit 条（= 最新 limit 条），再反转成时间顺序。
  // 之前的 slice(-limit) 取的是 DOM 末尾 = 最老的 limit 条，导致：
  //   1) 会话超过 limit 条后，新消息完全读不到；
  //   2) 顺序颠倒，最新消息被渲染到列表顶部，而页面滚动到底部，
  //      于是用户永远看不到新发的消息。
  const nodes = Array.from(root.querySelectorAll('[data-index]'));
  nodes.sort((a, b) => {
    const left = parseInt(a.getAttribute('data-index'), 10);
    const right = parseInt(b.getAttribute('data-index'), 10);
    if (Number.isNaN(left) || Number.isNaN(right)) return 0;
    return left - right;
  });
  const slice = nodes.slice(0, limit);
  slice.reverse();

  const items = slice.map((el) => {
    const fromMe = !!el.querySelector('[class*="messageMessageBoxisFromMe"]');
    const timeEl = el.querySelector('[class*="MessageBoxTimetimeLayout"]');
    const time = timeEl ? (timeEl.innerText || '').trim() : '';
    const pure = el.querySelector('[class*="TextMessageTextpureText"]');
    const emoji = el.querySelector('[class*="MessageItemEmojiemojiBox"]');
    const image = el.querySelector('[class*="commonMyImageimgReal"]');
    const img = (emoji || image) ? (emoji || image).querySelector('img') : null;

    let type = 'unknown';
    let text = '';
    if (pure) {
      type = 'text';
      text = (pure.innerText || '').trim();
    } else if (emoji) {
      type = 'sticker';
      text = (img && img.getAttribute('alt')) ? img.getAttribute('alt') : '表情';
    } else if (image) {
      type = 'image';
      text = '图片';
    } else if (time) {
      type = 'time';
      text = time;
    }

    let media = null;
    if (img) {
      media = img.getAttribute('src') || img.getAttribute('data-src') || null;
    }

    return {
      key: el.getAttribute('data-index'),
      side: type === 'time' ? 'system' : (fromMe ? 'me' : 'them'),
      type,
      text,
      time,
      media,
    };
  });

  return {found: true, items, total: nodes.length};
}"""


async def read_messages(page: Page, limit: int = 30) -> dict[str, Any]:
    """读取当前已打开的会话里最近 limit 条消息。

    返回 {"found": bool, "total": int, "messages": [...]}；
    其中 messages 已剔除无法识别的空节点，并把时间分隔转成 type=time 的条目。
    """
    raw = await page.evaluate(READ_MESSAGES_JS, limit)
    if not raw or not raw.get("found"):
        return {"found": False, "total": 0, "messages": []}

    messages = []
    for item in raw.get("items", []):
        item_type = item.get("type")
        if item_type == "unknown":
            continue
        text = (item.get("text") or "").strip()
        if item_type != "time" and not text:
            continue
        media = item.get("media")
        messages.append(
            {
                "key": item.get("key"),
                "side": item.get("side"),
                "type": item_type,
                "text": text,
                "time": (item.get("time") or "").strip(),
                "media": media if isinstance(media, str) and media.startswith("http") else None,
            }
        )

    return {"found": True, "total": int(raw.get("total") or 0), "messages": messages}


async def read_conversations(page: Page, limit: int = 30) -> list[dict[str, Any]]:
    """读取左侧会话列表（昵称 + 最后一条消息预览）。

    抖音会话列表的 class 命名在不同版本有差异，这里做多 selector 兜底；
    读不到时返回空列表，由调用方回退到配置文件里的好友清单。

    名字在 Python 这一侧收（`app.names.normalize_name`），JS 那边不做：那个
    `titleEl.innerText` 会把昵称和右侧的时间拼成一段（块级子元素之间自动插换行），
    整段当名字就会得到 `"某位好友\n前天"` 这种脏数据。
    """
    raw = await page.evaluate(
        """(limit) => {
          const selectors = [
            '[class*="conversationConversationItem"]',
            '[data-e2e="conversation-item"]',
            '[class*="conversation-item"]',
          ];
          const visible = (el) => {
            const rect = el.getBoundingClientRect();
            if (rect.width <= 0 || rect.height <= 0) return false;
            const style = getComputedStyle(el);
            return style.display !== 'none' && style.visibility !== 'hidden';
          };
          for (const selector of selectors) {
            const all = Array.from(document.querySelectorAll(selector));
            if (!all.length) continue;
            // 只认**可见**的行。判据用 `all.length`（这个 selector 在页面上存不存在）
            // 决定要不要换下一个，用 `rows`（可见的那些）当数据 —— 两者不能混。
            //
            // 为什么非要卡可见性：页面停在搜索态时，同一个 selector 照样命中几十个
            // 元素，但它们**全部不可见**（2026-09-25 实测：55 个命中、0 个可见），
            // 读出来的「名字」是长度 12/16/11 的杂串 —— 那是搜索面板里的节点，不是
            // 会话行。不清掉这一层，手表就会拿到一份错名单。
            // 正常态下没有这个风险：实测 80 行命中、80 行全可见。
            const rows = all.filter(visible);
            return rows.slice(0, limit).map((row) => {
              const titleEl = row.querySelector('[class*="conversationConversationItemtitle"]')
                || row.querySelector('[class*="ConversationItemtitle"]')
                || row.querySelector('[class*="conversation-item-title"]');
              const text = (row.innerText || '').trim().split('\\n').filter(Boolean);
              return {
                name: titleEl ? (titleEl.innerText || '').trim() : (text[0] || ''),
                preview: text.slice(1).join(' ').slice(0, 60),
              };
            }).filter((item) => item.name);
          }
          return [];
        }""",
        limit,
    )
    return _clean_items(raw)


def _clean_items(raw: Any) -> list[dict[str, Any]]:
    """把名字收干净（`app.names.normalize_name`），其余字段原样带过。

    空名字的条目这里不滤 —— JS 那边 `.filter((item) => item.name)` 已经滤过一遍，
    `read_all_conversations` 也不收空名字。这一层只管「名字干不干净」。
    """
    if not isinstance(raw, list):
        return []
    items: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        items.append(
            {
                "name": normalize_name(item.get("name")),
                "preview": str(item.get("preview") or ""),
            }
        )
    return items


# 把会话列表往下滚一屏。抖音的 class 名一版一变，所以这里不按名字找滚动容器，
# 而是从会话行往上找第一个「真的能滚」的祖先（overflow 是 auto/scroll 且内容超高）。
SCROLL_CONVERSATIONS_JS = """() => {
  const selectors = [
    '[class*="conversationConversationItem"]',
    '[data-e2e="conversation-item"]',
    '[class*="conversation-item"]',
  ];
  let row = null;
  for (const selector of selectors) {
    row = document.querySelector(selector);
    if (row) { break; }
  }
  if (!row) { return {moved: false, reason: 'no-row'}; }
  let node = row.parentElement;
  while (node && node !== document.documentElement) {
    const style = getComputedStyle(node);
    const scrollable = style.overflowY === 'auto' || style.overflowY === 'scroll';
    if (scrollable && node.scrollHeight > node.clientHeight + 4) {
      const before = node.scrollTop;
      node.scrollTop = node.scrollHeight;
      const atEnd = node.scrollTop + node.clientHeight >= node.scrollHeight - 4;
      return {
        moved: node.scrollTop > before,
        atEnd: atEnd,
        count: document.querySelectorAll(selectors[0]).length,
        reason: 'ok',
      };
    }
    node = node.parentElement;
  }
  return {moved: false, reason: 'no-scroller'};
}"""


async def read_all_conversations(
    page: Page,
    limit: int = 200,
    *,
    max_rounds: int = 40,
    settle_ms: int = 450,
    should_stop: Callable[[], bool] | None = None,
    log: Callable[[str], None] | None = None,
) -> list[dict[str, Any]]:
    """把左侧会话列表滚到底，尽量把好友全捞出来（上限 `limit` 位）。

    为什么要**边滚边收集**、而不是滚完再读一次：会话列表很可能用了虚拟滚动
    （滚过去的行会被 DOM 回收），滚到最后 DOM 里只剩末尾那几行。所以每一轮都把
    当轮读到的名字并进结果，按名字去重 —— 顺序就是「最近聊过的排前面」。

    停止条件（任一）：
      - 收满 `limit` 位；
      - 滚不动了（到底 / 压根没找到滚动容器）；
      - 连着 3 轮没有新名字（虚拟列表给出重复内容时的兜底）；
      - `should_stop()` 返回真（页面上的「取消」）。
    """

    def note(text: str) -> None:
        if log is not None:
            log(text)

    seen: dict[str, dict[str, Any]] = {}
    stale = 0
    for _ in range(max_rounds):
        if should_stop is not None and should_stop():
            note(f"读会话列表被取消，已收集 {len(seen)} 位")
            break
        added = 0
        for item in await read_conversations(page, limit=limit):
            name = normalize_name(item.get("name"))
            if not name or name in seen:
                continue
            seen[name] = {"name": name, "preview": str(item.get("preview") or "")}
            added += 1
        if len(seen) >= limit:
            note(f"读会话列表到上限 {limit} 位，停在这里")
            break

        moved = await page.evaluate(SCROLL_CONVERSATIONS_JS)
        moved = moved if isinstance(moved, dict) else {}
        if not moved.get("moved"):
            if moved.get("atEnd") or moved.get("reason") == "no-scroller":
                note(f"会话列表已经加载完了（共 {len(seen)} 位）")
                break
            stale += 1
        else:
            stale = 0 if added else stale + 1
        if stale >= 3:
            note(f"滚了 3 轮没有新会话，就当到底了（共 {len(seen)} 位）")
            break
        await asyncio.sleep(settle_ms / 1000)
    return list(seen.values())
