"""让常驻的手表服务自己刷新表情库。

**为什么需要它**

`scripts/scan_stickers.py` 是「离线」扫描：它自己开一个 Chromium、自己登一次抖音，
而且必须独占 `artifacts/run.lock` —— 也就是说，想扫一次新收藏的表情，得先把手表服务停掉。
用户收藏了一个表情，手表上要等很久才出现，还得记得手动跑一次扫描。

而手表服务本身已经有一个常驻的、已登录的抖音页面。让它顺手刷一下表情面板，
既不用停服务、也不用第二次登录（少一次风控暴露面），收藏的表情就能自动出现在手表上。
刷新完库文件被改写，推送循环的 mtime 检查会自动把新状态推给手表，手表端不需要改任何东西。

**为什么不复制一份扫描逻辑**

采集/下载/合并这条路线上，「选择器必须和发送端一致」是硬要求
（见 `app/selectors.py` 里 STICKER_TAB_ITEMS 的注释）。复制一份出来迟早会和
`scan_stickers.py` 分叉，而分叉的后果是「点的是这个、发的是那个」。
所以这里只做转发，真正干活的是 `scan_stickers.refresh_in_live_session`。
"""

from __future__ import annotations

import logging
import sys
import types
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("douyin_watch")

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _scanner():
    """惰性导入 `scripts/scan_stickers.py`。

    必须放在函数里：`scan_stickers` 反过来依赖 `bridge.session`
    （取 ALLOWED_MEDIA_HOSTS），模块级导入会形成循环 ——
    bridge.session 还没定义完就被要这个常量。
    """
    scripts_dir = PROJECT_ROOT / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import scan_stickers  # noqa: PLC0415 - 见上面的循环导入说明

    return scan_stickers


async def refresh_stickers(
    *,
    page,
    context,
    artifacts_dir: Path,
    indices: list[int] | None = None,
    skip_tabs: set[int] | None = None,
    log=None,
    should_yield=None,
) -> dict[str, Any]:
    """在已经登录的常驻页面上重扫表情面板。

    `indices` 默认 None = 除默认跳过栏外全扫（分类 3 因为排列不稳定默认不收，
    原因见 `scan_stickers.DEFAULT_SKIP_TABS`）。

    `should_yield` 原样转给扫描器：它是「现在有没有用户操作在等浏览器」的探针，
    扫描器在每段等待之间用它决定要不要主动让路（见 `app.douyin.RefreshYielded`）。
    """
    scanner = _scanner()
    skip = set(scanner.DEFAULT_SKIP_TABS) if skip_tabs is None else set(skip_tabs)
    # 下载缩略图那步要用 session.context.request；服务里 page 和 context 是分开存的，
    # 这里拼一个最小对象出来，免得把下载函数的签名改得更别扭。
    session = types.SimpleNamespace(page=page, context=context)
    return await scanner.refresh_in_live_session(
        session,
        artifacts_dir,
        indices=indices,
        skip_tabs=skip,
        log=log or LOGGER.info,
        should_yield=should_yield,
    )
