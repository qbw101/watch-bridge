"""手表端好友名单：`config.json` 里 `friends` 这一个键的读写。

`config.json` 现在只干一件事：**手表上列出哪些会话**。服务启动时把它读进来
（`bridge/session.py`），配置页（`bridge/setup_web.py`）负责让用户勾。除此之外
它没有别的用途，也没有别的程序在读它。

三条硬规矩：

1. **写前先校验**：名单必须是非空列表、每一项都是非空字符串。一个都不留的话手表上
   的会话列表是空的，服务也没有可以打开的会话去取表情面板。
2. **验不过绝不落盘**：宁可拒绝保存，也不像 `server_config.json` 那样降级成默认值
   覆盖 —— 那等于静默删掉用户勾好的名单。
3. **只改 `friends`**：文件里别的键（手写的备注、别处留的字段）一律原样带回去，
   所以待保存的文档从磁盘那份拷贝起步。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from app.names import normalize_name

LOGGER = logging.getLogger("douyin_watch")

CONFIG_NAME = "config.json"
ENV_CONFIG = "WATCH_CONFIG"
BACKUP_SUFFIX = ".bak"

# 名单写进 `config.json` 后要重启服务才会生效：正在跑的会话用的是启动时读进来的那份。
RESTART_HINT = "修改后需重启服务方可生效 —— 正在运行的会话使用的是启动时读取的名单。"

# 手表屏幕就那么大，人太多其实翻不动，提一句。
CROWDED = 15


class WatchFriendsError(ValueError):
    """不合法/不能改。消息直接展示给用户，所以必须说人话。"""


def default_path() -> Path:
    """默认位置：项目根 `config.json`（可用 `WATCH_CONFIG` 覆盖），与 `app/config.py` 一致。"""
    return Path(os.getenv(ENV_CONFIG) or CONFIG_NAME).expanduser()


def load_friend_names(path: Path | str | None = None) -> list[str]:
    """服务启动时读名单。读不了 / 是空的都抛 `WatchFriendsError`，附人话说明。"""
    target = Path(path) if path else default_path()
    if not target.is_file():
        raise WatchFriendsError(
            f"未找到 {target} —— 该文件决定手表端显示哪些会话，请先打开一次配置页勾选好友。"
        )
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except OSError as exc:
        raise WatchFriendsError(f"无法读取 {target}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise WatchFriendsError(
            f"{target} 不是有效的 JSON（第 {exc.lineno} 行：{exc.msg}），请修复后再启动。"
        ) from exc
    if not isinstance(raw, dict):
        raise WatchFriendsError(f"{target} 的顶层必须是一个 JSON 对象。")
    names = _clean_names(raw.get("friends"))
    if not names:
        raise WatchFriendsError(
            f"{target} 中的 friends 为空 —— 手表端将没有任何会话，"
            "请先打开配置页勾选若干项（或直接编辑该文件）。"
        )
    return names


# ---------------------------------------------------------------------- 小工具


def _clean_names(raw: Any) -> list[str]:
    """名字去空白、去重、保序。认不出来的形态一概当空名单。

    只认「字符串列表」：页面上是一个个复选框，一位一个名字。早先那版还有个
    「一行一个名字」的多行文本框，所以这里能收字符串 —— 现在没有那个入口了，
    多一种输入形态就多一种能被写错的东西。

    收名字用 `app.names.normalize_name` 而不是 `.strip()`：后者只去首尾，拦不住
    `"某位好友\n前天"` 这种「名字里混进了会话时间」的脏数据。它还会被原样写回
    落盘，从此搜不到 —— 2026-09-25 踩过，手表端表现为卡在搜索框里。
    """
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        name = normalize_name(item)
        if name and name not in names:
            names.append(name)
    return names


def _soft_warnings(names: list[str]) -> list[str]:
    """「存下去了，不过你可能不是这个意思」。"""
    if len(names) >= CROWDED:
        return [
            f"已启用 {len(names)} 位 —— 这些会话都会出现在手表端，屏幕空间有限，浏览需多次滚动。"
            "暂时用不到的可以先不勾选，之后随时可以加回。"
        ]
    return []


# ---------------------------------------------------------------------- 主体


class FriendStore:
    """`config.json` 里好友名单的读写。页面通过 `describe()` / `save()` 跟它打交道。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else default_path()

    # ------------------------------------------------------------------ 读

    def _read_raw(self) -> tuple[dict[str, Any], str]:
        """返回 (原始对象, 错误说明)。文件不存在算正常（空对象、无错误）。"""
        if not self.path.is_file():
            return {}, ""
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            return {}, f"无法读取：{exc}"
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            return {}, f"不是有效的 JSON（第 {exc.lineno} 行：{exc.msg}）"
        if not isinstance(raw, dict):
            return {}, "顶层不是 JSON 对象"
        return raw, ""

    def describe(self) -> dict[str, Any]:
        """给页面用的状态：现有名单 + 能不能改。绝不抛异常。"""
        raw, error = self._read_raw()
        enabled = _clean_names(raw.get("friends"))
        editable = not error
        reason = "" if editable else f"该文件无法读取（{error}），请修复后再返回。"
        return {
            "path": str(self.path),
            "exists": self.path.is_file(),
            "error": error,
            "enabled": enabled,
            "enabled_count": len(enabled),
            "editable": editable,
            "readonly_reason": reason,
            "hint": RESTART_HINT,
        }

    # ------------------------------------------------------------------ 写

    def save(self, payload: dict[str, Any] | list[str]) -> dict[str, Any]:
        """把勾选结果写回 `friends`。返回 {enabled, warnings}。

        顺序不能反：先按磁盘上那份合并 → 校验 → 才写。校验不过时磁盘分毫未动。
        """
        if isinstance(payload, list):
            payload = {"enabled": payload}
        if not isinstance(payload, dict):
            raise WatchFriendsError("提交的数据应为对象")

        base, error = self._read_raw()
        if error:
            raise WatchFriendsError(
                f"现有 {self.path.name} 无法读取（{error}）。本页写入的是该文件，"
                "无法解析则不能确定还需保留哪些键，请先手动修复或改名留档后再返回。"
            )

        names = _clean_names(payload.get("enabled"))
        if not names:
            raise WatchFriendsError(
                "至少保留一位好友。若一位都不保留，手表端的会话列表将为空，"
                "服务也将没有可供打开的会话以获取表情面板。"
            )

        doc: dict[str, Any] = dict(base)
        doc["friends"] = names

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write(doc)
        LOGGER.info("config.json 的好友名单已更新：%s 位（%s）", len(names), self.path)
        return {"enabled": names, "warnings": _soft_warnings(names)}

    def _write(self, doc: dict[str, Any]) -> None:
        """先留一份 `.bak`，再写临时文件 + 原子替换。"""
        if self.path.is_file():
            try:
                self.path.with_name(self.path.name + BACKUP_SUFFIX).write_bytes(self.path.read_bytes())
            except OSError as exc:  # 留档失败不该拦住保存本身
                LOGGER.warning("config.json 留档失败：%s", exc)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)
