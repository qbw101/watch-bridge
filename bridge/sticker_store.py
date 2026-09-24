"""手表端表情库：`watch_stickers.json` 的读写。

为什么要单独一份文件：

- `config.json` 只管「手表上显示哪些好友」—— 一个键、一眼看得完。表情库有几十项，
  每项还带着缩略图文件名和启用开关，塞进去会把那份名单淹掉。
- 表情库是**扫描产物**（`scripts/scan_stickers.py` 去抖音表情面板里读出来的），
  名单是**用户勾的**。更新方式不一样，寿命也不一样。

缩略图为什么要落盘到 artifacts/stickers/：

- 抖音表情图的 URL 带签名参数，过一段时间就 403。扫描时顺手把二进制存到本地，
  之后手表端读本地文件，既快又不会过期。
- 顺便让服务端在「抖音会话没就绪」时也能正常显示表情图标。

对外只暴露三件事：库的加载/保存、按 id 或名字查项、缩略图文件路径。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.models import Sticker

LOGGER = logging.getLogger("douyin_watch")

LIBRARY_FILENAME = "watch_stickers.json"
THUMB_DIRNAME = "stickers"

# 缩略图扩展名白名单：只挑我们确定能直接喂给 <img>/Image 的格式。
THUMB_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


def make_item_id(name: str, category: str | None) -> str:
    """给表情生成稳定 id。

    用内容而不是顺序来派生，这样重扫一次不会让所有 id 漂移 ——
    手表端缓存的缩略图 URL 才不会集体失效。
    """
    raw = f"{category or ''}\x1f{name}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:10]


@dataclass
class LibraryItem:
    """表情库里的一项。

    `name` 是抖音表情面板里用来匹配的那个名字（匹配 `emojiEmojiItemDesc`
    的文案、aria-label、alt），`label` 是手表小图上显示的文字。
    """

    id: str
    name: str
    label: str = ""
    category: str | None = None
    accessible_name: str | None = None
    fallback_index: int | None = None
    # 表情面板底部第几个分类栏（0 起）+ 该栏里第几个表情。
    # 自建/收藏表情没有名字也没有 aria-label，只能靠这一对序号定位。
    tab_index: int | None = None
    # 图片在抖音侧的稳定资源名（URL 的 path 部分，不含过期的签名参数）。
    # 序号会因为抖音那边增删表情而顺移，靠它才能校验「序号还指得对」；
    # 也是 id 的派生来源，所以重扫不会让缩略图缓存整体失效。
    source_key: str | None = None
    thumb: str | None = None
    enabled: bool = True

    def to_sticker(self) -> Sticker:
        """转成 `app/sender.py` 能消费的 Sticker。"""
        return Sticker(
            name=self.name,
            category=self.category,
            accessible_name=self.accessible_name or self.name,
            fallback_index=self.fallback_index,
            tab_index=self.tab_index,
            # 资源名一起带上：发送时用它认图，序号顺移了也不会发错
            resource_key=self.source_key,
        )

    def to_watch(self, thumb_bytes: int = 0) -> dict[str, Any]:
        """给手表端的精简结构：只给渲染要用的字段。

        `thumbBytes` 是这个表情传到手表上要花多少字节，由调用方按**实际会下发的图**
        算出来（服务端下发的是派生小图，不是 artifacts/stickers/ 里的原图，
        两者能差一个数量级）。手表靠它估算「全量拉一遍要多少流量」，据此决定
        是懒加载还是先拉一屏 —— 报原图大小会让它以为一屏几十兆，白白多等一轮。
        """
        return {
            "id": self.id,
            "name": self.name,
            "label": self.label or self.name,
            "category": self.category or "",
            "has_thumb": bool(self.thumb),
            "thumbBytes": thumb_bytes,
        }

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "label": self.label or self.name,
            "enabled": self.enabled,
        }
        if self.category:
            payload["category"] = self.category
        if self.accessible_name:
            payload["accessible_name"] = self.accessible_name
        if self.fallback_index is not None:
            payload["fallback_index"] = self.fallback_index
        if self.tab_index is not None:
            payload["tab_index"] = self.tab_index
        if self.source_key:
            payload["source_key"] = self.source_key
        if self.thumb:
            payload["thumb"] = self.thumb
        return payload

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> "LibraryItem":
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ValueError("表情库每一项都需要 name")
        category = _optional_str(raw.get("category"))
        item_id = str(raw.get("id") or "").strip() or make_item_id(name, category)
        fallback = raw.get("fallback_index")
        if fallback is not None and (not isinstance(fallback, int) or fallback < 0):
            raise ValueError(f"表情 {name} 的 fallback_index 必须是非负整数")
        tab_index = raw.get("tab_index")
        if tab_index is not None and (not isinstance(tab_index, int) or tab_index < 0):
            raise ValueError(f"表情 {name} 的 tab_index 必须是非负整数")
        return cls(
            id=item_id,
            name=name,
            label=str(raw.get("label") or "").strip() or name,
            category=category,
            accessible_name=_optional_str(raw.get("accessible_name")),
            fallback_index=fallback,
            tab_index=tab_index,
            source_key=_optional_str(raw.get("source_key")),
            thumb=_optional_str(raw.get("thumb")),
            enabled=bool(raw.get("enabled", True)),
        )


class StickerLibrary:
    """手表端表情库，持有 watch_stickers.json 的内存副本。"""

    def __init__(self, path: Path, thumb_dir: Path, items: list[LibraryItem] | None = None) -> None:
        self.path = path
        self.thumb_dir = thumb_dir
        self.items: list[LibraryItem] = items or []

    # ------------------------------------------------------------------ 载入 / 保存

    @classmethod
    def load(cls, project_root: Path, artifacts_dir: Path) -> "StickerLibrary":
        path = project_root / LIBRARY_FILENAME
        library = cls(path=path, thumb_dir=artifacts_dir / THUMB_DIRNAME)
        if not path.is_file():
            library.items = []
            return library
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            # 库坏了不该拖垮整个服务：手表端退化成「只有配置里的快捷表情」。
            LOGGER.error("表情库读取失败，按空库处理: %s", exc)
            library.items = []
            return library
        raw_items = raw.get("items") if isinstance(raw, dict) else None
        if not isinstance(raw_items, list):
            LOGGER.error("表情库格式不对（items 不是数组），按空库处理")
            library.items = []
            return library
        items: list[LibraryItem] = []
        seen: set[str] = set()
        for entry in raw_items:
            if not isinstance(entry, dict):
                continue
            try:
                item = LibraryItem.from_json(entry)
            except ValueError as exc:
                LOGGER.warning("跳过表情库中的无效项: %s", exc)
                continue
            if item.id in seen:
                continue
            seen.add(item.id)
            items.append(item)
        library.items = items
        return library

    def save(self) -> None:
        payload = {
            "version": 1,
            "items": [item.to_json() for item in self.items],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        # 先写临时文件再替换：扫描中途被打断也不会留下半截 JSON。
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.path)

    # ------------------------------------------------------------------ 查询

    def enabled(self) -> list[LibraryItem]:
        return [item for item in self.items if item.enabled]

    def by_ref(self, ref: str) -> LibraryItem | None:
        """按 id 或 name 查。手表端发的是 name，缩略图接口用的是 id。"""
        target = (ref or "").strip()
        if not target:
            return None
        for item in self.items:
            if item.id == target:
                return item
        for item in self.items:
            if item.name == target:
                return item
        return None

    def thumb_file(self, item: LibraryItem) -> Path | None:
        if not item.thumb:
            return None
        # 只取文件名，防止配置里被塞进 ../ 之类的路径穿越。
        candidate = self.thumb_dir / Path(item.thumb).name
        return candidate if candidate.is_file() else None

    def thumb_bytes(self, item: LibraryItem) -> int:
        """本地缩略图的字节数；没有或读不到时回 0。"""
        path = self.thumb_file(item)
        if path is None:
            return 0
        try:
            return path.stat().st_size
        except OSError:
            return 0

    def total_thumb_bytes(self) -> int:
        return sum(self.thumb_bytes(item) for item in self.enabled())

    def replace_all(self, items: list[LibraryItem]) -> None:
        self.items = items

    def stats(self) -> dict[str, int]:
        return {
            "items": len(self.items),
            "enabled": len(self.enabled()),
            "with_thumb": sum(1 for item in self.items if item.thumb),
        }


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
