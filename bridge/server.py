"""手表端 HTTP 桥接服务（Python 标准库实现，不引入额外依赖）。

架构：
    手表浏览器  ──HTTP──▶  本模块（ThreadingHTTPServer）
                              │  提交协程
                              ▼
                        桥接 asyncio 事件循环 ──▶ 常驻 Chromium（抖音私信页）

鉴权：所有 /api/* 需要令牌，支持 ?token=xxx 或请求头 X-Auth-Token。
页面 / 与 /health 不需要令牌（页面本身不含隐私数据，真正的操作都在 /api/*）。
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from bridge import sticker_thumbs
from bridge.session import BridgeError, DouyinBridge


LOGGER = logging.getLogger("douyin_watch")

HTML_PATH = Path(__file__).with_name("watch.html")
MAX_BODY_BYTES = 16 * 1024
API_TIMEOUT_SECONDS = 120


class BridgeServer:
    def __init__(self, host: str, port: int, token: str) -> None:
        self.host = host
        self.port = port
        self.token = token
        self.bridge = DouyinBridge()
        self.loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._run_loop, name="bridge-loop", daemon=True)
        self._httpd: ThreadingHTTPServer | None = None
        self.startup_error: str | None = None

    # ------------------------------------------------------------------ 生命周期

    def start_backend(self) -> None:
        """在后台线程启动事件循环和浏览器会话。"""
        self._loop_thread.start()
        future = asyncio.run_coroutine_threadsafe(self.bridge.start(), self.loop)
        try:
            future.result(timeout=180)
        except Exception as exc:  # noqa: BLE001 - 启动失败要展示给用户
            self.startup_error = str(exc) or exc.__class__.__name__
            LOGGER.error("抖音会话启动失败: %s", self.startup_error)

    def serve_forever(self) -> None:
        handler = _make_handler(self)
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        # 手表浏览器会用多条连接并发拉取表情图，默认 backlog=5 偏小。
        self._httpd.request_queue_size = 128
        LOGGER.info("手表端服务已启动: http://%s:%d", self.host, self.port)
        self._httpd.serve_forever()

    def shutdown(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
        future = asyncio.run_coroutine_threadsafe(self.bridge.stop(), self.loop)
        try:
            future.result(timeout=30)
        except Exception:
            LOGGER.exception("关闭抖音会话失败")
        self.loop.call_soon_threadsafe(self.loop.stop)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    # ------------------------------------------------------------------ 协程调度

    def call(self, coro, timeout: int = API_TIMEOUT_SECONDS) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)

    def check_token(self, provided: str | None) -> bool:
        return bool(provided) and hmac.compare_digest(provided, self.token)


def _make_handler(server: BridgeServer):
    class Handler(BaseHTTPRequestHandler):
        server_version = "DouyinWatchBridge/1.0"
        protocol_version = "HTTP/1.1"

        # -------------------------------------------------------------- 工具

        def log_message(self, fmt: str, *args) -> None:  # 隐私：不记录查询串里的内容
            LOGGER.debug("%s - %s", self.address_string(), fmt % args)

        def _elapsed_ms(self) -> str:
            started = getattr(self, "_started_at", None)
            if started is None:
                return "0"
            return f"{(time.perf_counter() - started) * 1000:.0f}"

        def _send_json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Elapsed-Ms", self._elapsed_ms())
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self) -> None:
            try:
                stat = HTML_PATH.stat()
                body = HTML_PATH.read_bytes()
            except OSError:
                self._send_json({"error": "手表页面文件缺失"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            etag = f'"{int(stat.st_mtime)}-{stat.st_size}"'
            if self.headers.get("If-None-Match") == etag:
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("ETag", etag)
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            # no-cache + ETag：每次都回来问一句，没改就回 304，手表复用本地副本，
            # 打开页面时省掉整个 HTML 的重新下载。
            self.send_header("Cache-Control", "no-cache")
            self.send_header("ETag", etag)
            self.end_headers()
            self.wfile.write(body)

        def _auth(self, query: dict[str, list[str]]) -> bool:
            provided = self.headers.get("X-Auth-Token")
            if not provided:
                values = query.get("token") or []
                provided = values[0] if values else None
            if server.check_token(provided):
                return True
            self._send_json({"error": "令牌无效，请在手表页面填入正确的访问令牌"}, HTTPStatus.UNAUTHORIZED)
            return False

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY_BYTES:
                raise ValueError("请求体为空或过大")
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("请求体不是合法 JSON") from exc
            if not isinstance(payload, dict):
                raise ValueError("请求体必须是 JSON 对象")
            return payload

        # -------------------------------------------------------------- 路由

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
            self._started_at = time.perf_counter()
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)

            if parsed.path in ("/", "/index.html"):
                self._send_html()
                return
            if parsed.path == "/health":
                self._send_json(
                    {
                        "ok": True,
                        "ready": server.bridge.ready,
                        "startup_error": server.startup_error,
                        "media_cache": server.bridge.media_stats(),
                    }
                )
                return
            if not parsed.path.startswith("/api/"):
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            if not self._auth(query):
                return

            if parsed.path == "/api/status":
                self._handle_status()
                return
            if parsed.path == "/api/messages":
                name = (query.get("name") or [""])[0].strip()
                limit = _safe_int((query.get("limit") or ["30"])[0], default=30, minimum=1, maximum=100)
                known_rev = (query.get("rev") or [""])[0].strip()
                if not name:
                    self._send_json({"error": "缺少 name 参数"}, HTTPStatus.BAD_REQUEST)
                    return
                self._handle_messages(name, limit, known_rev)
                return
            if parsed.path == "/api/conversations":
                self._run(lambda: server.bridge.read_conversations())
                return
            if parsed.path == "/api/diag":
                # 只读诊断：量发送确认链路各步骤的往返耗时，用于定位「发送慢」。
                self._run(lambda: server.bridge.diagnose_send_path(), timeout=60)
                return
            if parsed.path == "/api/media":
                url = (query.get("url") or [""])[0]
                if not url:
                    self._send_json({"error": "缺少 url 参数"}, HTTPStatus.BAD_REQUEST)
                    return
                self._send_media(url)
                return
            if parsed.path == "/api/stickers":
                # 表情库只读本地 JSON，跟抖音会话是否就绪无关，所以不走 _run。
                try:
                    self._send_json(server.bridge.sticker_library_payload())
                except BridgeError as exc:
                    self._send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
                return
            if parsed.path == "/api/sticker_thumb":
                ref = (query.get("id") or query.get("name") or [""])[0].strip()
                if not ref:
                    self._send_json({"error": "缺少 id 参数"}, HTTPStatus.BAD_REQUEST)
                    return
                self._send_sticker_thumb(ref)
                return

            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            self._started_at = time.perf_counter()
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            if parsed.path != "/api/send":
                self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                return
            if not self._auth(query):
                return
            try:
                payload = self._read_json()
            except ValueError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
                return

            name = str(payload.get("name") or "").strip()
            text = str(payload.get("text") or "")
            sticker = payload.get("sticker")
            sticker = str(sticker).strip() if isinstance(sticker, str) and sticker.strip() else None
            if not name:
                self._send_json({"error": "缺少 name 字段"}, HTTPStatus.BAD_REQUEST)
                return
            self._run(lambda: server.bridge.send_message(name, text, sticker), timeout=180)

        # -------------------------------------------------------------- 执行

        def _handle_messages(self, name: str, limit: int, known_rev: str) -> None:
            """读会话。带上 rev 且内容未变时只回一个 unchanged —— 手表端连渲染都省了。"""
            if server.startup_error:
                self._send_json({"error": server.startup_error}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            if not server.bridge.ready:
                self._send_json({"error": "抖音会话正在启动，请稍候"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            try:
                result = server.call(server.bridge.read_chat(name, limit))
            except BridgeError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
                return
            except TimeoutError:
                self._send_json({"error": "操作超时，请重试"}, HTTPStatus.GATEWAY_TIMEOUT)
                return
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("读取消息失败")
                self._send_json({"error": str(exc) or exc.__class__.__name__}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            rev = result.get("rev") or ""
            if known_rev and rev and known_rev == rev:
                self._send_json({"friend": name, "rev": rev, "unchanged": True, "messages": []})
                return
            self._send_json(result)

        def _handle_status(self) -> None:
            if server.startup_error:
                self._send_json({"ready": False, "error": server.startup_error}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            if not server.bridge.ready:
                self._send_json({"ready": False, "error": "抖音会话正在启动，请稍候"}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            try:
                data = server.call(server.bridge.status(), timeout=30)
            except Exception as exc:  # noqa: BLE001
                self._send_json({"ready": False, "error": str(exc)}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            self._send_json(data)

        def _send_media(self, url: str) -> None:
            if server.startup_error:
                self._send_json({"error": server.startup_error}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            try:
                content_type, body, etag = server.call(server.bridge.fetch_media(url), timeout=40)
            except BridgeError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
                return
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("代理图片失败")
                self._send_json({"error": str(exc) or "图片代理失败"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            quoted = f'"{etag}"'
            if self.headers.get("If-None-Match") == quoted:
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("ETag", quoted)
                self.send_header("Cache-Control", "private, max-age=600")
                self.send_header("X-Elapsed-Ms", self._elapsed_ms())
                self.end_headers()
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", quoted)
            self.send_header("Cache-Control", "private, max-age=600")
            self.send_header("X-Elapsed-Ms", self._elapsed_ms())
            self.end_headers()
            self.wfile.write(body)

        def _send_sticker_thumb(self, ref: str) -> None:
            """表情库的本地缩略图。

            跟 `_send_media` 的区别：这张图在扫描时就落到磁盘了，不碰网络、
            也不要求抖音会话就绪，所以缓存时间可以给得很长（id 由内容派生，
            同一 id 的图内容不会变）。

            按手表默认尺寸 `WATCH_PX` 取派生小图：原图里有 1MB+ 的动图，
            而这个接口就是给手表显示图标用的，没有理由把原图搬出去。
            """
            try:
                content_type, body, etag = server.bridge.sticker_thumb(ref, sticker_thumbs.WATCH_PX)
            except BridgeError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.NOT_FOUND)
                return
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("读取表情缩略图失败")
                self._send_json({"error": str(exc) or "缩略图读取失败"}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            quoted = f'"{etag}"'
            if self.headers.get("If-None-Match") == quoted:
                self.send_response(HTTPStatus.NOT_MODIFIED)
                self.send_header("ETag", quoted)
                self.send_header("Cache-Control", "private, max-age=86400")
                self.send_header("X-Elapsed-Ms", self._elapsed_ms())
                self.end_headers()
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("ETag", quoted)
            self.send_header("Cache-Control", "private, max-age=86400")
            self.send_header("X-Elapsed-Ms", self._elapsed_ms())
            self.end_headers()
            self.wfile.write(body)

        def _run(self, factory, timeout: int = API_TIMEOUT_SECONDS) -> None:
            if server.startup_error:
                self._send_json({"error": server.startup_error}, HTTPStatus.SERVICE_UNAVAILABLE)
                return
            try:
                result = server.call(_await(factory()), timeout=timeout)
            except BridgeError as exc:
                self._send_json({"error": str(exc)}, HTTPStatus.CONFLICT)
                return
            except TimeoutError:
                self._send_json({"error": "操作超时，请重试"}, HTTPStatus.GATEWAY_TIMEOUT)
                return
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("处理请求失败")
                self._send_json({"error": str(exc) or exc.__class__.__name__}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
            self._send_json(result)

    return Handler


async def _await(value):
    """允许传入协程工厂的结果（协程或直接值）。"""
    if asyncio.iscoroutine(value):
        return await value
    return value


def _safe_int(raw: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))
