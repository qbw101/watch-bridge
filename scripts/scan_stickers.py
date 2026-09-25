"""扫描抖音私信的表情面板，生成手表端表情库（watch_stickers.json）。

为什么需要它
------------
手表端要发「自己收藏的表情」，就得知道那些表情在面板里叫什么、长什么样。
手写这些名字不现实（抖音面板里很多表情都叫「表情」），所以让浏览器去看。

安全前提（很重要）
------------------
表情面板里的**表情本体点一下就立刻发送**（`app/sender.py::_click_and_confirm_sticker`）。
所以要写进库里的东西，一律靠读 DOM 拿，绝不点击：

- 遍历内容区 DOM 里的表情节点，一次性收集（不滚动、不点表情）；
- **只有分类栏会被点击**（切页用），并且每点一栏都比对一次聊天区的消息框数量，
  一旦变化立刻中止 —— `--click-tabs` 实测过点分类栏不会发消息，但这是守卫不是假设。

为什么要点分类栏：面板底部的分类是**纯图标**（没有文字、没有 aria-label），
自建/收藏表情也没有名字，只能靠「第几栏 + 该栏第几个」定位。不点开就扫不到它们。

用法
----
    python scripts/scan_stickers.py                 # 扫全部分类，合并进 watch_stickers.json
    python scripts/scan_stickers.py --tabs          # 只 dump 分类栏结构（完全不点击）
    python scripts/scan_stickers.py --click-tabs    # 逐栏 dump 内容，搞清哪一栏是什么（会点分类栏）
    python scripts/scan_stickers.py --probe         # 只 dump 面板结构，不写配置
    python scripts/scan_stickers.py --scan-tabs 0,2 # 只扫指定分类（省时间，但没扫的栏会少一批表情）
    python scripts/scan_stickers.py --scan-tabs ""  # 老路：只扫面板打开时默认那一屏
    python scripts/scan_stickers.py --friend "小明"  # 指定用哪个会话打开面板
    python scripts/scan_stickers.py --reset         # 丢掉旧库，完全按这次扫描结果重建

跑完可以用 `python scripts/make_sticker_board.py --open` 在电脑上肉眼核对
「哪些表情入库了、哪些分类被漏掉了」。

跑之前请先停掉手表服务（两边都持有 artifacts/run.lock，会互相挡住）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.browser import open_douyin, open_private_messages, verify_login
from app.config import load_settings
from app.douyin import DouyinChat, RefreshYielded, first_visible
from app.lockfile import AlreadyRunningError, run_lock
from app.selectors import (
    STICKER_BUTTONS,
    STICKER_PANELS,
    STICKER_TAB_CONTENT,
    STICKER_TAB_ITEMS,
    STICKER_TABS,
    sticker_resource_key,
)
from bridge import sticker_thumbs
from bridge.session import ALLOWED_MEDIA_HOSTS
from bridge.sticker_store import (
    THUMB_EXTENSIONS,
    LibraryItem,
    StickerLibrary,
    make_item_id,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
LOGGER = logging.getLogger("scan_stickers")

PROBE_PATH = Path("artifacts/stickers_probe.json")
TABS_PROBE_PATH = Path("artifacts/stickers_tabs_probe.json")
THUMB_CONCURRENCY = 4


# 面板结构采集。刻意只读取 class / 属性 / 图片地址，不读取聊天内容。
_DUMP_JS = """
() => {
  const panelSelectors = ['.componentsemojiemojiPanel', '[class*="emojiPanel"]', '[role="dialog"]', '[class*="sticker"]'];
  let panel = null, panelSelector = null;
  for (const sel of panelSelectors) {
    const el = document.querySelector(sel);
    if (el) { panel = el; panelSelector = sel; break; }
  }
  if (!panel) return {found: false};

  const textOf = (el) => {
    const desc = el.querySelector('[class*="emojiEmojiItememojiItemDesc"]');
    return (desc ? (desc.textContent || '') : '').trim();
  };
  const attrOf = (el) => (el.getAttribute('aria-label') || el.getAttribute('title') || el.getAttribute('alt') || '').trim();
  const srcOf = (el) => {
    const img = el.tagName === 'IMG' ? el : el.querySelector('img');
    if (img) {
      const src = img.getAttribute('src') || img.getAttribute('data-src') || '';
      if (src) return src;
    }
    const bg = getComputedStyle(el).backgroundImage || '';
    const m = /url\\(["']?(.+?)["']?\\)/.exec(bg);
    return m ? m[1] : '';
  };

  // item 容器的 class 是 emojiEmojiItememojiItem，名字容器是 ...ItemDesc，
  // 后者是子串匹配的附带结果，按 Desc 后缀剔掉。
  const nodes = Array.from(panel.querySelectorAll('[class*="emojiEmojiItememojiItem"]'));
  const rows = [];
  nodes.forEach((el, index) => {
    const cls = typeof el.className === 'string' ? el.className : '';
    if (cls.includes('Desc')) return;
    rows.push({
      index,
      name: textOf(el) || attrOf(el),
      src: srcOf(el),
      cls,
      hidden: !(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
      parentCls: el.parentElement && typeof el.parentElement.className === 'string' ? el.parentElement.className : '',
    });
  });

  const classes = new Set();
  for (const el of panel.querySelectorAll('*')) {
    const c = typeof el.className === 'string' ? el.className : '';
    if (c) c.split(/\\s+/).forEach((one) => { if (one) classes.add(one); });
  }

  // 按直接父容器归类：抖音若给每个分类一个独立容器，这里会自然分出几组。
  const parentGroups = {};
  rows.forEach((row) => {
    const key = row.parentCls || '(none)';
    parentGroups[key] = (parentGroups[key] || 0) + 1;
  });

  // 分类栏候选：同一父节点下 ≥2 个同 class 的兄弟元素。
  // 真正有用的信号是 itemCount === 0（组内不含表情项）且 texts 像是分类名。
  const groups = [];
  for (const el of panel.querySelectorAll('*')) {
    const kids = Array.from(el.children);
    if (kids.length < 2 || kids.length > 30) continue;
    const classNames = kids.map((k) => (typeof k.className === 'string' ? k.className : ''));
    if (!classNames[0] || new Set(classNames).size !== 1) continue;
    const sample = kids[0].getBoundingClientRect();
    groups.push({
      parentCls: typeof el.className === 'string' ? el.className : '',
      childCls: classNames[0],
      count: kids.length,
      texts: kids.map((k) => (k.textContent || '').trim().slice(0, 10)),
      hasImg: kids.some((k) => k.querySelector('img')),
      itemCount: kids.reduce((n, k) => n + k.querySelectorAll('[class*="emojiEmojiItememojiItem"]').length, 0),
      size: [Math.round(sample.width), Math.round(sample.height)],
      rects: kids.slice(0, 8).map((k) => {
        const r = k.getBoundingClientRect();
        return [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)];
      }),
    });
  }

  const panelRect = panel.getBoundingClientRect();
  return {
    found: true,
    panelSelector,
    panelClass: typeof panel.className === 'string' ? panel.className : '',
    panelRect: [Math.round(panelRect.x), Math.round(panelRect.y), Math.round(panelRect.width), Math.round(panelRect.height)],
    viewport: [window.innerWidth, window.innerHeight],
    rowCount: rows.length,
    rows,
    parentGroups,
    classes: Array.from(classes).slice(0, 250),
    siblingGroups: groups.slice(0, 40),
    html: panel.outerHTML.slice(0, 6000),
  };
}
"""


# 分类栏探测。仍只读 DOM，不点任何东西 —— 用来搞清「自建表情」在哪一栏。
_TABS_JS = """
() => {
  const panelSelectors = ['.componentsemojiemojiPanel', '[class*="emojiPanel"]', '[role="dialog"]', '[class*="sticker"]'];
  let panel = null, panelSelector = null;
  for (const sel of panelSelectors) {
    const el = document.querySelector(sel);
    if (el) { panel = el; panelSelector = sel; break; }
  }
  if (!panel) return {found: false};

  const rectOf = (el) => { const r = el.getBoundingClientRect(); return [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)]; };
  const clsOf = (el) => (typeof el.className === 'string' ? el.className : '');
  const visOf = (el) => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);

  const tabNodes = Array.from(panel.querySelectorAll('[class*="emojiEmojisModalTab"]')).map((el) => ({
    tag: el.tagName,
    cls: clsOf(el),
    text: (el.textContent || '').trim().slice(0, 24),
    aria: el.getAttribute('aria-label') || '',
    title: el.getAttribute('title') || '',
    children: el.children.length,
    imgs: el.querySelectorAll('img').length,
    svgs: el.querySelectorAll('svg').length,
    rect: rectOf(el),
    visible: visOf(el),
  }));

  const content = panel.querySelector('.componentsemojitabPanel') || panel;
  const classCount = {};
  content.querySelectorAll('*').forEach((el) => {
    const c = clsOf(el);
    if (c) c.split(/\\s+/).forEach((one) => { if (one) classCount[one] = (classCount[one] || 0) + 1; });
  });

  const itemNodes = Array.from(content.querySelectorAll('[class*="emojiEmojiItememojiItem"]')).filter((el) => !clsOf(el).includes('Desc'));
  const items = [];
  itemNodes.forEach((el, i) => {
    const img = el.querySelector('img');
    items.push({
      i,
      cls: clsOf(el),
      text: (el.textContent || '').trim().slice(0, 20),
      aria: el.getAttribute('aria-label') || '',
      title: el.getAttribute('title') || '',
      role: el.getAttribute('role') || '',
      imgSrc: img ? (img.getAttribute('src') || img.getAttribute('data-src') || '') : '',
      imgAlt: img ? (img.getAttribute('alt') || '') : '',
      rect: rectOf(el),
      visible: visOf(el),
    });
  });

  return {
    found: true,
    panelSelector,
    panelClass: clsOf(panel),
    panelRect: rectOf(panel),
    viewport: [window.innerWidth, window.innerHeight],
    tabNodes,
    contentClass: clsOf(content),
    contentChildClasses: Array.from(content.children).map((k) => clsOf(k)).slice(0, 12),
    classCount,
    itemCount: itemNodes.length,
    items: items.slice(0, 40),
    tabsHtml: (panel.querySelector('.emojiEmojisModalTabtabs') || panel).outerHTML.slice(0, 3000),
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
        print("  请先停掉手表服务，再跑扫描。")
        return 3

    try:
        async with open_douyin(settings) as session:
            page = session.page
            await open_private_messages(page)
            await verify_login(page)

            friend = args.friend
            if not friend:
                friends = _friends_from_config(settings.config_path)
                if not friends:
                    print("× config.json 里没有 friends，无法确定用哪个会话打开表情面板。")
                    print("  用 --friend \"好友昵称\" 指定一个。")
                    return 2
                friend = friends[0]
            print(f"→ 打开会话「{friend}」以唤出输入框…")
            chat = DouyinChat(page)
            await chat.open_target(friend)

            print("→ 点开表情面板（只点这一个按钮）…")
            button = await first_visible(page, STICKER_BUTTONS)
            await button.click(force=True)
            await page.wait_for_timeout(1500)
            if not await _panel_visible(page):
                print("× 表情面板没有出现，抖音可能改版了。请检查 STICKER_PANELS 选择器。")
                return 1
            await page.wait_for_timeout(1000)

            if args.tabs:
                tabs_dump = await page.evaluate(_TABS_JS)
                if not tabs_dump or not tabs_dump.get("found"):
                    print("× 没能在页面里定位到表情面板容器。")
                    return 1
                tabs_path = PROJECT_ROOT / TABS_PROBE_PATH
                tabs_path.parent.mkdir(parents=True, exist_ok=True)
                tabs_path.write_text(
                    json.dumps(tabs_dump, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                _print_tabs_summary(tabs_dump)
                print(f"\n完整结构见 {tabs_path}")
                return 0

            if args.click_tabs:
                return await _dump_tabs_content(page, artifacts_dir)

            if args.verify:
                return await _verify_library(page, artifacts_dir, log=print)

            artifacts_dir.mkdir(parents=True, exist_ok=True)
            tab_indices = _parse_tab_list(args.scan_tabs)
            # None 和 [0,2] 都是「按分类扫」，[] 才是「只扫默认那一屏」。
            use_tabs = tab_indices is None or len(tab_indices) > 0
            # 「只扫默认那一屏」没有分类概念，拿不到 per_tab 统计。
            scan_stats: dict[str, Any] | None = None

            # 默认不收的栏。用户点名要（--scan-tabs 写上）或显式放行（--include-tabs）时让路。
            forced = set(_parse_tab_list(args.include_tabs) or [])
            explicitly = set(tab_indices or [])
            skip_tabs = set(DEFAULT_SKIP_TABS) - forced - explicitly
            if tab_indices is not None:
                # 点名扫的时候连 disabled 也只在列表内，跳过集合同样要收敛到列表内。
                tab_indices = [i for i in tab_indices if i not in skip_tabs]
                use_tabs = len(tab_indices) > 0
            if skip_tabs:
                print(
                    f"→ 分类 {sorted(skip_tabs)} 默认不收（这一栏排列不稳定，"
                    "缩略图和实际发出去的可能对不上）。要收加 --include-tabs "
                    + ",".join(str(i) for i in sorted(skip_tabs))
                )

            if use_tabs:
                entries, scan_stats = await _collect_tab_entries(
                    page, tab_indices, log=print, skip_tabs=skip_tabs
                )
                if not entries:
                    print("× 没有从指定分类里解析出任何表情。")
                    print("  先跑 `--tabs` 看分类栏、再跑 `--click-tabs` 看每栏实际有什么。")
                    return 1
            else:
                dump = await page.evaluate(_DUMP_JS)
                if not dump or not dump.get("found"):
                    print("× 没能在页面里定位到表情面板容器。")
                    return 1

                rows = dump.get("rows") or []
                print(f"→ 面板选择器 {dump.get('panelSelector')}，采集到 {len(rows)} 个表情节点")

                probe_path = PROJECT_ROOT / PROBE_PATH
                probe_path.parent.mkdir(parents=True, exist_ok=True)
                probe_path.write_text(
                    json.dumps(dump, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                print(f"  结构诊断已写入 {probe_path}")

                if args.probe:
                    _print_probe_summary(dump)
                    return 0

                entries = _to_entries(rows, log=print)
                if not entries:
                    print("× 没有解析出任何表情。看上面那份结构诊断，或者把 --probe 的输出发我看。")
                    return 1

            thumb_dir = artifacts_dir / "stickers"
            thumb_dir.mkdir(parents=True, exist_ok=True)
            usable = [entry for entry in entries if entry["src"]]
            skipped = len(entries) - len(usable)
            if skipped:
                print(f"  其中 {skipped} 个没有可用图片，手表上将显示成文字按钮")
            if usable:
                print(f"→ 下载 {len(usable)} 张缩略图到 {thumb_dir} …")
                await _download_thumbs(session, usable, thumb_dir, log=print)

            result = _persist_entries(
                entries, artifacts_dir, skip_tabs, scan_stats=scan_stats, reset=args.reset, log=print
            )
            library = result["library"]

            print("")
            print(f"✓ 表情库已写入 {library.path}")
            print(
                f"  原有 {result['before']} 项 → 现在 {result['items']} 项"
                f"（{result['with_thumb']} 项带缩略图，新增 {result['added']} 项）"
            )
            if use_tabs:
                by_tab: dict[str, int] = {}
                for item in library.items:
                    key = "未记分类" if item.tab_index is None else f"分类{item.tab_index}"
                    by_tab[key] = by_tab.get(key, 0) + 1
                print("  分布：" + "，".join(f"{k} {v} 项" for k, v in sorted(by_tab.items())))
                print("  发送靠「第几栏 + 栏内第几个」定位，这两个序号已一并写入库文件。")
                print("  服务若正在运行，手表刷新一下即可看到新表情；库文件改动会被自动感知。")
                print("  想核对库里到底有什么，跑 python scripts/make_sticker_board.py --open")
            else:
                no_category = sum(1 for item in library.items if not item.category)
                if no_category:
                    print("")
                    print(f"! 其中 {no_category} 项没有记录所属分类。")
                    print("  这条老路只扫「面板打开时默认那一屏」，自建表情不在那一屏里。")
                    print("  用 `--scan-tabs 0,2` 走分类扫描（会点分类栏，但不会点表情）。")
                else:
                    print("  服务若正在运行，手表刷新一下即可看到新表情；库文件改动会被自动感知。")
            return 0
    finally:
        lock.__exit__(None, None, None)


def _print_probe_summary(dump: dict[str, Any]) -> None:
    print("\n===== 面板结构摘要 =====")
    print(f"容器 class: {dump.get('panelClass')}")
    print(f"容器位置尺寸: {dump.get('panelRect')}  视口: {dump.get('viewport')}")
    print(f"表情节点数: {dump.get('rowCount')}")
    print("\n-- 前 20 个表情节点 --")
    for row in (dump.get("rows") or [])[:20]:
        print(
            f"  [{row.get('index')}] name={row.get('name')!r} "
            f"hidden={row.get('hidden')} src={str(row.get('src'))[:90]}"
        )
    print("\n-- 按直接父容器归类（若每个分类一个容器，这里会分出多组） --")
    for cls, count in (dump.get("parentGroups") or {}).items():
        print(f"  {count:>4} 项  parentCls={cls}")
    print("\n-- 疑似分类栏（同父节点同 class 的兄弟组，重点看 itemCount=0 的） --")
    for group in (dump.get("siblingGroups") or [])[:15]:
        print(
            f"  count={group.get('count')} items={group.get('itemCount')} "
            f"hasImg={group.get('hasImg')} size={group.get('size')}"
        )
        print(f"      childCls={group.get('childCls')}")
        print(f"      parentCls={group.get('parentCls')}")
        print(f"      texts={group.get('texts')}")
    print("\n-- 面板内出现过的 class（前 80 个） --")
    for name in (dump.get("classes") or [])[:80]:
        print(f"  {name}")
    print("\n完整结构见 artifacts/stickers_probe.json（含 6000 字符 outerHTML 片段）")


# 某一栏的内容。关键点：这里枚举用的是 `.emojiEmojiItememojiItem`，
# 与发送端 `send_douyin_sticker` 定位表情项用的选择器**必须完全一致** ——
# 扫描记下的序号就是发送时要点第几个，两者一旦分叉就会「点的是这个、发的是那个」。
# （不能用「直接子节点」：有的栏外面还包了一层没有 class 的 wrapper。）
_TAB_ITEMS_JS = """
() => {
  const panel = document.querySelector('.componentsemojiemojiPanel') || document.querySelector('[class*="emojiPanel"]');
  if (!panel) return {found: false};
  const content = panel.querySelector('.componentsemojitabPanel') || panel;
  const nodes = Array.from(content.querySelectorAll('.emojiEmojiItememojiItem'));
  const items = nodes.map((el, i) => {
    const cls = typeof el.className === 'string' ? el.className : '';
    const img = el.querySelector('img');
    const desc = el.querySelector('.emojiEmojiItememojiItemDesc');
    const r = el.getBoundingClientRect();
    return {
      i: i,
      tag: el.tagName,
      cls: cls,
      desc: desc ? (desc.textContent || '').trim() : '',
      text: (el.textContent || '').trim().slice(0, 24),
      aria: el.getAttribute('aria-label') || '',
      title: el.getAttribute('title') || '',
      role: el.getAttribute('role') || '',
      img: img ? (img.getAttribute('src') || img.getAttribute('data-src') || '') : '',
      rect: [Math.round(r.x), Math.round(r.y), Math.round(r.width), Math.round(r.height)],
      visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
    };
  });
  const classCount = {};
  content.querySelectorAll('*').forEach((el) => {
    const c = typeof el.className === 'string' ? el.className : '';
    if (c) c.split(/\\s+/).forEach((one) => { if (one) classCount[one] = (classCount[one] || 0) + 1; });
  });
  return {
    found: true,
    contentClass: typeof content.className === 'string' ? content.className : '',
    childCount: content.children.length,
    itemCount: nodes.length,
    classCount: classCount,
    items: items.slice(0, 120),
  };
}
"""

# 点分类栏的安全阀。抖音的表情面板里，**表情本体**点一下就发送；分类栏只是切页。
# 但这是推断，不是保证 —— 所以每点一栏都比对一次聊天区消息框数量，
# 一旦变化说明真的发出去了，立即停止后续点击。
_MESSAGE_FINGERPRINT_JS = """
() => {
  const list = document.querySelector('.messageMessageListlist');
  if (!list) return 'no-list';
  const boxes = list.querySelectorAll('.messageMessageBoxmessageBox');
  const last = boxes[boxes.length - 1];
  return boxes.length + '|' + (last ? last.outerHTML.length : 0);
}
"""


async def _dump_tabs_content(page, artifacts_dir: Path) -> int:
    """逐个点开分类栏，把每一栏的表情项 dump 出来（只点分类，绝不点表情）。"""
    tabs = page.locator(".emojiEmojisModalTabsubTab")
    total = await tabs.count()
    if total == 0:
        print("× 没找到分类栏（.emojiEmojisModalTabsubTab）。")
        return 1
    print(f"→ 分类栏共 {total} 个 tab")
    baseline = await page.evaluate(_MESSAGE_FINGERPRINT_JS)
    print(f"  消息区指纹（基准）= {baseline}")

    report: list[dict[str, Any]] = []
    for index in range(total):
        tab = tabs.nth(index)
        cls = (await tab.get_attribute("class")) or ""
        entry: dict[str, Any] = {"index": index, "class": cls, "rect": await tab.bounding_box()}
        if "disabled" in cls:
            entry["skipped"] = "disabled"
            print(f"  [{index}] 跳过（disabled）")
            report.append(entry)
            continue
        try:
            await tab.click(force=True)
        except Exception as exc:
            entry["skipped"] = f"click failed: {exc}"
            print(f"  [{index}] 点击失败：{exc}")
            report.append(entry)
            continue
        await page.wait_for_timeout(1200)
        content = await page.evaluate(_TAB_ITEMS_JS)
        after = await page.evaluate(_MESSAGE_FINGERPRINT_JS)
        entry["content"] = content
        entry["fingerprint_after"] = after
        entry["message_sent_guard"] = after != baseline
        print(
            f"  [{index}] 表情项 {content.get('itemCount')} 个（内容区 {content.get('childCount')} 个子节点）"
            f"  指纹={after} 已发送={after != baseline}"
        )
        if content.get("classCount"):
            top = sorted(content["classCount"].items(), key=lambda kv: -kv[1])[:5]
            print("        该栏 class 频次前 5: " + ", ".join(f"{n}×{c}" for n, c in top))
        report.append(entry)
        if after != baseline:
            print("  ! 点这一栏后聊天区变了，立即中止，不再继续点。")
            break

    path = artifacts_dir / "stickers_tabs_content.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"baseline": baseline, "tabs": report}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n→ 已写入 {path}")

    for entry in report:
        items = (entry.get("content") or {}).get("items") or []
        if not items:
            continue
        print(f"\n-- tab[{entry['index']}] 前 10 项 --")
        for row in items[:10]:
            print(
                f"   [{row.get('i'):>2}] cls={row.get('cls')} desc={row.get('desc')!r} "
                f"text={row.get('text')!r} aria={row.get('aria')!r}"
            )
            print(f"         img={str(row.get('img'))[:90]}")
    return 0


def _print_tabs_summary(dump: dict[str, Any]) -> None:
    print("\n===== 分类栏探测 =====")
    print(f"容器: {dump.get('panelSelector')}  位置尺寸: {dump.get('panelRect')}  视口: {dump.get('viewport')}")
    print(f"内容区 class: {dump.get('contentClass')}")
    print(f"内容区直接子节点 class: {dump.get('contentChildClasses')}")
    print(f"\n-- emojiEmojisModalTab* 节点（{len(dump.get('tabNodes') or [])} 个） --")
    for i, node in enumerate(dump.get("tabNodes") or []):
        print(
            f"  [{i:>2}] <{node.get('tag')}> vis={node.get('visible')} rect={node.get('rect')} "
            f"kids={node.get('children')} img={node.get('imgs')} svg={node.get('svgs')}"
        )
        print(f"        cls={node.get('cls')}")
        print(f"        text={node.get('text')!r} aria={node.get('aria')!r} title={node.get('title')!r}")
    print(f"\n-- 当前这栏的表情项（{dump.get('itemCount')} 个，只列前 40） --")
    for row in (dump.get("items") or []):
        print(
            f"  [{row.get('i'):>2}] vis={row.get('visible')} text={row.get('text')!r} "
            f"aria={row.get('aria')!r} alt={row.get('imgAlt')!r}"
        )
        print(f"        img={str(row.get('imgSrc'))[:100]}")
    print("\n-- 内容区 class 频次（前 40） --")
    counts = dump.get("classCount") or {}
    for name, n in sorted(counts.items(), key=lambda kv: -kv[1])[:40]:
        print(f"  {n:>4}  {name}")
    print("\n-- 分类栏 HTML 片段 --")
    print((dump.get("tabsHtml") or "")[:1500])


def _resource_key(src: str) -> str:
    """从表情图地址里取稳定的资源名（用来派生不随顺序漂移的 id）。

    规则本身在 `app.selectors.sticker_resource_key` —— 发送时要按同一个值去面板里
    认图（收藏新表情会插队、序号整体顺移，只有资源名不会变），所以两边必须共用
    一份实现，这里只是转发，不再自己写一遍。
    """
    return sticker_resource_key(src)


# 「扫全部分类」的哨兵值。返回 None 而不是 []，是因为后者有明确含义：
# 空字符串 = 走老路（只扫面板打开时默认那一屏），两者不能混。
ALL_TABS = "all"
ALL_TABS_ALIASES = {ALL_TABS, "*", "全部", "所有"}

# 默认不收的分类栏。
#
# 分类 3 是「自建/收藏」那一类里排列**不稳定**的一栏：实测同一个栏位在不同会话里
# 指向了不同的图（第 17/18/19 个整体后移一位、第 42↔43 互换），下次会话又换一批。
# 而这类表情没有名字，只能靠「第几栏第几个」定位 —— 顺序一变，点下去发出去的就是
# 另一个表情。缩略图显示 A、发出去是 B，这比"少一批表情"糟得多。
# 与其收一堆会发错的项，不如不收。需要时用 --include-tabs 3 强制扫一次看看。
DEFAULT_SKIP_TABS: tuple[int, ...] = (3,)

# 默认要扫的分类栏（用户明确要的：收藏的表情自动出现在手表上）。
# 留空表示「除 DEFAULT_SKIP_TABS 外全扫」。
DEFAULT_SCAN_TABS: tuple[int, ...] = ()


def _parse_tab_list(raw: str) -> list[int] | None:
    """解析 --scan-tabs。返回 None 表示「全部分类」，[] 表示「只扫默认那一屏」。

    为什么要支持 all：分类栏序号是抖音那边的实现细节，写死 0,2 迟早会漏。
    2026-09-20 那次就是这样 —— 面板实际有 7 栏、其中第 3 栏还压着 50 项，
    而默认只扫了 0 和 2，用户在手表上完全看不到那一栏的表情，
    库文件却看不出任何异常（没有任何报错，就是少了一批）。
    """
    text = (raw or "").strip()
    parts = [p.strip().lower() for p in text.replace("，", ",").split(",") if p.strip()]
    # 只要出现 all，就按「全部」处理。不这么写的话 `--scan-tabs all,3` 会退化成
    # 只扫分类 3 —— 与用户意图正好相反，而且不会报错。
    if any(part in ALL_TABS_ALIASES for part in parts):
        return None

    out: list[int] = []
    for part in parts:
        try:
            value = int(part)
        except ValueError:
            continue
        if value >= 0 and value not in out:
            out.append(value)
    return out


async def _wait_or_yield(page, ms: int, should_yield=None) -> None:
    """等一小段；如果期间有界面请求在等浏览器就立刻放弃这一轮刷新。

    刻意拆成小片轮询而不是一个 `wait_for_timeout(1200)`：刷新全程要等好几次
    「面板切栏后渲染」，那些等待加起来十几秒。整段傻等的话，用户在这期间点一下
    发送就得排到整段后面 —— 而这一轮刷新本来就是他打开表情面板时顺手踢的。
    切片之后最坏只多等他一个片长（200ms 级），体感上就是「秒发」。
    """
    if should_yield is None:
        await page.wait_for_timeout(ms)
        return
    step = 200
    waited = 0
    while waited < ms:
        if should_yield():
            raise RefreshYielded("有界面请求在等浏览器，本轮刷新让路")
        await page.wait_for_timeout(min(step, ms - waited))
        waited += step


async def _collect_tab_entries(
    page, indices: list[int] | None, log, skip_tabs: set[int] | None = None, should_yield=None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """逐个点开指定分类栏，把每栏的表情项收成 entries（外加一份本次扫描统计）。

    `indices` 传 None 表示「除 skip_tabs 外全扫」（连 disabled 和空的也会走一遍，
    但空栏不产出任何项，所以不会污染结果）。

    `skip_tabs` 是明确不收的栏（见 DEFAULT_SKIP_TABS 的说明）。它和 disabled 不同：
    disabled 是抖音说这栏没内容，skip_tabs 是我们主动不看某栏，提示语要分开。

    每条 entry 多带两个字段：
    - `tab_index`：面板底部第几个分类栏（0 起）
    - `fallback_index`：该栏里第几个表情项（0 起）

    这一对序号就是发送时要点「第几栏的第几个」，所以枚举用的选择器必须和
    `app/sender.py` 的 `STICKER_TAB_ITEMS` 完全一致，否则会「点的是这个、发的是那个」。
    """
    skip_tabs = set(skip_tabs or ())
    tabs = page.locator(STICKER_TABS)
    total = await tabs.count()
    if total == 0:
        log(f"× 没找到表情面板底部的分类栏（{STICKER_TABS}）。")
        return [], {"total_tabs": 0, "scanned": [], "disabled": [], "per_tab": {}}
    if indices is None:
        # 先记住「本次是全扫」。下面 indices 会被展开成完整列表，
        # 之后就没法再区分「全扫」和「点名扫」了，而那正决定了漏扫提示该怎么说。
        full_scan = True
        indices = list(range(total))
        log(f"→ 分类栏共 {total} 个，本次全扫")
    else:
        full_scan = False
        skipped_tabs = [i for i in range(total) if i not in indices]
        log(f"→ 分类栏共 {total} 个，本次扫 {indices}")
        if skipped_tabs:
            log(f"  注意：分类 {skipped_tabs} 本次没扫，这些栏里的表情不会入库")
    if skip_tabs:
        log(f"  按默认规则跳过分类 {sorted(skip_tabs)}（排列不稳定，收了会发错；--include-tabs 可强制扫）")
    baseline = await page.evaluate(_MESSAGE_FINGERPRINT_JS)

    entries: list[dict[str, Any]] = []
    scanned: list[int] = []
    disabled: list[int] = []
    skipped_by_rule: list[int] = []
    per_tab: dict[int, int] = {}
    for index in indices:
        if index >= total:
            log(f"  [{index}] 超出范围（共 {total} 个），跳过")
            continue
        if index in skip_tabs:
            # 不再逐栏打日志：进循环前和出循环后各有一句说明，这里再打就是三遍了。
            skipped_by_rule.append(index)
            continue
        tab = tabs.nth(index)
        cls = (await tab.get_attribute("class")) or ""
        if "disabled" in cls:
            # disabled = 面板上这一栏本来就没内容，不是「漏扫」。
            # 分清这件事很重要：否则全扫时也会被当成漏扫，提示去加 --scan-tabs all（无效建议）。
            disabled.append(index)
            log(f"  [{index}] disabled（面板上无内容），跳过")
            continue
        await tab.click(force=True)
        await _wait_or_yield(page, 1200, should_yield)
        content = await page.evaluate(_TAB_ITEMS_JS)
        after = await page.evaluate(_MESSAGE_FINGERPRINT_JS)
        if after != baseline:
            log(f"  ! 点分类 [{index}] 后聊天区消息数变了（{baseline} → {after}），立即中止扫描。")
            break

        rows = content.get("items") or []
        src_count: dict[str, int] = {}
        for row in rows:
            src = str(row.get("img") or "").strip()
            if src:
                src_count[src] = src_count.get(src, 0) + 1

        named = 0
        unnamed = 0
        seen_name: dict[str, int] = {}
        for row in rows:
            src = str(row.get("img") or "").strip()
            shared = bool(src) and src_count.get(src, 0) > 1
            key = _resource_key(src)
            raw_name = str(row.get("desc") or "").strip()
            if raw_name:
                # 有名字的（内置表情）：沿用按名字定位那条老路，id 也保持与从前一致。
                count = seen_name.get(raw_name, 0) + 1
                seen_name[raw_name] = count
                uid = raw_name if count == 1 else f"{raw_name} ({count})"
                name = raw_name
                label = uid
                auto_label = False
                named += 1
            else:
                # 自建/收藏表情：没有名字也没有 aria-label，只能按序号发。
                if not key:
                    continue
                uid = f"tab{index}:{key}"
                name = f"tab{index}-{row.get('i', 0) + 1}"
                label = str(row.get("i", 0) + 1)
                auto_label = True
                unnamed += 1
            entries.append(
                {
                    "name": name,
                    "label": label,
                    "uid": uid,
                    "src": "" if shared else src,
                    "shared_thumb": shared,
                    "category": None,
                    "tab_index": index,
                    "fallback_index": row.get("i", 0),
                    "source_key": key,
                    "auto_label": auto_label,
                }
            )
        scanned.append(index)
        per_tab[index] = len(rows)
        log(f"  [{index}] {len(rows)} 个表情项：{named} 个有名字，{unnamed} 个按序号发（自建/收藏）")

    not_scanned = [i for i in range(total) if i not in scanned]
    # 四种「没扫到」要分开说，否则提示会自相矛盾：
    #   untouched       —— 点名扫时故意没选的栏（尚可通过 --scan-tabs all 补上）
    #   failed          —— 点名要扫却因越界/中止没扫成（补扫无效，得排查）
    #   disabled        —— 面板上本来就没内容，不是漏扫
    #   skipped_by_rule —— 我们主动不收（排列不稳定）
    # 全扫时不存在 untouched，此时再说「要一起扫请加 --scan-tabs all」纯属误导；
    # skipped_by_rule 也不能算 failed —— 全扫时 indices 覆盖了它，不排掉就会
    # 冒出「点名要扫却没扫成」这种自相矛盾的告警（实测踩过）。
    excluded = set(disabled) | set(skipped_by_rule)
    untouched = [] if full_scan else [i for i in not_scanned if i not in excluded and i not in indices]
    failed = [i for i in not_scanned if i not in excluded and i in indices]
    if disabled:
        log(f"  分类 {disabled} 是 disabled 状态（面板上本来就没有内容），无需处理。")
    if skipped_by_rule:
        log(f"  分类 {sorted(skipped_by_rule)} 按默认规则没收，不会出现在手表上。")
    if untouched:
        # 这条提醒是刻意的：漏扫不会报错、不会警告、库文件也看不出异常，
        # 唯一的症状是「手表上少了一批表情」。不在扫描时就把它喊出来，事后极难定位。
        log(
            f"! 分类 {untouched} 没有扫描（本次扫了 {scanned}）。"
            "这些栏里的表情不会出现在手表上 —— 要一起扫请加 --scan-tabs all。"
        )
    if failed:
        log(
            f"! 分类 {failed} 本次点名要扫却没扫成（可能是面板结构变了或扫描被中止）。"
            "这些栏里的表情不会出现在手表上，请重跑一次；反复出现请用 --probe 看结构。"
        )

    stats = {
        "total_tabs": total,
        "scanned": scanned,
        "disabled": disabled,
        "skipped_by_rule": skipped_by_rule,
        "untouched": untouched,
        "failed": failed,
        "full_scan": full_scan,
        "per_tab": {str(k): v for k, v in sorted(per_tab.items())},
    }
    return entries, stats


def _box_inside(inner: dict[str, float], outer: dict[str, float], margin: float = 4.0) -> bool:
    """inner 是否落在 outer 里（留几像素误差，滚动位置常是小数对齐）。"""
    return (
        inner["x"] >= outer["x"] - margin
        and inner["y"] >= outer["y"] - margin
        and inner["x"] + inner["width"] <= outer["x"] + outer["width"] + margin
        and inner["y"] + inner["height"] <= outer["y"] + outer["height"] + margin
    )


async def _verify_library(page, artifacts_dir: Path, log) -> int:
    """校验库里的「第几栏 + 第几个」在当前面板上还指得对。

    全程只读、不点表情：按序号定位到 DOM 节点，再比对图片的资源名。
    序号是发送时唯一的定位依据（自建表情没有名字可比），所以这个校验等价于
    「发出去的就是你想发的那个」。改过库、或抖音那边增删过表情之后跑一次最稳。
    """
    library = StickerLibrary.load(PROJECT_ROOT, artifacts_dir)
    items = [item for item in library.items if item.tab_index is not None]
    if not items:
        log("× 库里没有任何带分类序号的项，没什么可校验的。先跑一次默认扫描。")
        return 1

    by_tab: dict[int, list[LibraryItem]] = {}
    for item in items:
        by_tab.setdefault(item.tab_index, []).append(item)

    tabs = page.locator(STICKER_TABS)
    total_tabs = await tabs.count()
    baseline = await page.evaluate(_MESSAGE_FINGERPRINT_JS)

    ok = 0
    problems: list[str] = []
    # 每个分类分别记「对不上几条」和「这一栏当前实际有哪些图」。
    # 用来判断对不上的性质：是抖音那边插删表情导致序号整体顺移（重扫即可修），
    # 还是同一批图自己换了位置（重扫也修不好，每次会话都可能不一样）。
    mismatch_tabs: dict[int, int] = {}
    panel_keys_by_tab: dict[int, list[str]] = {}
    for tab_index in sorted(by_tab):
        if tab_index >= total_tabs:
            problems.append(f"分类 {tab_index}: 面板上只有 {total_tabs} 个分类")
            continue
        tab = tabs.nth(tab_index)
        cls = (await tab.get_attribute("class")) or ""
        if "disabled" in cls:
            problems.append(f"分类 {tab_index}: 现在是 disabled 状态")
            continue
        await tab.click(force=True)
        await page.wait_for_timeout(1200)
        if await page.evaluate(_MESSAGE_FINGERPRINT_JS) != baseline:
            problems.append(f"分类 {tab_index}: 点开之后聊天区消息数变了，中止")
            break
        rows = (await page.evaluate(_TAB_ITEMS_JS)).get("items") or []
        by_index = {row.get("i"): row for row in rows}
        panel_keys_by_tab[tab_index] = [k for k in (_resource_key(str(r.get("img") or "")) for r in rows) if k]
        log(f"  [分类 {tab_index}] 面板上 {len(rows)} 个表情项，库里记了 {len(by_tab[tab_index])} 项")

        for item in sorted(by_tab[tab_index], key=lambda one: one.fallback_index or 0):
            position = item.fallback_index or 0
            row = by_index.get(position)
            if row is None:
                problems.append(f"{item.name}: 该栏第 {position + 1} 个不存在（面板上只有 {len(rows)} 个）")
                continue
            src = str(row.get("img") or "")
            desc = str(row.get("desc") or "").strip()
            key = _resource_key(src)
            if item.source_key:
                if key and key == item.source_key:
                    ok += 1
                else:
                    mismatch_tabs[tab_index] = mismatch_tabs.get(tab_index, 0) + 1
                    problems.append(
                        f"{item.name}: 该栏第 {position + 1} 个现在指向 {key or '(没有图)'}，"
                        f"库里记的是 {item.source_key}"
                    )
            elif desc == item.name:
                ok += 1
            else:
                problems.append(f"{item.name}: 该栏第 {position + 1} 个现在指向 {desc!r}")

        # 序号对得上还不够：面板一屏装不下几十个自建表情，排在后面的项必须能滚进可视区，
        # 否则发送时那一click会落在面板外面（原因见 app/sender.py 里的说明）。
        last_position = max(one.fallback_index or 0 for one in by_tab[tab_index])
        try:
            last_item = page.locator(STICKER_TAB_ITEMS).nth(last_position)
            await last_item.scroll_into_view_if_needed(timeout=5000)
            item_box = await last_item.bounding_box()
            view_box = await page.locator(STICKER_TAB_CONTENT).first.bounding_box()
        except Exception as exc:
            problems.append(f"分类 {tab_index}: 滚动到第 {last_position + 1} 个失败（{exc}）")
            continue
        if item_box is None or view_box is None:
            problems.append(f"分类 {tab_index}: 第 {last_position + 1} 个滚进可视区后拿不到位置")
        elif _box_inside(item_box, view_box):
            log(f"        最末一项（该栏第 {last_position + 1} 个）能滚进可视区 ✓")
        else:
            problems.append(
                f"分类 {tab_index}: 第 {last_position + 1} 个滚动后仍在面板可视区外"
                f"（项 y={item_box['y']:.0f} h={item_box['height']:.0f}，"
                f"可视区 y={view_box['y']:.0f} h={view_box['height']:.0f}）"
            )

    print("")
    if problems:
        print(f"! {ok} 项对得上，{len(problems)} 项对不上：")
        for line in problems[:20]:
            print(f"    {line}")
        if len(problems) > 20:
            print(f"    …还有 {len(problems) - 20} 条")

        # 区分两种「对不上」，因为处置方式完全相反：
        #   顺移 —— 抖音那边增删了表情，后面全体挪位。重扫一次就能修好。
        #   漂移 —— 这一栏的排列本身不稳定：图还在，但位置变了（甚至整批换一批）。
        #           重扫只是把「这一次」的顺序记下来，下次打开面板照样会变 ——
        #           按序号发就可能发出别的表情。
        # 判据不能用「集合完全相同」：实测这一栏连集合都会变，那样根本判不出来。
        # 改成看「库里记的图有多少还在这栏里」—— 只要大部分还在，就说明问题是排列，
        # 不是表情真的没了。
        unstable: list[tuple[int, int, int, int]] = []
        for tab_index, count in sorted(mismatch_tabs.items()):
            lib_keys = [k for k in (one.source_key for one in by_tab[tab_index]) if k]
            panel_keys = set(panel_keys_by_tab.get(tab_index) or [])
            if not lib_keys:
                continue
            present = sum(1 for k in lib_keys if k in panel_keys)
            absent = len(lib_keys) - present
            if count >= 2 and present * 10 >= len(lib_keys) * 6:
                unstable.append((tab_index, count, present, absent))

        if unstable:
            for tab, count, present, absent in unstable:
                print("")
                print(f"× 分类 {tab}：{count} 项位置对不上，其中 {present} 项「图还在这栏里、只是换了位置」"
                      + (f"，另有 {absent} 项已不在这一栏。" if absent else "。"))
            print("  这不是表情被增删，而是这一栏的排列本身不稳定 —— 抖音按最近使用/热度之类的")
            print("  规则排，并列项的顺序每次会话都可能不同，甚至整批换一批。")
            print("  重扫也修不好：它只是把「这一次」的顺序记下来，下次打开面板照样会变。")
            print("  影响：这一栏的表情按序号发出去时，可能发的不是你点的那一个。")
            print("  建议：优先用有名字的表情（分类 0，按名字匹配，不受顺序影响）；")
            print("        或改成按图片资源名定位再换算成序号发送，那样顺序怎么变都不会发错。")
            stable_tabs = [t for t in mismatch_tabs if t not in {tab for tab, *_ in unstable}]
            if stable_tabs:
                print(f"  另有分类 {stable_tabs} 属于序号顺移，重扫一次即可刷新：")
                print("    python scripts/scan_stickers.py")
        else:
            print("  重新跑一次扫描即可刷新序号：python scripts/scan_stickers.py")
        return 1
    print(f"✓ 库里 {ok} 项的表情序号全部对得上，可以放心发。")
    return 0


def _to_entries(rows: list[dict[str, Any]], log) -> list[dict[str, Any]]:
    """把 DOM 节点整理成候选表情项。

    几个必须处理的现实问题：

    1. `name` 是要拿去表情面板里做精确匹配的，**一个字都不能改**。抖音面板里
       重名很常见（一堆都叫「表情」），所以区分它们靠的是 `label`（显示名）和
       `uid`（派生 id 用），绝不能往 `name` 上追加序号 —— 那样就匹配不到了。
    2. 图片地址可能被多个表情共用（雪碧图 / 默认占位图）。照搬的话手表上会显示成
       一张错的脸，比只显示文字更糟，所以共用地址的一律判为不可用。
    """
    seen_name: dict[str, int] = {}
    src_count: dict[str, int] = {}
    for row in rows:
        src = str(row.get("src") or "").strip()
        if src:
            src_count[src] = src_count.get(src, 0) + 1

    entries: list[dict[str, Any]] = []
    blank = 0
    for row in rows:
        raw_name = str(row.get("name") or "").strip()
        if not raw_name:
            blank += 1
            continue
        count = seen_name.get(raw_name, 0) + 1
        seen_name[raw_name] = count
        uid = raw_name if count == 1 else f"{raw_name} ({count})"
        src = str(row.get("src") or "").strip()
        shared = bool(src) and src_count.get(src, 0) > 1
        entries.append(
            {
                "name": raw_name,
                "label": uid,
                "uid": uid,
                "src": "" if shared else src,
                "shared_thumb": shared,
                "category": None,
            }
        )
    if blank:
        log(f"  跳过 {blank} 个没有名字的节点")
    if entries:
        shared_count = sum(1 for entry in entries if entry["shared_thumb"])
        if shared_count:
            log(f"  {shared_count} 个表情和其他表情共用同一张图（雪碧图或占位图），不给缩略图")
    return entries


async def _download_thumbs(session, entries: list[dict[str, Any]], thumb_dir: Path, log) -> None:
    semaphore = asyncio.Semaphore(THUMB_CONCURRENCY)
    done = 0
    total = len(entries)

    async def fetch(entry: dict[str, Any]) -> None:
        nonlocal done
        # 已经下过的直接跳过。缩略图文件名是按内容派生的 id（uid = 图片资源名），
        # 同一个文件名必然对应同一张图 —— 重下一次只会白白刷掉原图的 mtime，
        # 而派生小图的新鲜度判据正是「派生图 mtime ≥ 原图 mtime」：
        # 原图一被重写，几十张派生小图同时失效，手表打开表情面板时每张都要
        # 现场解码动图 webp（实测 240~590ms/张），网格就一格一格往外蹦。
        # 跳过之后：mtime 稳定 → 派生图继续命中 → 服务端每张只要 1ms。
        item_id = make_item_id(entry["uid"], entry["category"])
        existing = next((p for p in thumb_dir.glob(f"{item_id}.*") if p.is_file()), None)
        if existing is not None:
            entry["thumb"] = existing.name
            done += 1
            return
        url = entry["src"]
        if not _allowed_media_url(url):
            entry["src"] = ""
            return
        async with semaphore:
            try:
                response = await session.context.request.get(
                    url,
                    headers={"Referer": "https://www.douyin.com/", "Accept": "image/*,*/*;q=0.8"},
                    timeout=20_000,
                )
                if not response.ok:
                    entry["src"] = ""
                    return
                content_type = (response.headers or {}).get("content-type", "").split(";")[0].strip().lower()
                body = await response.body()
            except Exception:
                entry["src"] = ""
                return
        suffix = THUMB_EXTENSIONS.get(content_type)
        if suffix is None:
            entry["src"] = ""
            return
        target = thumb_dir / f"{item_id}{suffix}"
        try:
            target.write_bytes(body)
        except OSError:
            entry["src"] = ""
            return
        entry["thumb"] = target.name
        done += 1
        if done % 10 == 0 or done == total:
            log(f"  缩略图 {done}/{total}")

    await asyncio.gather(*(fetch(entry) for entry in entries))


def _warm_derived_thumbs(items, artifacts_dir: Path, log=print) -> None:
    """（保留函数名占位）派生小图的预热在 `_persist_entries` 里由
    `sticker_thumbs.ensure_many` 完成，这里不再重复实现。"""
    return None


def _persist_entries(
    entries: list[dict[str, Any]],
    artifacts_dir: Path,
    skip_tabs: set[int],
    *,
    scan_stats: dict[str, Any] | None = None,
    reset: bool = False,
    log=print,
) -> dict[str, Any]:
    """把扫到的 entries 落进表情库并写扫描统计。下载缩略图那步在外面做。

    抽出来是因为有两条路要落盘：命令行扫描（run）和常驻服务里的刷新
    （refresh_in_live_session）。两条路要是各写一份，迟早会在「哪个栏位该留哪条」
    这种细节上分叉 —— 而这正是曾经把 43 条重复记录写进库里的地方。
    """
    library = StickerLibrary.load(PROJECT_ROOT, artifacts_dir)
    before_ids = {item.id for item in library.items}
    base = [] if reset else library.items
    kept = _merge(base, entries)

    removed = 0
    if skip_tabs:
        # 光是不扫还不够：之前扫进来的记录会一直躺在库里，手表照旧显示，
        # 而它们的位置已经不可信了。
        stale = [item for item in kept if item.tab_index in skip_tabs]
        if stale:
            kept = [item for item in kept if item.tab_index not in skip_tabs]
            removed = len(stale)
            log(f"  清出 {removed} 条分类 {sorted(skip_tabs)} 的旧记录（这些栏的排列不稳定，留着会发错）")

    library.replace_all(kept)
    library.save()
    after_ids = {item.id for item in kept}

    if scan_stats is not None:
        summary_path = artifacts_dir / "stickers_scan_summary.json"
        try:
            summary_path.write_text(
                json.dumps(
                    {
                        "scanned_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                        **scan_stats,
                        "library_total": len(kept),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError as exc:
            log(f"  注意：扫描统计没能写入 {summary_path}（{exc}），核对清单会退回旧探针数据。")

    # 顺手把手表要显示的小图缩好落盘。这一步不在这里做的话，就得等手表端每请求一张
    # 才现场解码一次 —— 而原图里有 1MB+ 的动图，一次请求要几十毫秒，首屏几十张就是
    # 一两秒的等待。放在落盘之后做，成本只有这一次（几秒），而且服务重启也还在。
    thumb_sources = [path for item in kept if (path := library.thumb_file(item)) is not None]
    thumb_stats = sticker_thumbs.ensure_many(
        thumb_sources,
        sticker_thumbs.cache_dir(artifacts_dir),
        px=sticker_thumbs.WATCH_PX,
        log=log,
    )
    # 库里已经删掉的项（例如被清出去的不稳定分类），它的派生图也一并清掉。
    kept_stems = {path.stem for path in thumb_sources}
    removed_thumbs = sticker_thumbs.prune(
        sticker_thumbs.cache_dir(artifacts_dir), kept_stems, px=sticker_thumbs.WATCH_PX
    )
    if removed_thumbs:
        log(f"  清掉 {removed_thumbs} 张库里已没有的表情小图")

    return {
        "library": library,
        "before": len(before_ids),
        "items": len(kept),
        "added": len(after_ids - before_ids),
        "removed_tabs": removed,
        "with_thumb": sum(1 for item in kept if item.thumb),
        "thumbs": thumb_stats,
    }


async def _close_sticker_panel(page, log=print) -> None:
    """收起表情面板。

    必须收：面板挡在输入区上方，开着的时候后续的点击/输入都可能落在面板上
    （发送端虽然每次都自己重新点开按钮，但让服务停在一个「面板开着」的状态很容易出事）。
    先按 Esc（抖音面板支持），没收起来再点一次表情按钮（切换开关）。全程只读式操作，
    不碰任何表情项。
    """
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(300)
        if not await _panel_visible(page):
            return
        button = await first_visible(page, STICKER_BUTTONS)
        await button.click(force=True)
        await page.wait_for_timeout(300)
    except Exception:  # noqa: BLE001 - 收不起来不该让整次刷新算失败
        log("  注意：表情面板没能确认收起，下次操作前会重新点开，影响不大")


async def refresh_in_live_session(
    session,
    artifacts_dir: Path,
    *,
    indices: list[int] | None = None,
    skip_tabs: set[int] | None = None,
    log=print,
    reset: bool = False,
    should_yield=None,
) -> dict[str, Any]:
    """在**已经登录、已经打开私信页**的会话里点开表情面板，把表情收进库。

    与 `run()` 的区别：不自己开浏览器、不自己登录、不碰运行锁 ——
    所以可以由常驻的手表服务直接调用。这正是「收藏了表情，手表上自动出现」的实现方式：
    服务本来就有一个已登录的常驻页面，让它顺手刷一下，不必为了扫描把服务停掉、也不必第二次登录。

    安全性：全程只点分类栏和面板开关，**绝不点表情本体**（点一下就真发出去了）。
    并且每步之后都比对聊天区消息指纹，一旦变化立即中止。

    `should_yield` 是「现在有没有用户操作在等浏览器」的探针。刷新全程要独占页面
    十几秒，而它多半是被用户打开表情面板这一下踢起来的 —— 不设让路点的话，用户
    紧接着的「发送」要排在整段刷新后面。抛 `RefreshYielded` 时本次什么都还没落盘，
    调用方过一会儿重排即可。
    """
    scanner_skip = set(DEFAULT_SKIP_TABS) if skip_tabs is None else set(skip_tabs)
    page = session.page
    baseline = await page.evaluate(_MESSAGE_FINGERPRINT_JS)

    if should_yield is not None and should_yield():
        raise RefreshYielded("有界面请求在等浏览器，本轮刷新让路")

    log("→ 点开表情面板（只点这一个按钮）…")
    button = await first_visible(page, STICKER_BUTTONS)
    await button.click(force=True)
    await _wait_or_yield(page, 1200, should_yield)
    if not await _panel_visible(page):
        raise RuntimeError("表情面板没有出现，抖音可能改版了（检查 STICKER_PANELS 选择器）")

    try:
        entries, scan_stats = await _collect_tab_entries(
            page, indices, log=log, skip_tabs=scanner_skip, should_yield=should_yield
        )
        if await page.evaluate(_MESSAGE_FINGERPRINT_JS) != baseline:
            raise RuntimeError("扫描过程中聊天区消息数变了，已中止（可能有表情被发了出去）")
    finally:
        await _close_sticker_panel(page, log=log)

    if not entries:
        log("× 没有从任何分类里解析出表情，库未改动。")
        return {"items": 0, "added": 0, "changed": False, "per_tab": {}}

    thumb_dir = artifacts_dir / "stickers"
    thumb_dir.mkdir(parents=True, exist_ok=True)
    usable = [entry for entry in entries if entry["src"]]
    if len(usable) < len(entries):
        log(f"  其中 {len(entries) - len(usable)} 个没有可用图片，手表上将显示成文字按钮")
    if usable:
        # 下载是网络活儿，但同样占着这条常驻会话 —— 让路点要一直留着
        if should_yield is not None and should_yield():
            raise RefreshYielded("有界面请求在等浏览器，本轮刷新让路")
        await _download_thumbs(session, usable, thumb_dir, log=log)

    result = _persist_entries(entries, artifacts_dir, scanner_skip, scan_stats=scan_stats, reset=reset, log=log)
    result["per_tab"] = scan_stats.get("per_tab") or {}
    result["changed"] = bool(result["added"] or result["removed_tabs"])
    return result


def _is_positional(item: LibraryItem) -> bool:
    """这一项是不是「只能按序号发」的那类（自建/收藏，没有名字）。

    判据是它自己的 name 是否等于按位置生成的那个规范名（`tab{栏}-{栏内序号+1}`）。
    不用额外字段标记是有意的：老库文件里没有这个字段，而 name 一直在被
    `_merge` 同步刷新，所以拿它反推身份永远自洽。
    """
    if item.tab_index is None or item.fallback_index is None:
        return False
    return item.name == f"tab{item.tab_index}-{item.fallback_index + 1}"


def _merge(library_items: list[LibraryItem], entries: list[dict[str, Any]]) -> list[LibraryItem]:
    """把扫描结果并进已有库。

    已有项优先保留「用户改过的东西」：显示名、启用开关、手动设的分类。
    扫描只负责更新名字、缩略图和新增条目 —— 重扫一次不该把手工调整冲掉。

    关于「同一位置堆积多条」：无名字表情的 id 由**图片资源名**派生，而它的
    发送坐标是「第几栏第几个」。两者并不总是一致 —— 实测同一个栏位在两次会话里
    指向了不同的图（tab3 就是这样），于是按 id 去重会把它当成新项追加，
    同一个栏位越扫越多条，手表网格里出现两个都叫「8」但图不同的格子，
    而真正能点到的只有那一个位置。
    所以这类项改为**按栏位去重**：同一个 (栏, 位置) 只保留本次扫到的那条，
    旧的那条连同它过期的图一起让位。栏位才是它唯一的身份。
    """
    by_id: dict[str, LibraryItem] = {}
    order: list[str] = []
    for item in library_items:
        by_id[item.id] = item
        order.append(item.id)

    # 本次扫描实际写到过的 id。收尾去重时用它决定「同一栏位留哪一条」。
    written: set[str] = set()

    for entry in entries:
        item_id = make_item_id(entry["uid"], entry["category"])
        thumb = entry.get("thumb")
        existing = by_id.get(item_id)
        if existing is None:
            by_id[item_id] = LibraryItem(
                id=item_id,
                name=entry["name"],
                label=entry["label"],
                category=entry["category"],
                fallback_index=entry.get("fallback_index"),
                tab_index=entry.get("tab_index"),
                source_key=entry.get("source_key"),
                thumb=thumb,
            )
            order.append(item_id)
            written.add(item_id)
            continue
        existing.thumb = thumb or existing.thumb
        written.add(item_id)
        # 定位信息是扫描算出来的、不是人改的，每次重扫都要刷新：
        # 抖音那边插一个表情，后面所有项的栏内序号都会顺移，不刷新就会点错。
        existing.tab_index = entry.get("tab_index")
        existing.fallback_index = entry.get("fallback_index")
        existing.source_key = entry.get("source_key") or existing.source_key
        # 自建表情在手表上的名字和显示名都是序号，同样要跟着刷新；
        # 有名字的那些保留用户可能改过的显示名。
        # name 不刷新会撞名：新表情插进面板后，旧条目的序号全顺移了，
        # 而新条目按它的位置生成的 name 和某个旧条目的一模一样 ——
        # 手表端按 name 去重，撞名的那个直接消失；按 name 发送也会发错。
        if entry.get("auto_label"):
            existing.name = entry["name"]
        if entry.get("auto_label") or not existing.label:
            existing.label = entry["label"]

    merged = [by_id[item_id] for item_id in order]

    # 收尾去重：同一个栏位只能留一条「按序号发」的项。
    # 留谁：优先留本次扫描写到的 —— 那才是面板此刻的真实内容；两条都不是本次扫到的
    # （历史遗留的堆积）则留先出现的。
    # 这里刻意放在收尾统一处理，而不是在写入时抢占栏位：一个表情可能从第 5 个挪到第 9 个，
    # 写入时抢占会把「已经搬走的旧条目」误删 —— 收尾时它已经在新栏位上重新登记过了。
    kept: list[LibraryItem] = []
    slot_taken: dict[tuple[int, int], int] = {}
    dropped: list[LibraryItem] = []
    for item in merged:
        if not _is_positional(item):
            kept.append(item)
            continue
        slot = (item.tab_index, item.fallback_index)  # type: ignore[arg-type]
        at = slot_taken.get(slot)
        if at is None:
            slot_taken[slot] = len(kept)
            kept.append(item)
            continue
        incumbent = kept[at]
        if item.id in written and incumbent.id not in written:
            kept[at] = item
            dropped.append(incumbent)
        else:
            dropped.append(item)

    if dropped:
        print(
            f"  栏位去重：{len(dropped)} 条旧记录被同一栏位的新内容顶掉"
            f"（无名字表情只能按「第几栏第几个」定位，一个栏位留一条）"
        )

    # 面板里的真实顺序就是（第几栏, 栏内第几个）。按内容匹配回来的条目还排在
    # 老面板的顺序里，新插队的表情被 append 到末尾 —— 重排一次，网格顺序
    # 才和抖音面板一致（新加的收藏表情出现在它该在的位置，而不是最末尾）。
    kept.sort(key=_panel_order)
    return kept


def _panel_order(item: LibraryItem) -> tuple[int, int]:
    tab = item.tab_index if item.tab_index is not None else 1 << 30
    pos = item.fallback_index if item.fallback_index is not None else 1 << 30
    return (tab, pos)


def _allowed_media_url(url: str) -> bool:
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return False
    host = host.lower()
    if not host:
        return False
    return any(host == allowed.lstrip(".") or host.endswith(allowed) for allowed in ALLOWED_MEDIA_HOSTS)


def _friends_from_config(path: Path) -> list[str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    friends = raw.get("friends") if isinstance(raw, dict) else None
    if isinstance(friends, list):
        return [str(name).strip() for name in friends if str(name).strip()]
    return []


async def _panel_visible(page) -> bool:
    for selector in STICKER_PANELS:
        try:
            if await page.locator(selector).first.is_visible():
                return True
        except Exception:
            continue
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="扫描抖音表情面板，生成手表端表情库")
    parser.add_argument("--friend", default="", help="用哪个会话打开表情面板（默认 config.json 里的第一个好友）")
    parser.add_argument("--probe", action="store_true", help="只 dump 面板结构，不写配置")
    parser.add_argument("--tabs", action="store_true", help="只 dump 分类栏结构，不点击任何东西")
    parser.add_argument(
        "--click-tabs",
        action="store_true",
        help="逐个点开分类栏并 dump 每栏内容（带消息数守卫，只点分类不点表情）",
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="只校验库里记的「第几栏+第几个」还指得对（只读，不点表情，不写配置）",
    )
    parser.add_argument(
        "--scan-tabs",
        default=ALL_TABS,
        help=(
            "要扫的分类栏序号，逗号分隔；默认 all = 扫全部非空分类"
            f"（但会跳过 {','.join(str(i) for i in DEFAULT_SKIP_TABS)}，原因见 --include-tabs）。"
            "传空字符串走老路（只扫面板打开时默认那一屏）。序号见 --click-tabs 的输出。"
        ),
    )
    parser.add_argument(
        "--include-tabs",
        default="",
        help=(
            "强制把默认跳过的分类栏也收进来，逗号分隔。"
            "默认跳过的栏（见 DEFAULT_SKIP_TABS）排列不稳定：同一个栏位在不同会话里会指向"
            "不同的图，而这类表情只能按序号发送 —— 收了就可能「显示的是这个、发出去的是那个」。"
            "只在明确知道风险、想看看那一栏有什么时才用。"
        ),
    )
    parser.add_argument("--reset", action="store_true", help="丢掉旧库，完全按本次扫描结果重建")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
