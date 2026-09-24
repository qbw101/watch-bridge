"""量一量「手表拉一张表情图」在服务端要花多少时间。

手表端拉表情的链路是：
    手表发一次 op=image  →  服务端读本地缩略图 → 解码 → LANCZOS 缩到 72px
    → 重新编码 → AES 加密 → 传回

这条链上服务端能优化的只有「解码 + 缩放 + 编码」和「传多少字节」。
本脚本只读不写：不改库文件、不动缩略图，只是把同一套 `_downscale` 拿来
对着现成的图跑一遍，好知道优化空间到底有多大。

默认只测原图（artifacts/stickers/）。加 `--compare` 会把同一批表情的
「原图」和「派生小图」（artifacts/sticker_thumbs/）并排跑一遍 —— 这两个
数字的差就是「把缩放从每次请求挪成一次落盘」省下来的那部分。

用法：
    python scripts/bench_sticker_images.py              # 原图，全部
    python scripts/bench_sticker_images.py --compare    # 原图 vs 派生小图
    python scripts/bench_sticker_images.py --px 72      # 指定目标边长
    python scripts/bench_sticker_images.py --limit 20   # 只看前 20 张
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bridge import sticker_thumbs  # noqa: E402
from bridge.sticker_store import StickerLibrary  # noqa: E402
from bridge.tcp_server import STICKER_PX, _downscale  # noqa: E402

ARTIFACTS_DIR = PROJECT_ROOT / "artifacts"

# 后缀 → MIME，和服务端 `MIME_BY_SUFFIX` 保持同一套判断，避免量出来的耗时
# 和真实请求走的代码路径不一样。
MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def _describe(path: Path) -> dict:
    """读图的元信息：格式、尺寸、是不是动图。"""
    try:
        from PIL import Image
    except ImportError:
        return {"format": "?", "size": (0, 0), "frames": 1}
    try:
        with Image.open(path) as image:
            frames = getattr(image, "n_frames", 1)
            return {"format": image.format or "?", "size": image.size, "frames": frames}
    except Exception:
        return {"format": "?", "size": (0, 0), "frames": 1}


def collect_sources(limit: int = 0) -> list[tuple[Path, Path]]:
    """列出库里每个表情的 (原图, 派生小图)。

    只取库里引用的项，不直接扫目录 —— 否则会拿「库里已经删掉、但文件还在」
    的旧图参与平均，两个数据源的数量也就对不上了。
    """
    library = StickerLibrary.load(PROJECT_ROOT, ARTIFACTS_DIR)
    cache = sticker_thumbs.cache_dir(ARTIFACTS_DIR)
    pairs: list[tuple[Path, Path]] = []
    for item in library.items:
        original = library.thumb_file(item)
        if original is None:
            continue
        pairs.append((original, sticker_thumbs.derived_path(original, cache, sticker_thumbs.WATCH_PX)))
    if limit:
        pairs = pairs[:limit]
    return pairs


def measure(pairs: list[tuple[Path, Path]], px: int, label: str, warmup: bool = False) -> dict:
    """对一组图跑 `_downscale`，返回耗时与体积统计。"""
    rows: list[dict] = []
    durations: list[float] = []
    total_before = total_after = 0

    for source, derived in pairs:
        path = derived if label == "派生小图" else source
        if not path.is_file():
            continue
        body = path.read_bytes()
        content_type = MIME_BY_SUFFIX.get(path.suffix.lower(), "image/png")

        if warmup:
            # 先空跑一次把「首次导入 PIL / 解码器初始化」的固定开销排除掉，
            # 否则第一张会把这部分算进去，两个数据源起步条件就不一样了。
            _downscale(content_type, body, px)

        started = time.perf_counter()
        out_type, out_body = _downscale(content_type, body, px)
        elapsed = time.perf_counter() - started

        before, after = len(body), len(out_body)
        total_before += before
        total_after += after
        durations.append(elapsed)
        rows.append(
            {
                "name": source.name,
                "src_size": _describe(path),
                "before": before,
                "after": after,
                "ms": elapsed * 1000,
                "out_type": out_type,
            }
        )

    rows.sort(key=lambda r: -r["ms"])
    return {
        "label": label,
        "rows": rows,
        "count": len(rows),
        "total_ms": sum(durations),
        "mean_ms": statistics.mean(durations) * 1000 if durations else 0,
        "median_ms": statistics.median(durations) * 1000 if durations else 0,
        "max_ms": max(durations) * 1000 if durations else 0,
        "bytes_before": total_before,
        "bytes_after": total_after,
        # 首屏（最慢的 12 张）的服务端处理时间，这是用户最直观感受到的那一段。
        "first_screen_ms": sum(sorted(durations, reverse=True)[:12]) * 1000,
    }


def _print_detail(result: dict) -> None:
    print(f"\n【{result['label']}】逐张明细（慢的在前）")
    print(f"{'文件':<24}{'格式':<7}{'尺寸':<12}{'帧':<5}{'原大小':>9}{'耗时':>10}{'缩后':>9}")
    print("-" * 80)
    for row in result["rows"][:15]:
        meta = row["src_size"]
        w, h = meta["size"]
        print(
            f"{row['name'][:23]:<24}{meta['format']:<7}{f'{w}x{h}':<12}{meta['frames']:<5}"
            f"{row['before'] / 1024:>8.1f}K{row['ms']:>9.1f}ms{row['after'] / 1024:>8.1f}K"
        )
    if result["count"] > 15:
        print(f"  …还有 {result['count'] - 15} 张")


def _print_summary(result: dict) -> None:
    r = result
    print(
        f"  {r['label']:<8} 共 {r['count']:>3} 张  "
        f"总耗时 {r['total_ms']:>6.2f}s（平均 {r['mean_ms']:>5.1f}ms，中位 {r['median_ms']:>5.1f}ms，"
        f"最慢 {r['max_ms']:>6.1f}ms）  "
        f"传输 {r['bytes_before'] / 1024 / 1024:>5.2f}MB→{r['bytes_after'] / 1024 / 1024:.2f}MB"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="表情图降采样耗时基准")
    parser.add_argument("--px", type=int, default=STICKER_PX, help=f"目标最长边，默认 {STICKER_PX}")
    parser.add_argument("--limit", type=int, default=0, help="只测前 N 张（0 = 全部）")
    parser.add_argument("--compare", action="store_true", help="原图与派生小图并排对比")
    args = parser.parse_args()

    pairs = collect_sources(args.limit)
    if not pairs:
        print("表情库里没有可测的图，先跑一次扫描。")
        return 1

    print(f"库中 {len(pairs)} 个表情，输出目标边长 {args.px}px\n")

    labels = ["原图", "派生小图"] if args.compare else ["原图"]
    results = [measure(pairs, args.px, label, warmup=True) for label in labels]

    for result in results:
        _print_detail(result)

    print("\n" + "=" * 80)
    print("汇总")
    for result in results:
        _print_summary(result)

    if len(results) == 2:
        old, new = results
        if new["total_ms"] > 0:
            print(
                f"\n  服务端处理提速 {old['total_ms'] / new['total_ms']:.1f}x"
                f"（{old['total_ms']:.2f}s → {new['total_ms']:.2f}s）"
            )
        if new["bytes_before"]:
            print(
                f"  读取体积减少 {(1 - new['bytes_before'] / old['bytes_before']) * 100:.0f}%"
                f"（{old['bytes_before'] / 1024 / 1024:.2f}MB → {new['bytes_before'] / 1024 / 1024:.2f}MB）"
            )
        print(
            f"  首屏（最慢 12 张）{old['first_screen_ms']:.0f}ms → {new['first_screen_ms']:.0f}ms"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
