"""从 `.env` 读出跑抖音页面需要的设置（`app.models.Settings`）。

只认环境变量，不碰 `config.json` —— 那份文件是「手表上显示哪些好友」的名单，
由 `bridge/watch_friends.py` 负责读写。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from app.models import Settings


class ConfigError(ValueError):
    pass


def load_settings(env_file: str | Path | None = None) -> Settings:
    load_dotenv(dotenv_path=env_file)
    config_path = Path(os.getenv("WATCH_CONFIG", "config.json")).expanduser()
    artifacts_dir = Path(os.getenv("ARTIFACTS_DIR", "artifacts")).expanduser()
    default_state = Path("storage-state.json")

    return Settings(
        config_path=config_path,
        storage_state=_optional_env("DOUYIN_STORAGE_STATE") or (str(default_state) if default_state.is_file() else None),
        cookie=_optional_env("DOUYIN_COOKIE"),
        headless=_parse_bool(os.getenv("HEADLESS", "false"), "HEADLESS"),
        browser_path=_optional_env("BROWSER_PATH"),
        artifacts_dir=artifacts_dir,
        trace=_parse_bool(os.getenv("TRACE", "true"), "TRACE"),
    )


def parse_auth_json(value: str, label: str) -> Any:
    candidate = Path(value).expanduser()
    try:
        if candidate.is_file():
            return json.loads(candidate.read_text(encoding="utf-8"))
    except OSError:
        pass
    try:
        return json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{label} 不是有效 JSON 或可读文件路径") from exc


def _parse_bool(value: str, label: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{label} 必须是 true 或 false")


def _optional_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _optional_env(name: str) -> str | None:
    return _optional_string(os.getenv(name))
