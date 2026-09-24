"""生成/重建手表表情小图缓存。

为什么需要这个脚本：手表上每个表情只是一个 70 来 px 的静态格子，而抖音的原图里
有 1MB+ 的动图 webp。服务端如果每次请求都拿原图现解码、现缩放，实测平均 24.6ms、
最慢 961ms —— 68 个表情的首屏光服务端就要一秒多，而且那份内存缓存一重启就没了。

所以缩放被挪到「一次落盘」：缩好的小图存在 artifacts/sticker_thumbs/，
之后服务端只要读一个十几 KB 的文件，几十毫秒变一毫秒。

日常**不需要手动跑**这个脚本：
- `scripts/scan_stickers.py` 落盘时会自动补齐变化的那几张；
- 服务端遇到缺失的会按需生成。

它只用在「缓存目录被删了/想强制重建」的时候。

用法：
    python scripts/build_sticker_thumbs.py           # 补齐缺失的
    python scripts/build_sticker_thumbs.py --force   # 全部重做
    python scripts/build_sticker_thumbs.py --px 96   # 换一档尺寸
"""

from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bridge.sticker_thumbs import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
