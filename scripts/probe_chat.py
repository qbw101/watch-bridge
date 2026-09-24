"""探测抖音私信页消息列表的 DOM 结构，用于确定「读取消息」的 selector。

用法：
    python scripts/probe_chat.py "好友昵称" [更多好友...]

输出：把每个好友会话的消息列表结构 dump 到 artifacts/probe_chat.txt
（只读操作，不会发送任何消息）
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.browser import open_douyin, open_private_messages, verify_login
from app.config import load_settings
from app.douyin import DouyinChat


# 在页面里采集消息列表结构：不修改页面，只读取 class 与文本
_DUMP_JS = """(limit) => {
  const root = document.querySelector('[class*="messageMessageListlist"]');
  if (!root) return {found: false};

  const items = Array.from(root.querySelectorAll('[data-index]'));
  const rows = items.slice(-limit).map((el) => {
    const box = el.querySelector('[class*="messageMessageBoxmessageBox"]') || el;
    const fromMe = box.className.includes('isFromMe');
    const content = box.querySelector('[class*="messageMessageBoxcontentBox"]');
    const texts = [];
    if (content) {
      for (const node of content.querySelectorAll('*')) {
        if (node.children.length === 0 && (node.innerText || '').trim()) {
          texts.push(node.innerText.trim());
        }
      }
    }
    return {
      index: el.getAttribute('data-index'),
      elClass: el.className,
      boxClass: box.className,
      fromMe,
      contentClass: content ? content.className : null,
      texts: texts.slice(0, 6),
      boxText: (box.innerText || '').trim().slice(0, 200),
    };
  });

  // 时间分隔 / 系统提示等非消息节点
  const classSet = new Set();
  for (const el of root.querySelectorAll('*')) {
    const c = typeof el.className === 'string' ? el.className : '';
    if (c) classSet.add(c);
  }
  return {
    found: true,
    rootClass: root.className,
    itemCount: items.length,
    rows,
    classes: Array.from(classSet).slice(0, 120),
  };
}"""


async def probe(names: list[str], limit: int = 6) -> int:
    settings = load_settings()
    out_path = Path("artifacts/probe_chat.txt")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def log(text: str) -> None:
        print(text, flush=True)
        lines.append(text)

    async with open_douyin(settings) as session:
        page = session.page
        await open_private_messages(page)
        await verify_login(page)
        chat = DouyinChat(page)

        for name in names:
            log(f"\n########## 会话: {name} ##########")
            try:
                await chat.open_target(name)
            except Exception as exc:
                log(f"打开会话失败: {type(exc).__name__}: {exc}")
                continue
            await page.wait_for_timeout(2_500)

            data = await page.evaluate(_DUMP_JS, limit)
            if not data.get("found"):
                log("未找到消息列表容器 [class*=\"messageMessageListlist\"]")
                continue

            log(f"消息列表容器 class: {data['rootClass']}")
            log(f"消息条数: {data['itemCount']}")
            for row in data["rows"]:
                log(
                    "  "
                    + json.dumps(row, ensure_ascii=False)
                )
            log("列表内出现过的 class（前 120 个）:")
            log("  " + "\n  ".join(data.get("classes", [])))

    out_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n结果已写入 {out_path}")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        print("用法: python scripts/probe_chat.py \"好友昵称\" [更多好友...]")
        raise SystemExit(2)
    raise SystemExit(asyncio.run(probe(args)))
