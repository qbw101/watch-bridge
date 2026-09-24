"""量一量表情面板是不是「懒渲染」的，以及滚动能多捞出多少表情。

背景：同一个分类，两次扫描分别得到 50 项和 93 项。区别只可能是渲染时机 ——
表情网格如果只渲染可视区域，DOM 里没有的项就抓不到，库自然抓不全。

本脚本只做三件事：点分类栏（不点表情）、滚动面板、数节点数量。
点分类栏是安全的（`_MESSAGE_FINGERPRINT_JS` 守卫：聊天区消息数一变就立即中止）。

用法：
    python scripts/probe_sticker_scroll.py              # 默认量分类 3
    python scripts/probe_sticker_scroll.py --tab 2
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(SCRIPT_DIR))

from scan_stickers import (  # noqa: E402
    STICKER_BUTTONS,
    STICKER_TABS,
    AlreadyRunningError,
    DouyinChat,
    _MESSAGE_FINGERPRINT_JS,
    _panel_visible,
    first_visible,
    load_settings,
    open_douyin,
    open_private_messages,
    run_lock,
    verify_login,
)

# 找出表情项容器「最近的、真的能滚的那个祖先」，并报告滚动几何。
# 不能想当然用 panel.querySelector('.componentsemojitabPanel')：滚动容器可能是它自己，
# 也可能是再上层的壳，猜错就会「滚了但什么也没变」，白跑一轮。
_SCROLL_INFO_JS = """
() => {
  const panel = document.querySelector('.componentsemojiemojiPanel') || document.querySelector('[class*="emojiPanel"]');
  if (!panel) return {found: false};
  const content = panel.querySelector('.componentsemojitabPanel') || panel;
  const nodes = Array.from(content.querySelectorAll('.emojiEmojiItememojiItem'));
  const scrollables = [];
  let el = content;
  while (el && el !== document.documentElement) {
    const st = getComputedStyle(el);
    const canScroll = /(auto|scroll)/.test(st.overflowY);
    if (canScroll || el.scrollHeight > el.clientHeight + 4) {
      scrollables.push({
        cls: typeof el.className === 'string' ? el.className : '',
        scrollTop: Math.round(el.scrollTop),
        scrollHeight: el.scrollHeight,
        clientHeight: el.clientHeight,
        overflowY: st.overflowY,
        scrollable: canScroll,
      });
    }
    el = el.parentElement;
  }
  const first = nodes[0];
  return {
    found: true,
    contentClass: typeof content.className === 'string' ? content.className : '',
    itemCount: nodes.length,
    maxIndex: nodes.length ? nodes[nodes.length - 1].getAttribute('data-index') : null,
    firstRect: first ? [Math.round(first.getBoundingClientRect().x), Math.round(first.getBoundingClientRect().y)] : null,
    lastRect: nodes.length ? [Math.round(nodes[nodes.length - 1].getBoundingClientRect().x), Math.round(nodes[nodes.length - 1].getBoundingClientRect().y)] : null,
    panelRect: [Math.round(panel.getBoundingClientRect().x), Math.round(panel.getBoundingClientRect().y), Math.round(panel.getBoundingClientRect().width), Math.round(panel.getBoundingClientRect().height)],
    scrollables: scrollables,
  };
}
"""

# 把能滚的那个祖先滚到底。返回滚完的几何，方便判断「到底了没有」。
_SCROLL_BOTTOM_JS = """
() => {
  const panel = document.querySelector('.componentsemojiemojiPanel') || document.querySelector('[class*="emojiPanel"]');
  if (!panel) return {found: false};
  const content = panel.querySelector('.componentsemojitabPanel') || panel;
  let el = content;
  let target = null;
  while (el && el !== document.documentElement) {
    const st = getComputedStyle(el);
    if (/(auto|scroll)/.test(st.overflowY) || el.scrollHeight > el.clientHeight + 4) { target = el; break; }
    el = el.parentElement;
  }
  if (!target) return {found: true, scrolled: false};
  target.scrollTop = target.scrollHeight;
  const nodes = content.querySelectorAll('.emojiEmojiItememojiItem');
  return {
    found: true,
    scrolled: true,
    cls: typeof target.className === 'string' ? target.className : '',
    scrollTop: Math.round(target.scrollTop),
    scrollHeight: target.scrollHeight,
    clientHeight: target.clientHeight,
    atBottom: target.scrollTop + target.clientHeight >= target.scrollHeight - 2,
    itemCount: nodes.length,
  };
}
"""


async def run(args: argparse.Namespace) -> int:
    settings = load_settings()
    artifacts_dir = settings.artifacts_dir

    try:
        lock = run_lock(artifacts_dir / "run.lock")
        lock.__enter__()
    except AlreadyRunningError as exc:
        print(f"× {exc}")
        print("  请先停掉手表服务，再跑诊断。")
        return 3

    try:
        async with open_douyin(settings) as session:
            page = session.page
            await open_private_messages(page)
            await verify_login(page)

            friends = None
            friend = args.friend
            if not friend:
                try:
                    from scan_stickers import _friends_from_config

                    friends = _friends_from_config(settings.config_path)
                except Exception:  # noqa: BLE001
                    friends = None
                if not friends:
                    print("× config.json 里没有 friends，用 --friend 指定一个会话。")
                    return 2
                friend = friends[0]
            print(f"→ 打开会话「{friend}」…")
            await DouyinChat(page).open_target(friend)

            print("→ 点开表情面板…")
            button = await first_visible(page, STICKER_BUTTONS)
            await button.click(force=True)
            await page.wait_for_timeout(1500)
            if not await _panel_visible(page):
                print("× 表情面板没有出现。")
                return 1
            await page.wait_for_timeout(1000)

            tabs = page.locator(STICKER_TABS)
            total = await tabs.count()
            print(f"→ 分类栏共 {total} 个")
            if not 0 <= args.tab < total:
                print(f"× 分类 {args.tab} 超出范围。")
                return 2

            baseline = await page.evaluate(_MESSAGE_FINGERPRINT_JS)
            print(f"  消息区指纹（基准）= {baseline}")

            tab = tabs.nth(args.tab)
            cls = (await tab.get_attribute("class")) or ""
            if "disabled" in cls:
                print(f"× 分类 {args.tab} 是 disabled 状态，没有内容可量。")
                return 1
            print(f"→ 点开分类 {args.tab}（只点分类，不点表情）…")
            await tab.click(force=True)
            await page.wait_for_timeout(args.wait)

            after = await page.evaluate(_MESSAGE_FINGERPRINT_JS)
            if after != baseline:
                print(f"× 点分类后消息区变了（{baseline} → {after}），可能有表情被发出去，立即停止。")
                return 1
            print("  消息区未变化，安全。")

            info = await page.evaluate(_SCROLL_INFO_JS)
            print("\n初始状态：")
            print(f"  表情节点数 = {info['itemCount']}")
            print(f"  容器 class = {info['contentClass']}")
            print(f"  面板矩形 = {info['panelRect']}")
            print("  可滚动祖先：")
            for s in info.get("scrollables") or []:
                print(
                    f"    - {s['cls'] or '(无 class)'}  overflowY={s['overflowY']} "
                    f"scrollable={s['scrollable']} scrollHeight={s['scrollHeight']} "
                    f"clientHeight={s['clientHeight']} scrollTop={s['scrollTop']}"
                )
            if not info.get("scrollables"):
                print("    （没有找到可滚动祖先 —— 面板一屏就能显示全部？）")

            print(f"\n逐轮滚到底（最多 {args.rounds} 轮）：")
            history = [info["itemCount"]]
            for round_no in range(1, args.rounds + 1):
                result = await page.evaluate(_SCROLL_BOTTOM_JS)
                await page.wait_for_timeout(args.wait)
                after = await page.evaluate(_MESSAGE_FINGERPRINT_JS)
                if after != baseline:
                    print(f"  × 第 {round_no} 轮后消息区变了，立即停止。")
                    break
                latest = await page.evaluate(_SCROLL_INFO_JS)
                count = latest["itemCount"]
                history.append(count)
                print(
                    f"  第 {round_no} 轮：节点数 {count}（atBottom={result.get('atBottom')}，"
                    f"scrollTop={result.get('scrollTop')}/{result.get('scrollHeight')}）"
                )
                if count == history[-2] and result.get("atBottom"):
                    print("  → 数量不再增长且已到底，收敛。")
                    break

            print("\n汇总：")
            print(f"  节点数轨迹 = {history}")
            print(f"  初始 {history[0]} → 最终 {history[-1]}（多捞到 {history[-1] - history[0]} 项）")

            if args.json:
                payload = {
                    "tab": args.tab,
                    "history": history,
                    "initial": info,
                    "final": await page.evaluate(_SCROLL_INFO_JS),
                }
                Path(args.json).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                print(f"  明细已写入 {args.json}")
            return 0
    except AlreadyRunningError as exc:
        print(f"× {exc}")
        return 3


def main() -> int:
    parser = argparse.ArgumentParser(description="诊断表情面板的懒渲染情况")
    parser.add_argument("--tab", type=int, default=3, help="要量的分类序号（默认 3）")
    parser.add_argument("--friend", default="", help="用哪个会话唤出输入框（默认取 config.json 第一个好友）")
    parser.add_argument("--wait", type=int, default=900, help="每轮滚动后的等待毫秒数")
    parser.add_argument("--rounds", type=int, default=25, help="最多滚多少轮")
    parser.add_argument("--json", default="", help="把明细写成 JSON 到这个路径")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
