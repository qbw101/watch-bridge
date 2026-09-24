"""服务端配置页面：一个只监听回环地址的本地小 HTTP 服务。

页面管三件事：
    1. 服务自己的开关（`server_config.json`）：端口 / 令牌 / 加密 / 图片尺寸；
    2. 抖音账号（`storage-state.json`）—— 通过 `account_store` 接进来的
       `bridge/credentials.py`：看登录的是谁、发起扫码登录、退出登录；
    3. 手表上要列出哪些会话（`config.json` 的 `friends`）—— 通过 `friend_store` 接进来的
       `bridge/watch_friends.py`。名单可以点一下按钮从抖音私信列表里捞出来勾选，那个动作
       走 `scanner`（`bridge/friend_scan.py`）。保存前会验一遍，不合法绝不落盘。
    三个 store 没传时页面上对应的块整个不出现，不会给用户一个「点了没反应」的按钮。

为什么绑 `127.0.0.1` 而不是 `0.0.0.0`：
    这个页面**明文展示访问令牌**，令牌就是手表连过来的唯一凭据。绑到 0.0.0.0 等于
    把它发给整个局域网（宿舍/校园网里谁扫一下就拿到了）。桥接端口 8787 必须对外，
    配置页 8788 必须只对本机 —— 这两件事不要混在一个监听上。

不需要令牌，靠两件事挡跨站请求：
    1. 校验 `Host` 头（DNS rebinding：攻击者把域名解析到 127.0.0.1，浏览器发出的
       请求 `Host` 仍是攻击者的域名，一眼就能认出来）；
    2. 所有 POST 必须带自定义头 `X-Watch-Setup`。跨站的 `<form>` 发不出自定义头；
       跨站 `fetch` 带自定义头会先触发 CORS 预检，而我们从不回 CORS 头，预检失败
       请求压根不会发出去。这就是不需要额外令牌的 CSRF 防线。
    另外响应里一律 `Cache-Control: no-store`，别让带令牌的页面留在浏览器缓存里。
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from bridge.server_config import ConfigError, ServerConfig

LOGGER = logging.getLogger("douyin_watch")

HTML_PATH = Path(__file__).with_name("setup.html")
MAX_BODY_BYTES = 64 * 1024
CSRF_HEADER = "X-Watch-Setup"
# 8788 起顺延几个：用户可能同时开着旧窗口没退干净，为了一个占用就整页消失不值得
CANDIDATE_PORTS = (8788, 8789, 8790, 8791, 8792)
ALLOWED_HOSTS = ("127.0.0.1", "localhost", "[::1]")


def _host_ok(host_header: str) -> bool:
    name = host_header.strip().lower()
    if not name:
        return False
    if name.startswith("["):
        return name.startswith("[::1]")
    return name.split(":", 1)[0] in ("127.0.0.1", "localhost")


class _Handler(BaseHTTPRequestHandler):
    server_version = "WatchSetup/1.0"
    protocol_version = "HTTP/1.1"

    # 别把每个请求都写进控制台 —— 页面每 2 秒轮询一次状态，日志会被刷屏。
    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        LOGGER.debug("[setup] %s " + fmt, self.address_string(), *args)

    # ------------------------------------------------------------------ 工具

    app: "SetupWeb"

    def _send(self, status: HTTPStatus, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _fail(self, status: HTTPStatus, message: str) -> None:
        self._json({"ok": False, "error": message}, status)

    def _guard(self) -> bool:
        """两个跨站防线。返回 False 表示已经回过响应了。"""
        if not _host_ok(self.headers.get("Host", "")):
            self._fail(HTTPStatus.FORBIDDEN, "只接受来自本机的访问")
            return False
        if self.command == "POST" and self.headers.get(CSRF_HEADER) != "1":
            self._fail(HTTPStatus.FORBIDDEN, "缺少本页专用的请求头，请刷新页面后重试")
            return False
        return True

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise ConfigError("提交的数据过大")
        raw = self.rfile.read(length)
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ConfigError(f"数据格式错误：{exc}") from None
        if not isinstance(parsed, dict):
            raise ConfigError("提交的数据应为对象")
        return parsed

    # ------------------------------------------------------------------ 路由

    def do_GET(self) -> None:  # noqa: N802
        if not self._guard():
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html", "/setup.html"):
            return self._page()
        if path == "/api/state":
            return self._json(self.app.state())
        if path == "/favicon.ico":

            return self._send(HTTPStatus.NO_CONTENT, b"", "image/x-icon")
        return self._fail(HTTPStatus.NOT_FOUND, "无此地址")

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_POST(self) -> None:  # noqa: N802
        if not self._guard():
            return
        path = self.path.split("?", 1)[0]
        try:
            payload = self._body()
        except ConfigError as exc:
            return self._fail(HTTPStatus.BAD_REQUEST, str(exc))

        if path == "/api/config":
            return self._save(payload, mark_done=False)
        if path == "/api/launch":
            return self._save(payload, mark_done=False, launch=True)
        if path == "/api/token":
            return self._rotate_token(payload)
        if path == "/api/friends":
            return self._save_friends(payload)
        if path == "/api/friends/scan":
            return self._action(lambda: self.app.start_friend_scan())
        if path == "/api/friends/cancel":
            return self._action(lambda: self.app.cancel_friend_scan())
        if path == "/api/account/login":
            return self._account(lambda: self.app.start_login(payload))
        if path == "/api/account/cancel":
            return self._account(lambda: self.app.cancel_login())
        if path == "/api/account/logout":
            return self._account(lambda: self.app.logout_account())
        return self._fail(HTTPStatus.NOT_FOUND, "无此地址")

    # ------------------------------------------------------------------ 动作

    def _page(self) -> None:
        try:
            html = HTML_PATH.read_bytes()
        except OSError as exc:
            LOGGER.error("配置页面读不到: %s", exc)
            return self._fail(HTTPStatus.INTERNAL_SERVER_ERROR, f"页面文件无法读取：{exc}")
        self._send(HTTPStatus.OK, html, "text/html; charset=utf-8")

    def _save(self, payload: dict[str, Any], *, mark_done: bool, launch: bool = False) -> None:
        try:
            warnings = self.app.save(payload)
        except ConfigError as exc:
            return self._fail(HTTPStatus.BAD_REQUEST, str(exc))
        if launch:
            self.app.launch_event.set()
        self._json(
            {
                "ok": True,
                "warnings": warnings,
                "restart": self.app.restart_fields(),
                "launching": launch,
            }
        )

    def _rotate_token(self, payload: dict[str, Any]) -> None:
        try:
            token = self.app.rotate_token(payload)
        except ConfigError as exc:
            return self._fail(HTTPStatus.BAD_REQUEST, str(exc))
        self._json({"ok": True, "token": token, "restart": self.app.restart_fields()})

    def _save_friends(self, payload: dict[str, Any]) -> None:
        """保存 `config.json` 里的好友名单。

        这里接的是 `watch_friends.WatchFriendsError`，它也继承 `ValueError`；不直接
        import 那个类型是为了让本模块对「谁提供这份名单」保持无知 —— 只要给进来的
        东西有 `save()`，它就能用。
        """
        try:
            result = self.app.save_friends(payload)
        except ValueError as exc:
            return self._fail(HTTPStatus.BAD_REQUEST, str(exc))
        self._json({"ok": True, **result})

    def _action(self, action: Any) -> None:
        """要花几秒的浏览器动作（读好友列表 / 取消读）。

        和 `_account` 同理：只要抛 `ValueError` 就把消息原样发给用户；成功时顺手把
        最新状态一起回给页面，省掉一次 2 秒轮询的等待。
        """
        try:
            action()
        except ValueError as exc:
            return self._fail(HTTPStatus.BAD_REQUEST, str(exc))
        self._json({"ok": True, "friends_scan": self.app.scan_state()})

    def _account(self, action: Any) -> None:
        """账号相关的动作（登录 / 取消 / 退出）。

        和上面同理：只要给进来的东西会抛 `ValueError`（`AccountError` 是它的子类），
        消息就能原样发给用户。动作成功时**顺手把最新状态一起回给页面** —— 否则页面
        要等到下一次 2 秒轮询才知道「浏览器开没开起来」。
        """
        try:
            result = action()
        except ValueError as exc:
            return self._fail(HTTPStatus.BAD_REQUEST, str(exc))
        self._json({"ok": True, "account": self.app.account_state(), **(result or {})})


def _make_handler(app: "SetupWeb") -> type[_Handler]:
    return type("BoundHandler", (_Handler,), {"app": app})


class SetupWeb:
    """配置页服务。常驻在服务进程里，服务活着就能随时打开页面看/改配置。"""

    def __init__(
        self,
        config: ServerConfig,
        *,
        info_provider: Callable[[], dict[str, Any]] | None = None,
        on_save: Callable[[ServerConfig], None] | None = None,
        account_store: Any | None = None,
        friend_store: Any | None = None,
        scanner: Any | None = None,
    ) -> None:
        self.config = config
        # 正在运行的那份（构造 TcpBridgeServer 时用的值），用来算「哪些改动还没生效」
        self.runtime: ServerConfig | None = None
        # 手表端好友名单（config.json 的 friends）。没接的话页面上那一块整个不出现 ——
        # 不能给用户一个「勾了却没地方存」的复选框。
        self.friend_store = friend_store
        # 从抖音读会话列表的那个后台任务。没接时页面上按钮会说明「服务起来后才能读」。
        self.scanner = scanner
        # 抖音登录凭证（storage-state.json）。同样：没接就不出现。
        self.account_store = account_store
        self._info_provider = info_provider
        self._on_save = on_save
        # 首次启动向导：主线程等这个事件（页面点「保存并启动」时置位）
        self.launch_event = threading.Event()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.url = ""
        self.startup_note = ""

    # ------------------------------------------------------------------ 生命周期

    def start(self, port: int | None = None) -> str:
        """起服务。返回可访问的 URL；端口都被占了就返回空串。

        `port` 是给测试用的：**传 0 让系统分配一个临时端口**。测试绝不能去抢 8788 ——
        那上面可能正跑着用户自己的服务，Windows 的 SO_REUSEADDR 会让第二次绑定
        「成功」，于是测试连上的是对方、把对方的接口当成自己的用，写坏真实配置。
        （这个坑真踩过：一次自检把用户正在跑的那份 server_config.json 改了。）
        """
        candidates = [port] if port is not None else list(CANDIDATE_PORTS)
        for candidate in candidates:
            try:
                httpd = ThreadingHTTPServer(("127.0.0.1", candidate), _make_handler(self))
            except OSError:
                continue
            httpd.daemon_threads = True
            self._httpd = httpd
            self._thread = threading.Thread(target=httpd.serve_forever, name="setup-web", daemon=True)
            self._thread.start()
            actual = httpd.server_address[1]
            self.url = f"http://127.0.0.1:{actual}/"
            if port is None and actual != CANDIDATE_PORTS[0]:
                self.startup_note = f"（{CANDIDATE_PORTS[0]} 被占用，已改用 {actual}）"
            LOGGER.info("配置页面已就绪: %s", self.url)
            return self.url
        self.startup_note = f"（{CANDIDATE_PORTS[0]} 起的若干端口均被占用）"
        LOGGER.warning("配置页面无法启动：端口被占用 %s", CANDIDATE_PORTS)
        return ""

    def stop(self) -> None:
        if self._httpd is None:
            return
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except OSError:
            pass
        self._httpd = None

    @property
    def running(self) -> bool:
        return self._httpd is not None

    # ------------------------------------------------------------------ 数据

    def save(self, payload: dict[str, Any]) -> list[str]:
        """校验并落盘。返回警告（存下去了但你可能是这个意思的前一句）。"""
        warnings = self.config.apply_payload(payload)
        self.config.save()
        if self._on_save is not None:
            try:
                self._on_save(self.config)
            except Exception:  # noqa: BLE001 - 同步外部文件失败不该让页面 500
                LOGGER.exception("保存配置后的回调失败")
        LOGGER.info("服务端配置已更新（%s）", self.config.path)
        return warnings

    def rotate_token(self, payload: dict[str, Any]) -> str:
        """换一个令牌。长度沿用原来的 8 位十六进制：手表上要手输。"""
        token = secrets.token_hex(4)
        merged = dict(payload)
        merged["token"] = token
        self.save(merged)
        return token

    # ------------------------------------------------------------------ 手表端好友

    def save_friends(self, payload: dict[str, Any]) -> dict[str, Any]:
        """把勾选结果写回 `config.json` 的 `friends`。不合法会抛 ValueError（消息给用户看）。"""
        if self.friend_store is None:
            raise ValueError("本服务未接入 config.json，本页无法修改好友名单")
        return self.friend_store.save(payload)

    def friends_state(self) -> dict[str, Any] | None:
        """页面轮询用的好友名单快照。只读，异常一律吞掉（只读面板不该把页面搞挂）。"""
        if self.friend_store is None:
            return None
        try:
            return self.friend_store.describe()
        except Exception:  # noqa: BLE001
            LOGGER.exception("读取 config.json 的好友名单失败")
            return {"path": "", "exists": False, "error": "读取失败，请查看服务日志",
                    "enabled": [], "enabled_count": 0,
                    "editable": False, "readonly_reason": "", "hint": ""}

    def _require_scanner(self) -> Any:
        if self.scanner is None:
            raise ValueError("本服务未接入抖音会话，无法读取好友列表")
        return self.scanner

    def start_friend_scan(self) -> dict[str, Any]:
        return self._require_scanner().start()

    def cancel_friend_scan(self) -> dict[str, Any]:
        return self._require_scanner().cancel()

    def scan_state(self) -> dict[str, Any] | None:
        """读好友列表的进度。只读，异常一律吞掉。"""
        if self.scanner is None:
            return None
        try:
            return self.scanner.state()
        except Exception:  # noqa: BLE001
            LOGGER.exception("读取好友扫描状态失败")
            return None

    def restart_fields(self) -> list[str]:
        return self.config.restart_diff(self.runtime)

    # ------------------------------------------------------------------ 抖音账号

    def _require_account(self) -> Any:
        if self.account_store is None:
            raise ValueError("本服务未接入抖音账号（storage-state.json），本页无法修改")
        return self.account_store

    def start_login(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._require_account().start_login(payload.get("timeout"))

    def cancel_login(self) -> dict[str, Any]:
        return self._require_account().cancel_login()

    def logout_account(self) -> dict[str, Any]:
        return self._require_account().logout()

    def account_state(self) -> dict[str, Any] | None:
        """账号快照 + 登录动作状态。只读，异常一律吞掉（轮询接口不该把页面搞挂）。"""
        if self.account_store is None:
            return None
        try:
            return {**self.account_store.describe(), "login": self.account_store.login_state()}
        except Exception:  # noqa: BLE001
            LOGGER.exception("读取登录凭证失败")
            return {"path": "", "exists": False, "error": "读取失败，请查看服务日志", "login": {"state": "idle"}}

    def state(self) -> dict[str, Any]:
        """页面轮询的完整状态：文件里的值 + 正在跑的值 + 只读信息。"""
        info: dict[str, Any] = {}
        if self._info_provider is not None:
            try:
                info = self._info_provider() or {}
            except Exception:  # noqa: BLE001 - 只读面板不该把页面搞挂
                LOGGER.exception("采集状态信息失败")
                info = {}
        return {
            "ok": True,
            "url": self.url,
            "config": self.config.to_dict(),
            "runtime": self.runtime.to_dict() if self.runtime else None,
            "restart": self.restart_fields(),
            "friends": self.friends_state(),
            "friends_scan": self.scan_state(),
            "account": self.account_state(),
            "first_run": not self.config.setup_done,
            "awaiting_launch": not self.launch_event.is_set(),
            "config_path": str(self.config.path or ""),
            **info,
        }
