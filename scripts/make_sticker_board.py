"""把表情库渲染成一张可视化清单，用来肉眼核对「手表上该看到哪些表情」。

为什么需要它：手表屏幕小、网格要滚动，肉眼在表上清点 66 个格子不现实。
这个页面在电脑上打开，一屏就能看出：

- 库里到底有哪些表情、分在哪几个面板分类里
- 哪些表情本地有缩略图（手表上能显示成图）、哪些只能显示成文字格
- **面板里实际有、但没被扫进库的分类**（用远程图试着加载，过期就标出来）

最后一条是关键：扫描是按分类栏点着扫的，漏扫某一栏时，那一栏的表情
在手表上「凭空消失」，而库文件本身看不出任何异常。

用法（在项目根目录）：

    python scripts/make_sticker_board.py            # 写到 artifacts/sticker_board.html
    python scripts/make_sticker_board.py --open     # 顺手用默认浏览器打开
"""

from __future__ import annotations

import argparse
import base64
import html
import io
import json
import pathlib
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
LIBRARY_PATH = PROJECT_ROOT / "watch_stickers.json"
THUMB_DIR = PROJECT_ROOT / "artifacts" / "stickers"
TABS_CONTENT_PATH = PROJECT_ROOT / "artifacts" / "stickers_tabs_content.json"
SCAN_SUMMARY_PATH = PROJECT_ROOT / "artifacts" / "stickers_scan_summary.json"
OUTPUT_PATH = PROJECT_ROOT / "artifacts" / "sticker_board.html"

# 内嵌缩略图的目标边长。表情库原图合起来有 7MB 量级，直接内嵌会撑成
# 十几 MB 的 HTML；缩到 88px 后每张几 KB，整个页面 300KB 上下，可以随便发人。
EMBED_PX = 88


