"""抖音登录账号：读凭证里的身份、发起扫码登录、退出登录。

为什么这块单独一个模块而不是塞进 `server_config.py`：
    服务自己的设置（端口 / 令牌）和「抖音登录的是谁」是两件完全不同的事。前者写在
    `server_config.json` 里、改完重启生效；后者是 `storage-state.json` 那坨 Playwright
    凭证，**只能靠人扫码产生**，页面上能做的只是「发起登录 / 删掉它」。

三件容易搞砸的事，这里都按下面处理：

1. **别在页面进程里开浏览器**：扫码登录要一个真实可见的浏览器窗口，而桥接服务自己的
   浏览器正开着那个抖音会话。所以登录一律**起子进程**跑 `scripts/login_auto.py`，
   服务只是看着它、把它的输出尾部显示出来。子进程崩了、用户不扫了，都不会连累服务。
2. **凭证文件只读不猜**：昵称 / 头像 / uid 都从 `storage-state.json` 里的
   `localStorage.user_info` 挖（那是抖音自己写进去的）。文件读不懂就说读不懂，
   不编一个「未登录」糊过去 —— 那会让人以为是掉登录了，其实是文件坏了。
3. **退出登录是真的删**：只删本地凭证、不动抖音服务端。所以删完必须提醒一句
   「正在跑的服务还带着旧凭证，重启后才真的登出」，否则用户会以为按一下就等于登出。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

LOGGER = logging.getLogger("douyin_watch")

CONFIG_NAME = "storage-state.json"
ENV_STATE = "DOUYIN_STORAGE_STATE"
LOGIN_SCRIPT = ("scripts", "login_auto.py")
DOUYIN_ORIGIN = "https://www.douyin.com"
# `user_info` 带 uid + 昵称 + 头像，`user_info_passport` 只有后两个；按这个顺序取，先到先得。
USER_INFO_KEYS = ("user_info", "user_info_passport")

DEFAULT_LOGIN_TIMEOUT = 240
MAX_LOGIN_TIMEOUT = 900
OUTPUT_TAIL_LINES = 12

STATE_IDLE = "idle"
STATE_WAITING = "waiting"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_TIMEOUT = "timeout"


class AccountError(ValueError):
    """页面上的操作没做成。消息直接展示给用户，所以必须说人话。"""


def default_path(project_root: Path) -> Path:
    """凭证文件位置：`DOUYIN_STORAGE_STATE` > 项目根下的 `storage-state.json`。

    与服务一致（`app/config.py::load_settings` 也是先看环境变量），相对路径按项目根解析 ——
    这条路径是「服务读的那份」，页面必须看同一份。
    """
    raw = (os.getenv(ENV_STATE) or "").strip()
    if raw:
        path = Path(raw).expanduser()
        return path if path.is_absolute() else project_root / path
    return project_root / CONFIG_NAME


def _fmt_time(timestamp: float) -> str:
    try:
        return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return ""


def _mask(value: str) -> str:
    return value if len(value) <= 16 else f"{value[:8]}…{value[-4:]}"


def _account_from_state(raw: Any) -> dict[str, str]:
    """从 storage_state 里挖出昵称 / 头像 / uid / 登录时间。挖不到就留空。"""
    found: dict[str, str] = {"nickname": "", "avatar": "", "uid": "", "login_at": ""}
    if not isinstance(raw, dict):
        return found

    for origin in raw.get("origins") or []:
        if not isinstance(origin, dict):
            continue
        if str(origin.get("origin") or "").rstrip("/") != DOUYIN_ORIGIN:
            continue
        entries: dict[str, Any] = {}
        for item in origin.get("localStorage") or []:
            if not isinstance(item, dict) or item.get("name") not in USER_INFO_KEYS:
                continue
            try:
                parsed = json.loads(item.get("value") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict):
                entries[str(item["name"])] = parsed
        for key in USER_INFO_KEYS:
            entry = entries.get(key) or {}
            found["nickname"] = found["nickname"] or str(entry.get("nickname") or "")
            avatar = entry.get("avatarUrl") or entry.get("avatar_url") or ""
            found["avatar"] = found["avatar"] or str(avatar)
            found["uid"] = found["uid"] or str(entry.get("uid") or "")

    for cookie in raw.get("cookies") or []:
        if not isinstance(cookie, dict) or cookie.get("name") != "login_time":
            continue
        try:
            seconds = float(str(cookie.get("value") or "").strip()) / 1000.0
        except ValueError:
            continue
        found["login_at"] = _fmt_time(seconds)
    return found


class AccountStore:
    """抖音登录凭证的读写。页面通过 `describe()` / `start_login()` / `logout()` 跟它打交道。"""

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        project_root: Path | str | None = None,
        python: str | None = None,
        script: Path | str | None = None,
        command_factory: Callable[[int], list[str]] | None = None,
    ) -> None:
        self.project_root = Path(project_root) if project_root else Path(__file__).resolve().parents[1]
        self.path = Path(path) if path else default_path(self.project_root)
        self.python = python or sys.executable
        self.script = Path(script) if script else self.project_root.joinpath(*LOGIN_SCRIPT)
        # 给自检用的缝：默认起真脚本，测试时换成「回声然后退出」这种命令。
        # 有它在，检查登录状态机就不必真的弹一个浏览器。
        self._command_factory = command_factory or self._default_command

        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._state = STATE_IDLE
        self._message = ""
        self._started = 0.0
        self._deadline = 0.0
        self._output: deque[str] = deque(maxlen=OUTPUT_TAIL_LINES)

    # ------------------------------------------------------------------ 读

    def describe(self) -> dict[str, Any]:
        """给页面看的账号快照。只读、绝不抛异常（读不到就说读不到）。"""
        info: dict[str, Any] = {
            "path": str(self.path),
            "exists": self.path.is_file(),
            "mtime": "",
            "mtime_ts": 0.0,
            "size": 0,
            "nickname": "",
            "avatar": "",
            "uid": "",
            "uid_short": "",
            "login_at": "",
            "error": "",
            "note": "",
            "script": str(self.script),
            "script_exists": self.script.is_file(),
        }
        if not info["exists"]:
            info["note"] = "尚未登录。点击「扫码登录」后浏览器将打开抖音，使用手机扫码即可。"
            return info

        try:
            stat = self.path.stat()
            info["mtime_ts"] = stat.st_mtime
            info["mtime"] = _fmt_time(stat.st_mtime)
            info["size"] = stat.st_size
        except OSError as exc:
            info["error"] = f"无法读取凭证文件：{exc}"
            return info

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            info["error"] = f"凭证文件无法读取（{exc}）—— 重新扫码登录一次即可覆盖。"
            return info

        info.update(_account_from_state(raw))
        info["uid_short"] = _mask(info["uid"])
        if not info["nickname"]:
            info["note"] = "凭证已存在，但其中没有账号信息（可能由其他方式写入）。重启服务后查看服务日志最为准确。"
        if (os.getenv(ENV_STATE) or "").strip():
            info["note"] = (
                (info["note"] + " " if info["note"] else "")
                + f"注意：凭证路径已被环境变量 {ENV_STATE} 修改，"
                f"而扫码登录脚本仍写入项目根的 {CONFIG_NAME}，二者可能并非同一份。"
            )
        return info

    # ------------------------------------------------------------------ 登录

    def _default_command(self, timeout: int) -> list[str]:
        return [self.python, str(self.script), str(timeout)]

    @staticmethod
    def _clamp_timeout(timeout: Any) -> int:
        if timeout in (None, ""):
            return DEFAULT_LOGIN_TIMEOUT
        try:
            seconds = int(float(str(timeout).strip()))
        except ValueError:
            raise AccountError("等待扫码的秒数需为数字") from None
        return max(30, min(seconds, MAX_LOGIN_TIMEOUT))

    def _running(self) -> bool:
        """子进程还在跑吗。跑完的进程仍留在 `self._proc` 上（要留退出码）。"""
        return self._proc is not None and self._proc.poll() is None

    def login_state(self) -> dict[str, Any]:
        """当前登录动作的状态。页面每 2 秒轮询它一次，超时就在这里判定。"""
        with self._lock:
            if self._state == STATE_WAITING and self._proc is not None:
                if self._proc.poll() is None and time.time() > self._deadline:
                    LOGGER.warning("扫码登录等待超时，结束登录进程")
                    self._state = STATE_TIMEOUT
                    self._message = "等待扫码超时，本次登录已结束。如需重试，请再次点击「扫码登录」。"
                    try:
                        self._proc.kill()
                        self._proc.wait(timeout=2)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
            return {
                "state": self._state,
                "message": self._message,
                "running": self._running(),
                "started_at": _fmt_time(self._started) if self._started else "",
                "seconds_left": max(0, int(self._deadline - time.time())) if self._state == STATE_WAITING else 0,
                "output": list(self._output),
                "timeout": int(self._deadline - self._started) if self._started else 0,
            }

    def start_login(self, timeout: Any = None) -> dict[str, Any]:
        """起一个浏览器让用户扫码。已经在等就报错，不会起第二个。"""
        with self._lock:
            if self._running():
                raise AccountError("上一次扫码仍在等待 —— 请先在页面上点击「取消」，或扫码完成本次登录")
            seconds = self._clamp_timeout(timeout)
            argv = self._command_factory(seconds)
            if not argv:
                raise AccountError("登录命令为空")
            creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            try:
                proc = subprocess.Popen(  # noqa: S603 - argv 是我们自己拼的
                    argv,
                    cwd=str(self.project_root),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creation,
                )
            except OSError as exc:
                raise AccountError(f"登录脚本无法启动：{exc}（请确认文件是否存在：{self.script}）") from None

            self._proc = proc
            self._state = STATE_WAITING
            self._started = time.time()
            self._deadline = self._started + seconds
            self._output.clear()
            self._message = f"浏览器窗口已打开，请使用抖音 App 扫码登录（最长等待 {seconds} 秒）"
            threading.Thread(target=self._pump, args=(proc,), name="login-output", daemon=True).start()
        # `login_state()` 也要拿同一把锁 —— 不能在 with 里面调（这把锁不可重入，
        # 会当场把自己锁死，页面就一直等不到响应）。
        LOGGER.info("已发起抖音扫码登录（最多等 %s 秒）", seconds)
        return self.login_state()

    def _pump(self, proc: subprocess.Popen[str]) -> None:
        """把子进程的输出收进环形缓冲，退出后判定成败。"""
        try:
            for line in proc.stdout or []:
                line = line.rstrip()
                if line:
                    self._output.append(line)
        except (OSError, ValueError):
            LOGGER.debug("读登录脚本输出失败", exc_info=True)
        finally:
            code = proc.wait()
            with self._lock:
                # 取消 / 超时已经定过调子，别拿退出码再覆盖一次
                if self._state == STATE_WAITING:
                    if code == 0 and self.path.is_file():
                        self._state = STATE_DONE
                        self._message = "登录成功，凭证已保存。重启服务后生效。"
                    elif code == 0:
                        self._state = STATE_FAILED
                        self._message = "脚本报告登录成功，但未发现凭证文件，请重试。"
                    else:
                        tail = " / ".join(list(self._output)[-3:]) or "没有任何输出"
                        self._state = STATE_FAILED
                        self._message = f"登录失败（脚本退出码 {code}）：{tail}"
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except OSError:
                pass

    def cancel_login(self) -> dict[str, Any]:
        with self._lock:
            if not self._running():
                raise AccountError("当前没有等待中的扫码")
            self._state = STATE_CANCELLED
            self._message = "已取消本次扫码。"
            proc = self._proc
        if proc is not None:
            try:
                proc.kill()
                # 等它真死：不等的话紧接着的 poll() 还可能回 None，页面就会先闪一下
                # 「还在等扫码」再变回来。
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                LOGGER.debug("结束登录进程失败", exc_info=True)
        LOGGER.info("用户取消了扫码登录")
        return self.login_state()

    def reset_login(self) -> dict[str, Any]:
        """把上一次的结果收起来，让卡片回到「什么都没发生」的样子。"""
        with self._lock:
            if self._running():
                raise AccountError("扫码仍在等待中，请先点击「取消」")
            self._state = STATE_IDLE
            self._message = ""
            self._output.clear()
        return self.login_state()

    # ------------------------------------------------------------------ 退出

    def logout(self) -> dict[str, Any]:
        """删掉本地凭证。抖音服务端那边不受影响 —— 所以要提醒用户重启才真的登出。"""
        with self._lock:
            if self._running():
                raise AccountError("正在扫码登录，请等待其结束或点击「取消」后再退出登录")
        if not self.path.is_file():
            return {"removed": False, "message": "本就没有登录凭证，无需退出。"}
        try:
            self.path.unlink()
        except OSError as exc:
            raise AccountError(f"无法删除凭证文件（{exc}）—— 该文件可能被其他程序占用。") from None
        # 上一次扫码的结果（「登录成功」）在退出之后就没有意义了。留着它，页面上那句
        # 「已退出登录，重启后生效」会被它顶掉，看着像退出没成功。
        with self._lock:
            self._state = STATE_IDLE
            self._message = ""
            self._output.clear()
        # 登录脚本写了一半被中断会留下 .tmp，顺手清掉，免得下次「有文件但读不了」
        for leftover in (self.path.with_name(self.path.name + ".tmp"),):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass
        LOGGER.info("已退出抖音登录：删除了 %s", self.path)
        return {
            "removed": True,
            "message": "凭证已删除。正在运行的服务仍持有旧凭证（需重启服务才算真正登出），"
                       "如需更换账号，现在即可点击「扫码登录」。",
        }
