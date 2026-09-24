"""抖音侧用到的两个数据类。

这里只放「跑浏览器、发表情」需要的东西（`Settings` / `Sticker`）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Sticker:
    name: str
    category: str | None = None
    accessible_name: str | None = None
    fallback_index: int | None = None
    # 表情面板底部第几个分类栏（0 起）。分类栏是纯图标、没有文字，
    # 所以「自建/收藏表情」这类只能按序号定位 —— 见 app/selectors.py::STICKER_TABS。
    tab_index: int | None = None
    # 图片的资源名（URL path 最后一段、截到 `~` 为止）。发送时按它在面板里认图：
    # 收藏新表情会让所有序号顺移，只有资源名认的还是同一张图。
    resource_key: str | None = None


@dataclass(frozen=True)
class Settings:
    """跑抖音页面需要的东西，全部来自 `.env`。"""

    # `config.json` 的位置 —— 里面是「手表上显示哪些好友」这份名单。
    config_path: Path
    storage_state: str | None
    cookie: str | None
    headless: bool
    browser_path: str | None
    artifacts_dir: Path
    trace: bool
