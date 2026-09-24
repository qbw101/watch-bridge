"""把手表要显示的表情图预先缩成小图，落盘缓存。

为什么需要这一层：

抖音的表情原图里有大量动图 webp —— 实测最大的 1267KB / 45 帧、957KB / 71 帧、
800x762 / 27 帧，而手表上它只是一个 70 来 px 的静态格子。服务端原先每次收到
`op=image` 都拿原图现解码、现缩放，实测平均 24.6ms、最慢 961ms；67 个表情的首屏
光服务端就要 1.9 秒。更糟的是那份内存缓存一重启就没了 —— 手表每次连上都要重等。

所以这里把「缩放」从「每次请求」挪成「一次落盘」：

- 派生图存 `artifacts/sticker_thumbs/`，与 `artifacts/stickers/`（原图）平级，
  两者互不干扰；原图一律保留，将来手表要更大尺寸、或核对清单要出图，都还得靠它。
- 派生图是纯缓存，**随时可以整个删掉**，删了会按需重新生成。
- 命中之后服务端只要读一个十几 KB 的小文件，几十毫秒变一毫秒。

动图只取第一帧：手表是静态格子，解 90 帧再丢掉 89 帧纯属浪费。
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

LOGGER = logging.getLogger("douyin_watch")

# 派生图是**按客户端实际要的尺寸分别缓存的**（文件名里带尺寸），所以这里没有
# 「唯一的目标边长」，只有两个边界：
#
# - WATCH_PX：手表端默认要的尺寸，和服务端 `tcp_server.STICKER_PX` 是同一个值。
#   扫描落盘时按它预生成一批，让手表第一次打开就是热的。两处改了要一起改。
# - MAX_PX：派生图上限。客户端要的比它还大，说明它想要大图，这时派生图帮倒忙
#   （还得放大），不如直接把原图给它。
WATCH_PX = 72
MAX_PX = 256

CACHE_DIRNAME = "sticker_thumbs"
_SUFFIX = ".png"


def cache_dir(artifacts_dir: Path) -> Path:
    """派生小图的存放目录。"""
    return artifacts_dir / CACHE_DIRNAME


def derived_path(source: Path, cache: Path, px: int) -> Path:
    """派生小图的路径。

    文件名带上目标边长：不同尺寸可以共存，改了尺寸也不会误用旧档位的缓存。
    """
    return cache / f"{source.stem}.{px}{_SUFFIX}"


def _fresh(source: Path, target: Path) -> bool:
    """派生图还在、且不比原图旧，就算命中。

    比 mtime 是为了让「重扫一次」不用把所有图重缩一遍 —— 扫描只重写变化的那几张，
    其余原图 mtime 不动，派生图自然继续命中。
    """
    try:
        return target.stat().st_mtime >= source.stat().st_mtime
    except OSError:
        return False


def _render(source: Path, target: Path, px: int) -> bool:
    """把 source 缩成最长边 px 的 PNG 写到 target。成功返回 True。"""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - 依赖缺失时退化成用原图
        LOGGER.warning("没装 Pillow，无法生成表情小图（pip install Pillow），将退回原图")
        return False

    try:
        with Image.open(source) as image:
            # 动图只取第一帧：手表是静态格子，后面的帧解出来也没人要。
            image.seek(0)
            has_alpha = image.mode in ("RGBA", "LA", "P") or "transparency" in image.info
            frame = image.convert("RGBA" if has_alpha else "RGB")
    except Exception:  # noqa: BLE001 - 单张图坏了不该影响其它表情
        LOGGER.debug("表情图解码失败：%s", source, exc_info=True)
        return False

    try:
        ratio = px / float(max(frame.size))
        if ratio < 1:
            size = (max(1, round(frame.width * ratio)), max(1, round(frame.height * ratio)))
            frame = frame.resize(size, Image.Resampling.LANCZOS)

        target.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再原子替换：多个请求线程同时给同一张图生成时，
        # 谁先写完谁生效，其余的结果一样，不存在读到半截文件的情况。
        fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".tmp-", suffix=_SUFFIX)
        os.close(fd)
        try:
            frame.save(tmp_name, format="PNG", optimize=True)
            os.replace(tmp_name, target)
        except Exception:  # noqa: BLE001
            Path(tmp_name).unlink(missing_ok=True)
            raise
    except Exception:  # noqa: BLE001 - 存不下来就当没生成，服务端会退回原图
        LOGGER.debug("表情小图写入失败：%s", target, exc_info=True)
        return False
    return True


def ensure(source: Path, cache: Path, px: int = WATCH_PX, *, force: bool = False) -> tuple[Path, bool]:
    """确保派生图存在，返回 (路径, 是否本次新生成)。

    生成失败时返回的路径可能并不存在 —— 调用方读之前要自己确认，
    好在这正是「退回原图」的判据。
    """
    target = derived_path(source, cache, px)
    if not force and _fresh(source, target):
        return target, False
    if _render(source, target, px):
        return target, True
    return target, False


def ensure_many(
    sources: list[Path],
    cache: Path,
    px: int = WATCH_PX,
    log=print,
) -> dict[str, int]:
    """批量生成派生图，返回一份统计（顺带算出省了多少字节）。"""
    made = reused = failed = 0
    bytes_before = bytes_after = 0

    for source in sources:
        if not source.is_file():
            failed += 1
            continue
        target, created = ensure(source, cache, px)
        if target.is_file():
            if created:
                made += 1
            else:
                reused += 1
            bytes_before += source.stat().st_size
            bytes_after += target.stat().st_size
        else:
            failed += 1

    stats = {
        "made": made,
        "reused": reused,
        "failed": failed,
        "bytes_before": bytes_before,
        "bytes_after": bytes_after,
        "path": str(cache),
    }
    if made or reused:
        saved = (1 - bytes_after / bytes_before) * 100 if bytes_before else 0
        log(
            f"  表情小图：新生成 {made} 张，沿用 {reused} 张"
            + (f"，失败 {failed} 张" if failed else "")
            + f"（{bytes_before / 1024 / 1024:.2f}MB → {bytes_after / 1024 / 1024:.2f}MB，省 {saved:.0f}%）"
        )
    return stats


def prune(cache: Path, keep_stems: set[str], px: int = WATCH_PX) -> int:
    """删掉库里已经不存在的派生图，返回删除数量。

    派生图是纯缓存，删错也不影响正确性（下次请求会重新生成），
    所以这里不必像动原图那样谨慎。
    """
    if not cache.is_dir():
        return 0
    removed = 0
    suffix = f".{px}{_SUFFIX}"
    for path in cache.iterdir():
        if not path.is_file():
            continue
        name = path.name
        if not name.endswith(suffix):
            # 别的 px 档位或临时文件，不归这次管。
            continue
        if name[: -len(suffix)] in keep_stems:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def main() -> int:
    """一次性生成/补齐全部派生小图（`scripts/build_sticker_thumbs.py` 调它）。

    日常并不需要手动跑：扫描落盘时会自动做，服务端请求时也会按需补。
    这个入口是给「删掉了整个缓存目录，想一次重建」准备的。
    """
    import argparse
    import sys

    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from bridge.sticker_store import StickerLibrary

    parser = argparse.ArgumentParser(description="生成手表表情小图缓存")
    parser.add_argument("--px", type=int, default=WATCH_PX, help=f"目标最长边，默认 {WATCH_PX}")
    parser.add_argument("--force", action="store_true", help="全部重做，忽略已命中的缓存")
    args = parser.parse_args()

    artifacts_dir = project_root / "artifacts"
    library = StickerLibrary.load(project_root, artifacts_dir)
    cache = cache_dir(artifacts_dir)

    sources: list[Path] = []
    stems: set[str] = set()
    for item in library.items:
        path = library.thumb_file(item)
        if path is None:
            continue
        sources.append(path)
        stems.add(path.stem)

    if not sources:
        print("库里没有任何带缩略图的表情，先跑一次扫描。")
        return 1

    if args.force:
        for source in sources:
            derived_path(source, cache, args.px).unlink(missing_ok=True)

    print(f"表情库 {len(library.items)} 项，其中 {len(sources)} 项有缩略图，目标边长 {args.px}px")
    stats = ensure_many(sources, cache, args.px)
    removed = prune(cache, stems, args.px)
    if removed:
        print(f"  清掉 {removed} 张库里已没有的派生图")
    print(f"✓ 完成，缓存目录：{stats['path']}")
    return 0
