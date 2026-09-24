"""服务端运行配置：`server_config.json` 的读写、校验与首次启动判定。

为什么单独一份文件而不是塞进 `config.json`：
    `config.json` 现在是「手表上显示哪些好友」的名单，由配置页整份重写。把端口、
    令牌、图片尺寸混进去有两个坏处：保存名单时得小心别把服务设置带丢（反过来也一样），
    而且用户手改名单时容易连带把服务设置改坏。服务自己的设置单独一份。

容错原则：**配置坏掉不该拦住服务启动**。文件缺失 → 默认值；JSON 坏了 → 改名留档
（`server_config.json.bad`）再用默认值，用户事后还能把自己的设置捞回来。真正需要
用户当场决定的事情（比如端口撞了）交给页面去提示，而不是在这里抛异常。
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("douyin_watch")

CONFIG_NAME = "server_config.json"
ENV_CONFIG = "WATCH_SERVER_CONFIG"

# 「启动时是否打开配置页」的三种状态。只给一个布尔值不够用：
# 第一次跑必须打开（否则用户根本不知道端口和令牌是多少），之后又大多不想被打扰。
OPEN_MODES = ("first", "always", "never")
CRYPTO_MODES = ("gcm", "auto", "plain")

FIELD_LABELS: dict[str, str] = {
    "host": "监听地址",
    "port": "监听端口",
    "token": "访问令牌",
    "crypto": "链路加密",
    "sticker_px": "表情缩略图边长",
    "media_px": "聊天图片边长",
}

# 这些字段是服务构造时就吃进去的，改完必须重启进程才生效；其余字段（比如
# open_page）下次启动自然读到，页面不该为它们喊「请重启」。
RESTART_FIELDS: tuple[str, ...] = ("host", "port", "token", "crypto", "sticker_px", "media_px")


class ConfigError(ValueError):
    """页面提交的值不合法。消息直接展示给用户，所以要说人话。"""


def default_config_path() -> Path:
    """默认位置：项目根的 `server_config.json`（可用 `WATCH_SERVER_CONFIG` 覆盖）。

    注意是相对**项目根**（调用方传绝对路径进来）而不是当前工作目录：`.bat` 会
    `cd` 到项目根，但从别处 `python scripts/watch_server.py` 时 cwd 就不对了 ——
    端口和令牌比 `config.json` 更不该因为「从哪儿启动」而变。
    """
    override = (os.getenv(ENV_CONFIG) or "").strip()
    return Path(override).expanduser() if override else Path(CONFIG_NAME)


def _as_int(value: Any, label: str, low: int, high: int) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        raise ConfigError(f"{label}需为整数") from None
    if not low <= number <= high:
        raise ConfigError(f"{label}需在 {low}~{high} 之间")
    return number


def _as_choice(value: Any, label: str, options: tuple[str, ...]) -> str:
    text = str(value).strip()
    if text not in options:
        raise ConfigError(f"{label}只能为 {' / '.join(options)}")
    return text


def _as_host(value: Any) -> str:
    text = str(value).strip()
    if not text or any(ch.isspace() for ch in text):
        raise ConfigError("监听地址不能为空，且不能包含空格")
    if ":" in text and not text.startswith("["):
        return text  # IPv6 字面量，原样（socket 直接吃 "::" 这种形式）
    return text


def _as_token(value: Any, *, strict: bool) -> str:
    text = str(value).strip()
    if not text:
        if strict:
            raise ConfigError("访问令牌不能为空（也可点击「重新生成」）")
        return ""
    if len(text) > 128:
        raise ConfigError("访问令牌最多 128 个字符")
    if any(ch.isspace() for ch in text):
        raise ConfigError("访问令牌不能包含空格（手表端输入时容易出错）")
    return text


@dataclass
class ServerConfig:
    """桥接服务的运行设置。字段名与 `server_config.json` 的键一一对应。"""

    host: str = "0.0.0.0"
    port: int = 8787
    token: str = ""
    crypto: str = "gcm"
    sticker_px: int = 72
    media_px: int = 128
    open_page: str = "first"
    setup_done: bool = False
    setup_at: str = ""
    # 文件里我们不认识、也不该丢掉的键（用户手写的备注等），原样保存回去
    extra: dict[str, Any] = field(default_factory=dict, repr=False)
    path: Path | None = field(default=None, repr=False, compare=False)

    # ------------------------------------------------------------------ 序列化

    def to_dict(self) -> dict[str, Any]:
        """给页面看的形态：不含路径，不含 extra（页面不认识它）。"""
        return {
            "host": self.host,
            "port": self.port,
            "token": self.token,
            "crypto": self.crypto,
            "sticker_px": self.sticker_px,
            "media_px": self.media_px,
            "open_page": self.open_page,
            "setup_done": self.setup_done,
            "setup_at": self.setup_at,
        }

    def _payload(self) -> dict[str, Any]:
        data = self.extra.copy()
        data.update(self.to_dict())
        return data

    def copy(self) -> "ServerConfig":
        clone = ServerConfig(**self.to_dict())
        clone.extra = dict(self.extra)
        clone.path = self.path
        return clone

    def adopt(self, other: "ServerConfig") -> None:
        """把另一份的值搬进自己。

        用「就地搬」而不是「换对象」是因为配置页握着这个对象的引用 —— 启动向导
        等用户点完「保存并启动」后会重新读一遍文件（用户可能在页面里改过），
        这时候如果换成新对象，页面后续的保存就全写进那份没人看的旧对象里了。
        """
        for f in fields(ServerConfig):
            if f.name in ("extra", "path"):
                continue
            setattr(self, f.name, getattr(other, f.name))
        self.extra = dict(other.extra)

    def save(self) -> None:
        """落盘。先写临时文件再 replace —— 半途断电也不会留下半截 JSON。"""
        target = self.path or default_config_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        text = json.dumps(self._payload(), ensure_ascii=False, indent=2) + "\n"
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
        self.path = target

    # ------------------------------------------------------------------ 校验

    def apply_payload(self, payload: dict[str, Any], *, strict_token: bool = True) -> list[str]:
        """把（HTML 表单来的、全是字符串的）值校验后写进自己，返回需要提醒的警告。

        警告和错误分开：错误（ConfigError）是「存不下去」，警告是「存下去了但你可能
        不是这个意思」—— 比如监听地址填了某张网卡的具体 IP，手表换个 Wi-Fi 就连不上。
        """
        if not isinstance(payload, dict):
            raise ConfigError("数据格式错误")

        warnings: list[str] = []
        host = _as_host(payload.get("host", self.host))
        port = _as_int(payload.get("port", self.port), "监听端口", 1, 65535)
        token = _as_token(payload.get("token", self.token), strict=strict_token)
        crypto = _as_choice(payload.get("crypto", self.crypto), "链路加密", CRYPTO_MODES)
        sticker_px = _as_int(payload.get("sticker_px", self.sticker_px), "表情缩略图边长", 16, 256)
        media_px = _as_int(payload.get("media_px", self.media_px), "聊天图片边长", 16, 512)
        open_page = _as_choice(payload.get("open_page", self.open_page), "启动时打开配置页", OPEN_MODES)

        if host not in ("0.0.0.0", "::"):
            warnings.append(
                f"监听地址为 {host}，仅监听该网卡。手表切换 Wi-Fi"
                "或本机更换网络后将无法连接，建议使用 0.0.0.0。"
            )
        if port < 1024:
            warnings.append(f"端口 {port} 低于 1024，部分系统需管理员权限方可监听。")
        if crypto == "plain":
            warnings.append("链路加密已关闭：令牌与聊天内容均为明文，仅建议排障时使用。")
        elif crypto == "auto":
            warnings.append("「跟随客户端」会使不支持加密的客户端降级为明文，日常建议使用「强制加密」。")
        if token and len(token) < 8:
            warnings.append("访问令牌长度偏短，建议点击「重新生成」更换为更长的令牌。")
        if media_px > 256:
            warnings.append(f"聊天图片 {media_px}px 会使手表端明显变慢（默认 128）。")

        self.host = host
        self.port = port
        self.token = token
        self.crypto = crypto
        self.sticker_px = sticker_px
        self.media_px = media_px
        self.open_page = open_page
        return warnings

    def mark_setup_done(self) -> None:
        self.setup_done = True
        self.setup_at = time.strftime("%Y-%m-%d %H:%M:%S")

    # ------------------------------------------------------------------ 比较

    def restart_diff(self, other: "ServerConfig | None") -> list[str]:
        """返回「和正在运行的这份不一样、因此要重启才生效」的字段中文名。"""
        if other is None:
            return []
        changed: list[str] = []
        for name in RESTART_FIELDS:
            if getattr(self, name) != getattr(other, name):
                changed.append(FIELD_LABELS.get(name, name))
        return changed


def load(path: Path | None = None) -> tuple[ServerConfig, list[str]]:
    """读配置。返回 (配置, 给用户看的一两句说明)。

    任何解析问题都降级成默认值 + 说明，绝不抛出 —— 这个函数在启动路径上，
    它挂掉就等于服务起不来。
    """
    target = Path(path) if path else default_config_path()
    config = ServerConfig(path=target)
    if not target.is_file():
        return config, ["尚无 server_config.json，本次使用默认值（保存后将自动生成）。"]

    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        backup = target.with_name(target.name + ".bad")
        try:
            os.replace(target, backup)
            kept = f"已留档到 {backup.name}，用默认值继续"
        except OSError:
            kept = "用默认值继续"
        LOGGER.warning("server_config.json 读不了（%s），%s", exc, kept)
        return config, [f"配置文件无法读取：{exc}。{kept}。"]

    if not isinstance(raw, dict):
        return config, ["配置文件顶层不是对象，已忽略，改用默认值继续。"]

    known = {f.name for f in fields(ServerConfig) if f.name not in ("extra", "path")}
    defaults = ServerConfig()
    candidate = dict(raw)
    for name in known:
        if name in candidate:
            setattr(config, name, candidate.pop(name))
    config.extra = candidate

    # 文件里的值也是「用户手写的」，一样要过校验。这里宽松一些：坏字段退回默认值
    # 并提醒，而不是整份作废（否则一个手滑的逗号就把端口打回 8787）。
    notes: list[str] = []
    try:
        warnings = config.apply_payload(config.to_dict(), strict_token=False)
    except ConfigError as exc:
        LOGGER.warning("server_config.json 有不合法的值（%s），退回默认值", exc)
        notes.append(f"配置文件包含不合法的值（{exc}），该项已退回默认值。")
        fixed = defaults.copy()
        fixed.path = config.path
        fixed.extra = config.extra
        fixed.setup_done = bool(getattr(config, "setup_done", False))
        config = fixed
        warnings = []
    notes.extend(warnings)
    return config, notes


def blank_for_page(config: ServerConfig) -> dict[str, Any]:
    """页面用的初始值：把「读不到/不合法」的字段换成默认，避免前端拿到 None。"""
    return config.copy().to_dict()