def _embed_thumb(path: pathlib.Path) -> str | None:
    """把缩略图缩到 EMBED_PX 并编码成 data URI。

    为什么要内嵌：本地 HTML 用相对路径引用 artifacts/stickers/ 时，
    只有「双击用 file:// 打开」才认。经任何 http 预览打开都会 404，
    用户看到一片裂图还以为表情库是空的。内嵌之后怎么打开都对。

    动图会被压成静止的第一帧 —— 这张清单只用来「认得出是哪个表情」，够了。
    Pillow 没装则返回 None，调用方退回相对路径。
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(path) as image:
            image.load()
            image.thumbnail((EMBED_PX, EMBED_PX), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            if image.mode in ("RGBA", "LA", "P"):
                image.convert("RGBA").save(buffer, format="PNG", optimize=True)
                mime = "image/png"
            else:
                image.convert("RGB").save(buffer, format="JPEG", quality=82, optimize=True)
                mime = "image/jpeg"
    except Exception:  # noqa: BLE001 - 单张图坏了不该拖垮整个清单
        return None
    body = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:{mime};base64,{body}"



def _load_library() -> list[dict]:
    if not LIBRARY_PATH.is_file():
        raise SystemExit(f"没找到表情库：{LIBRARY_PATH}\n先跑一次 scripts/scan_stickers.py。")
    raw = json.loads(LIBRARY_PATH.read_text(encoding="utf-8"))
    items = raw.get("items") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise SystemExit("表情库格式不对（items 不是数组）。")
    return items


def _load_scan_summary() -> dict | None:
    """读上一次扫描落下的统计（scan_stickers.py 写的）。

    它比 stickers_tabs_content.json 可靠：那份探针产物只有手动跑 --click-tabs 才刷新，
    实际往往是几天前的；而这份是每次扫描都重写，还自带 scanned_at 时间戳。
    「面板里有几项」要跟库里的项数对比，用过期数字比出来的差异全是假警报。
    """
    if not SCAN_SUMMARY_PATH.is_file():
        return None
    try:
        raw = json.loads(SCAN_SUMMARY_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return raw if isinstance(raw, dict) else None


def _load_probe() -> list[dict]:
    """读分类探针产物，拿到「面板里实际有」的那些分类。没有就返回空。"""
    if not TABS_CONTENT_PATH.is_file():
        return []
    try:
        raw = json.loads(TABS_CONTENT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    out: list[dict] = []
    for tab in raw.get("tabs") or []:
        content = tab.get("content") or {}
        items = content.get("items") or []
        out.append(
            {
                "index": tab.get("index"),
                "items": items,
                "sent_guard": tab.get("message_sent_guard"),
            }
        )
    return out


def _esc(text: object) -> str:
    return html.escape(str(text), quote=True)


def _thumb_cell(item: dict, lib_index: int) -> str:
    thumb = item.get("thumb") or ""
    label = item.get("label") or item.get("name") or "?"
    name = item.get("name") or ""
    item_id = item.get("id") or ""
    tab_index = item.get("tab_index")
    fallback = item.get("fallback_index")

    if thumb and (THUMB_DIR / pathlib.Path(thumb).name).is_file():
        path = THUMB_DIR / pathlib.Path(thumb).name
        embedded = _embed_thumb(path)
        # 内嵌成功就用 data URI；Pillow 缺失时退回相对路径（只有 file:// 打开才认）。
        src = embedded if embedded else f"stickers/{_esc(path.name)}"
        media = f'<img src="{src}" alt="{_esc(label)}" loading="lazy">'
        badge = ""
    else:
        media = f'<div class="nopic">{_esc(label[:4])}</div>'
        badge = '<span class="warn">无图</span>'

    pos = ""
    if tab_index is not None and fallback is not None:
        pos = f"分类{int(tab_index)}·第{int(fallback) + 1}个"

    return f"""    <figure class="cell">
      <div class="box">{media}{badge}</div>
      <figcaption>
        <b>#{lib_index + 1} {_esc(label)}</b>
        <span class="meta">{_esc(name)}</span>
        <span class="meta">{_esc(pos)}</span>
        <span class="meta dim">{_esc(item_id)}</span>
      </figcaption>
    </figure>"""


def _probe_cell(row: dict, order: int) -> str:
    """面板里实际有、但不在库里的表情。图是远程的，签名过期会加载失败。"""
    src = str(row.get("img") or "").strip()
    desc = str(row.get("desc") or "").strip()
    label = desc or f"第{int(row.get('i', order - 1)) + 1}个"
    if src:
        media = (
            f'<img src="{_esc(src)}" alt="{_esc(label)}" loading="lazy" '
            f'onerror="this.parentNode.classList.add(\'dead\')">'
        )
    else:
        media = '<div class="nopic">?</div>'
    return f"""    <figure class="cell missing">
      <div class="box">{media}</div>
      <figcaption>
        <b>#{order} {_esc(label)}</b>
        <span class="meta dim">未入库</span>
      </figcaption>
    </figure>"""


def build_html(items: list[dict], probes: list[dict], summary: dict | None = None) -> str:
    in_lib_tabs = {item.get("tab_index") for item in items}
    total_bytes = 0
    for item in items:
        thumb = item.get("thumb")
        if thumb:
            path = THUMB_DIR / pathlib.Path(thumb).name
            if path.is_file():
                total_bytes += path.stat().st_size

    # 按面板分类分组，顺序按 tab_index。
    groups: dict[object, list[dict]] = {}
    for item in items:
        groups.setdefault(item.get("tab_index"), []).append(item)
    ordered_tabs = sorted(groups, key=lambda t: (t is None, t if t is not None else 0))

    sections: list[str] = []
    for tab_index in ordered_tabs:
        rows = groups[tab_index]
        named = sum(1 for r in rows if not str(r.get("name") or "").startswith("tab"))
        title = "分类 0（内置表情，有名字）" if tab_index == 0 else f"分类 {tab_index}"
        sections.append(
            f"""  <section>
    <h2>{_esc(title)} <span class="count">{len(rows)} 项</span>
      <span class="hint">其中 {named} 项带名字、可直接按名字发送</span></h2>
    <div class="grid">
{chr(10).join(_thumb_cell(r, i) for i, r in enumerate(rows))}
    </div>
  </section>"""
        )

    # 「面板里实际有几项」的来源，按可信度排序：
    #   1. 本次扫描统计（每次扫描都重写，自带时间戳）
    #   2. 分类探针产物（只有手动 --click-tabs 才刷新，往往已过期）
    # 拿过期数字跟新库对比，会凭空长出「库里多了一项 / 少了一项」的假差异。
    panel_rows: list[dict] = []
    if summary and summary.get("per_tab"):
        per_tab = {int(k): int(v) for k, v in summary["per_tab"].items()}
        disabled = {int(i) for i in (summary.get("disabled") or [])}
        total_tabs = int(summary.get("total_tabs") or max(per_tab, default=-1) + 1)
        for index in range(total_tabs):
            if index in per_tab:
                panel_rows.append({"index": index, "count": per_tab[index], "disabled": False})
            elif index in disabled:
                panel_rows.append({"index": index, "count": 0, "disabled": True})
        panel_source = f"本次扫描（{summary.get('scanned_at') or '时间未知'}）"
    elif probes:
        import datetime as _dt

        stamp = _dt.datetime.fromtimestamp(TABS_CONTENT_PATH.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        panel_rows = [
            {"index": int(p["index"]), "count": len(p["items"]), "disabled": False}
            for p in probes
            if p.get("index") is not None
        ]
        panel_source = f"分类探针产物（{stamp} 生成，可能已过期 —— 重跑一次扫描可刷新）"
    else:
        panel_source = ""

    # 面板里有内容、库里却一项都没有的分类 —— 「扫漏了」的直接证据。
    probe_items_by_tab = {int(p["index"]): p["items"] for p in probes if p.get("index") is not None}
    missed: list[str] = []
    for row in panel_rows:
        tab_index = row["index"]
        cnt = row["count"]
        if not cnt or tab_index in in_lib_tabs:
            continue
        items_in_tab = probe_items_by_tab.get(tab_index) or []
        if items_in_tab:
            body = f"""    <div class="grid">
{chr(10).join(_probe_cell(r, i + 1) for i, r in enumerate(items_in_tab))}
    </div>"""
        else:
            body = f'    <p class="note">本次扫描看到 {cnt} 项，但没有留下缩略图，无法在这里预览。</p>'
        missed.append(
            f"""  <section class="danger">
    <h2>分类 {_esc(tab_index)}：面板里有 <span class="count">{cnt} 项</span>，
      <span class="bad">表情库里一项都没有</span></h2>
    <p class="note">这一栏从来没被扫到过。它的表情在手表上不会出现。重跑一次
       <code>python scripts/scan_stickers.py</code> 就能补上。</p>
{body}
  </section>"""
        )

    panel_summary = []
    for row in panel_rows:
        cnt = row["count"]
        if row["disabled"] and not cnt:
            flag = "无内容"
        else:
            flag = "已入库" if row["index"] in in_lib_tabs else ("未入库" if cnt else "空")
        panel_summary.append(f"分类 {row['index']}：{cnt} 项 → {flag}")
    probe_line = "　｜　".join(panel_summary) if panel_summary else "（没有扫描统计，也没有分类探针产物）"
    if panel_source:
        probe_line = f"{probe_line}　（来源：{panel_source}）"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>手表表情库一览（{len(items)} 项）</title>
<style>
  :root {{ color-scheme: light; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 28px 32px 64px;
    background: #F5F7FA; color: #1B1F24;
    font: 14px/1.6 "Microsoft YaHei", "PingFang SC", system-ui, sans-serif;
  }}
  h1 {{ font-size: 22px; margin: 0 0 6px; }}
  h2 {{ font-size: 15px; margin: 0 0 14px; font-weight: 600; }}
  .sub {{ color: #5C6672; margin: 0 0 20px; }}
  .stats {{
    display: flex; flex-wrap: wrap; gap: 10px; margin: 0 0 22px;
  }}
  .stat {{
    background: #fff; border: 1px solid #E3E7EC; border-radius: 10px;
    padding: 10px 16px; min-width: 120px;
  }}
  .stat b {{ display: block; font-size: 20px; }}
  .stat span {{ color: #6B7480; font-size: 12px; }}
  .alert {{
    background: #FFF4E5; border: 1px solid #FFD8A8; border-left: 4px solid #F08C00;
    border-radius: 8px; padding: 12px 16px; margin: 0 0 24px; color: #7A4A00;
  }}
  .alert b {{ color: #A34F00; }}
  section {{ margin: 0 0 32px; }}
  section.danger h2 {{ color: #B02A2A; }}
  .count {{
    background: #E8EDF3; color: #3A4653; border-radius: 20px;
    padding: 1px 10px; font-size: 12px; font-weight: 400;
  }}
  .bad {{ color: #C92A2A; }}
  .hint, .note {{ color: #6B7480; font-size: 12px; font-weight: 400; }}
  .note {{ margin: -6px 0 14px; }}
  .grid {{
    display: grid; gap: 12px;
    grid-template-columns: repeat(auto-fill, minmax(108px, 1fr));
  }}
  figure.cell {{
    margin: 0; background: #fff; border: 1px solid #E3E7EC;
    border-radius: 10px; padding: 10px; position: relative;
  }}
  figure.missing {{ border-style: dashed; background: #FFFBFB; }}
  .box {{
    position: relative; display: flex; align-items: center; justify-content: center;
    height: 92px; background: #F0F3F7; border-radius: 8px; overflow: hidden;
  }}
  .box img {{ max-width: 100%; max-height: 100%; object-fit: contain; }}
  .box.dead::after {{
    content: "图已过期"; color: #9AA3AD; font-size: 11px;
    position: absolute; inset: 0; display: flex; align-items: center; justify-content: center;
  }}
  .box.dead img {{ display: none; }}
  .nopic {{
    color: #8A939E; font-size: 16px; font-weight: 600;
    max-width: 100%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }}
  .warn {{
    position: absolute; top: 4px; right: 4px; background: #FFF0F0;
    color: #C92A2A; border-radius: 4px; font-size: 10px; padding: 0 5px;
  }}
  figcaption {{ margin-top: 8px; display: flex; flex-direction: column; gap: 1px; }}
  figcaption b {{ font-size: 13px; }}
  .meta {{ color: #5C6672; font-size: 11px; }}
  .meta.dim {{ color: #9AA3AD; font-family: Consolas, monospace; }}
</style>
</head>
<body>
  <h1>手表表情库一览</h1>
  <p class="sub">库文件 watch_stickers.json　·　缩略图 artifacts/stickers/　·　手表面上看到的就是这些格子</p>

  <div class="stats">
    <div class="stat"><b>{len(items)}</b><span>库里表情总数</span></div>
    <div class="stat"><b>{len(in_lib_tabs)}</b><span>覆盖的面板分类</span></div>
    <div class="stat"><b>{sum(1 for i in items if i.get("thumb"))}</b><span>带缩略图</span></div>
    <div class="stat"><b>{total_bytes / 1024:.0f} KB</b><span>缩略图原图总量</span></div>
  </div>

  <div class="alert">
    <b>关键对照</b>：扫到的分类 —— {_esc(probe_line)}<br>
    下面<b>红框</b>标出的分类是「面板里有、表情库里没有」，
    这些表情在手表上不会出现。要补进来就重跑扫描并带上那些分类号。
  </div>

{chr(10).join(sections)}
{chr(10).join(missed) if missed else '  <section><p class="note">没有发现漏扫的分类（或缺少探针产物，先跑一次 scan_stickers.py --click-tabs）。</p></section>'}
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="生成表情库可视化清单")
    parser.add_argument("--open", action="store_true", help="生成后用默认浏览器打开")
    parser.add_argument("--out", default=str(OUTPUT_PATH), help="输出路径")
    args = parser.parse_args()

    items = _load_library()
    probes = _load_probe()
    summary = _load_scan_summary()
    page = build_html(items, probes, summary)

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")

    lib_tabs = sorted({i.get("tab_index") for i in items}, key=lambda t: (t is None, t or 0))
    size_kb = out.stat().st_size / 1024
    embedded = page.count("data:image/")
    print(f"✓ 已写入 {out}（{size_kb:.0f} KB，内嵌 {embedded} 张缩略图）")
    if embedded == 0:
        print("  提示：Pillow 没装，缩略图走的是相对路径 —— 需要用文件管理器双击打开才看得见图。")
    print(f"  库里 {len(items)} 项，覆盖分类 {lib_tabs}")
    if summary and summary.get("per_tab"):
        print(f"  面板对照（本次扫描 {summary.get('scanned_at') or '时间未知'}）：")
        per_tab = {int(k): int(v) for k, v in summary["per_tab"].items()}
        for index in sorted(per_tab):
            cnt = per_tab[index]
            in_lib = sum(1 for i in items if i.get("tab_index") == index)
            mark = "已入库" if index in set(lib_tabs) else ("未入库 ← 漏扫" if cnt else "空")
            extra = f"（库里 {in_lib} 项）" if index in set(lib_tabs) else ""
            print(f"    分类 {index}：面板 {cnt} 项 {mark}{extra}")
    else:
        print("  提示：没有扫描统计，面板对照退回旧探针产物，数字可能已过期。")
        for probe in probes:
            cnt = len(probe["items"])
            mark = "已入库" if int(probe["index"]) in set(lib_tabs) else ("未入库 ← 漏扫" if cnt else "空")
            print(f"  面板分类 {probe['index']}：{cnt} 项  {mark}")

    if args.open:
        import webbrowser

        webbrowser.open(out.resolve().as_uri())
    return 0


if __name__ == "__main__":
    sys.exit(main())
