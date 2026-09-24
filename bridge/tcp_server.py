"""手表端桥接服务 —— 裸 TCP 长连接（替代原 HTTP 服务）。

为什么换掉 HTTP，见 bridge/protocol.py 顶部。这里只说服务端的结构：

    手表 ──TCP 长连接──▶ ClientSession（每连接一线程）
                            │  读循环：解帧 → 解密 → 分发
                            │  推送线程：订阅的会话变了就主动推
                            ▼
                      线程池 ──▶ DouyinBridge（常驻 Chromium 会话）

三个刻意的设计：

1. **读循环只做分帧和分发**，任何会阻塞的操作（读会话、发消息、拉图）都丢给线程池。
   否则一个慢请求会把这条连接上所有的帧都堵住，心跳也发不出去。
2. **推送放在服务端**。原来手表每 2 秒发一次请求，绝大多数结果是「什么都没变」——
   白白唤醒一次无线电。现在由电脑侧轮询浏览器（电脑是插电的，随便跑），
   只在内容真的变了才推一帧过去。
3. **图片在服务端降采样**。穿透带宽通常只有 1~5 Mbps，而 65 张表情原图合起来
   7.3MB，首屏要等一分钟。缩到显示所需的分辨率后总大小降一个数量级。
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import io
import logging
import socket
import socketserver
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from bridge.crypto import (
    DIR_CLIENT_TO_SERVER,
    DIR_SERVER_TO_CLIENT,
    PlainChannel,
    SecureChannel,
    derive_session_key,
    generate_keypair,
    random_public_nonce,
)
from bridge.protocol import (
    FLAG_ERROR,
    FLAG_MORE,
    FLAG_PUSH,
    HELLO_TIMEOUT_SECONDS,
    MAX_PAYLOAD,
    PROTOCOL_VERSION,
    TYPE_HELLO,
    TYPE_IMAGE,
    TYPE_PING,
    TYPE_PONG,
    TYPE_REQ,
    TYPE_RES,
    FrameReader,
    ProtocolError,
    encode_json,
    pack_frame,
    unpack_json,
)
from bridge.session import BridgeError, DouyinBridge

try:  # 让路异常定义在 app.douyin（扫描器的家），这里只做类型捕获用
    from app.douyin import RefreshYielded
except ImportError:  # pragma: no cover - app 包缺失时不该连服务都起不来
    class RefreshYielded(RuntimeError):
        pass

LOGGER = logging.getLogger("douyin_watch")

# 订阅推送的间隔。前台 1.5s 对得上人手能感知的延迟；切后台后放宽，
# 一条已经没人看的会话不该继续让浏览器每秒读一次 DOM。
PUSH_ACTIVE_SECONDS = 1.5
PUSH_IDLE_SECONDS = 6.0

# 连续这么多轮读下来内容没变之后，把节奏放宽到 PUSH_LAZY_SECONDS。
# 慢下来的理由不是省 CPU（读一次十几毫秒），而是每读一次都要独占浏览器那把
# asyncio 锁 —— 锁被轮询拿着的每一刻，用户的操作都在排队。
PUSH_LAZY_AFTER = 3
PUSH_LAZY_SECONDS = 2.5

# 图片降采样目标边长（像素）。手表上表情格约 0.2r ≈ 47vp、消息气泡约 56vp，
# 2 倍屏下 96~112px 已经看不出差别，给到 128 留一点余量。
STICKER_PX = 72
MEDIA_PX = 128

# 降采样结果缓存。键是 (来源, 标识, 目标边长)，值是 (content-type, bytes)。
SCALED_CACHE_MAX = 400

PING_INTERVAL_SECONDS = 15.0
PING_TIMEOUT_SECONDS = 60.0

# 表情库自动刷新的最小间隔。刷一次要切分类栏 + 下载几十张缩略图（15~40 秒），
# 期间浏览器被独占、用户的操作要排队 —— 所以不能太勤。
# 选 20 分钟：这是在「收藏了表情多久能在手表上看到」和「多久打断一次用户操作」之间取的平衡。
# 只在客户端连着、且浏览器空闲时才做（见 _maybe_refresh_stickers）。
STICKER_REFRESH_INTERVAL_SECONDS = 20 * 60

# 手表踢一脚（打开表情面板）时的最小间隔。比自动刷新短得多：用户正要挑表情，
# 这时候的「新鲜」最值钱。刷新本身也已经变快 —— 只有新增的表情才下载缩略图
# （见 scan_stickers._download_thumbs），其余走本地已有文件。
STICKER_REFRESH_KICK_INTERVAL_SECONDS = 90

# 批量载荷（整库表情缩略图）的分片大小。
#
# 不用 MAX_PAYLOAD（4MB）：整库 643KB 挤成一帧的话，两端都要做一次 643KB 的
# AES-GCM 单次运算、一次 643KB 的 socket 写；手表那边的收包缓冲还要靠反复
# 拼接来攒这一大坨（它的分帧器是按「收包」拼接的）。拆成 128KB 一片，每一帧
# 都是能立刻解完、立刻交给上层的规模，内存峰值也从「整包」降到「一片」。
BULK_CHUNK = 128 * 1024

# 一次刷新被用户操作打断后，多久再试一次。刷新会独占浏览器十几秒，被打断说明
# 此刻用户正在用（多数就是正在发消息）—— 隔一小会儿再试，而不是立刻顶上去。
STICKER_REFRESH_YIELD_RETRY_SECONDS = 15
# 连续被打断这么多次就先放弃，等下一次自然触发（免得每 15 秒就顶一次用户操作）。
STICKER_REFRESH_YIELD_MAX_ATTEMPTS = 6


class TcpBridgeServer:
    """手表端桥接服务主体。对外只有 start_backend / serve_forever / shutdown。"""

    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        *,
        crypto: str = "gcm",
        sticker_px: int = STICKER_PX,
        media_px: int = MEDIA_PX,
    ) -> None:
        self.host = host
        self.port = port
        self.token = token
        # "gcm" 强制加密；"plain" 明文（逃生舱，见 bridge/crypto.py 的说明）
        self.crypto = crypto
        self.sticker_px = sticker_px
        self.media_px = media_px
        self.bridge = DouyinBridge()
        self.startup_error: str | None = None
        # DouyinBridge 本身只是「一串要串行执行的浏览器操作」，它不持有事件循环。
        # 循环和承载它的线程归服务端所有 —— 请求从 TCP 的工作线程进来，用
        # run_coroutine_threadsafe 把协程投进这个循环，浏览器侧始终是单线程的。
        self.loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._run_loop, name="bridge-loop", daemon=True)
        self._executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="bridge-io")
        self._sessions: set[ClientSession] = set()
        self._sessions_lock = threading.Lock()
        self._scaled_cache: dict[tuple, tuple[str, bytes]] = {}
        self._scaled_lock = threading.Lock()
        # 「界面发起的请求」计数。浏览器那一侧靠一把 asyncio 锁串行，而推送轮询
        # 每 1.5 秒就要独占它读一次 DOM —— 用户的点击落在这个窗口里就只能排队，
        # 表现出来就是「点一下要愣半秒」。这个计数让推送在有人等锁时主动让路。
        self._interactive_lock = threading.Lock()
        self._interactive = 0
        # 表情库自动刷新的节流状态。放在服务级而不是连接级：手表断线重连很频繁，
        # 按连接记「上次刷新时间」会让每次连上都立刻刷一次（10 秒里浏览器被独占，
        # 用户的开屏操作全在排队）。放这里就是全局「20 分钟最多一次」。
        self._last_sticker_refresh_at = 0.0
        self._sticker_refresh_lock = threading.Lock()
        self._sticker_refresh_running = False
        # 连续「这一轮没刷成」（让路 / 到点时浏览器还被占着）的次数。到上限就先放下，
        # 免得用户一直在用的时候每 15 秒去顶一次他的操作。
        self._sticker_refresh_waits = 0
        self._httpd: socketserver.ThreadingTCPServer | None = None

    # ------------------------------------------------------------------ 生命周期

    def start_backend(self) -> None:
        """启动抖音会话。与原 HTTP 版一致：阻塞到就绪或失败（失败不抛出，记在 startup_error）。"""
        self._loop_thread.start()
        future = asyncio.run_coroutine_threadsafe(self.bridge.start(), self.loop)
        try:
            future.result(timeout=180)
        except Exception as exc:  # noqa: BLE001 - 启动失败要展示给用户
            self.startup_error = str(exc) or exc.__class__.__name__
            LOGGER.error("抖音会话启动失败: %s", self.startup_error)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def serve_forever(self) -> None:
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        server = _make_server_class(family)((self.host, self.port), _make_handler(self))
        self._httpd = server
        LOGGER.info(
            "手表端 TCP 服务已启动: %s:%d（加密：%s）",
            self.host,
            self.port,
            {"gcm": "AES-256-GCM 强制", "plain": "明文", "auto": "跟随客户端"}.get(self.crypto, self.crypto),
        )
        try:
            server.serve_forever()
        finally:
            self._close_sessions()

    def shutdown(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
        self._close_sessions()
        self._executor.shutdown(wait=False, cancel_futures=True)
        # 事件循环可能压根没起来（比如会话启动就失败了），那种情况下投协程进去
        # 只会干等到超时，所以先确认线程活着。
        if self._loop_thread.is_alive():
            try:
                self.call(self.bridge.stop(), timeout=30)
            except Exception:  # noqa: BLE001 - 关闭阶段的异常没有挽救价值
                LOGGER.exception("关闭抖音会话失败")
            self.loop.call_soon_threadsafe(self.loop.stop)

    def _close_sessions(self) -> None:
        with self._sessions_lock:
            sessions = list(self._sessions)
            self._sessions.clear()
        for session in sessions:
            session.close()

    # ------------------------------------------------------------------ 桥接转发

    def maybe_refresh_stickers(self, *, sub_active: bool) -> None:
        """到点了就排一次表情库刷新（不阻塞调用方）。

        触发条件三者都满足才做：
        1. 距上次刷新 ≥ STICKER_REFRESH_INTERVAL_SECONDS（服务级节流，不是连接级 ——
           手表断线重连很频繁，按连接记会让每次连上都刷一次）；
        2. 没有界面请求在等浏览器；
        3. **用户没正盯着聊天页**（订阅处于 active）。刷新要独占浏览器十几秒，
           在用户盯着屏幕打字时做，他感觉到的就是「卡住了」；等他切走/息屏再做，
           下次打开时新表情已经在了 —— 这才是「自动出现」该有的样子。
        """
        if sub_active:
            return
        self._start_sticker_refresh(STICKER_REFRESH_INTERVAL_SECONDS)

    def kick_sticker_refresh(self) -> bool:
        """手表打开表情面板时踢的一脚：排一次后台刷新，立刻返回、不等结果。

        与自动刷新的唯一区别是**不看 sub_active** —— 用户已经站在表情网格前了，
        正是最需要新表情的时刻（自动刷新恰恰在此时让路，这就是「收藏了表情、
        手表上却迟迟不出现」的原因）。刷完库文件被改写，推送循环的 mtime 检查
        会把新状态推给手表，那边不用等这次调用的结果。

        这是用户主动触发的一次，所以把「连续没排上」的计数清零：他刚做了个明确的
        动作，值得重新给足重试机会。
        """
        with self._sticker_refresh_lock:
            self._sticker_refresh_waits = 0
        return self._start_sticker_refresh(STICKER_REFRESH_KICK_INTERVAL_SECONDS)

    def _start_sticker_refresh(self, min_interval: float) -> bool:
        """节流 + 排一次刷新。返回是否真的排上了。"""
        with self._sticker_refresh_lock:
            if self._sticker_refresh_running:
                return False
            now = time.time()
            if now - self._last_sticker_refresh_at < min_interval:
                return False
            if self.interactive_busy():
                return False
            self._last_sticker_refresh_at = now
            self._sticker_refresh_running = True
        try:
            self.submit(self._run_sticker_refresh)
        except RuntimeError:
            # 线程池已关（服务在退出）——把标记放回去，别让状态卡住。
            with self._sticker_refresh_lock:
                self._sticker_refresh_running = False
            return False
        return True

    def _run_sticker_refresh(self) -> None:
        """在线程池里真正执行一次表情库刷新。

        带让路探针（`self.interactive_busy`）：这一整段要独占浏览器十几秒，而它
        常常是被「用户刚打开表情面板」踢起来的 —— 用户下一步很可能就是发消息。
        刷新在每段等待之间瞄一眼探针，有人等着就抛 `RefreshYielded` 主动走人，
        然后隔一小会儿再排一次。没有这层，用户看到的就是「打开表情面板之后
        发消息特别慢」—— 那正是这次改动要避免的事。

        **这里刻意用 `call` 而不是 `call_interactive`**：探针问的就是
        `interactive_busy()`，而 `call_interactive` 会把计数器顶起来，刷新自己
        就把探针点亮了 —— 第一下检查就会「有人在等」然后立刻放弃，永远刷不成。
        刷新是后台任务，本来也不该算作「界面请求」。
        """
        started = time.time()
        try:
            result = self.call(
                self.bridge.refresh_stickers(should_yield=self.interactive_busy),
                timeout=300,
            )
            if result.get("changed"):
                LOGGER.info(
                    "表情库自动刷新：新增 %s 项（共 %s 项），%.1fs",
                    result.get("added"),
                    result.get("items"),
                    time.time() - started,
                )
            else:
                LOGGER.debug("表情库自动刷新：没有变化（共 %s 项）", result.get("items"))
            with self._sticker_refresh_lock:
                self._sticker_refresh_waits = 0
        except RefreshYielded:
            waits = self._note_sticker_refresh_wait()
            LOGGER.info("表情库刷新让路（用户正在操作浏览器）：第 %d 次", waits)
            if waits < STICKER_REFRESH_YIELD_MAX_ATTEMPTS:
                self._schedule_sticker_refresh_retry()
            else:
                self._give_up_sticker_refresh()
            return
        except Exception as exc:  # noqa: BLE001 - 自动刷新失败不该影响任何用户操作
            # 库文件没被改写，推送循环不会推新状态，用户那边什么都没发生 —— 这是对的。
            # 下一次到点会再试。
            LOGGER.info("表情库自动刷新失败（下次再试）：%s", exc)
        finally:
            with self._sticker_refresh_lock:
                self._sticker_refresh_running = False

    def _note_sticker_refresh_wait(self) -> int:
        """记一次「这一轮没刷成」，返回累计次数；同时把节流钟往前拨。

        往前拨是必须的：不拨的话 90 秒的最小间隔会把这次让路变成「这次干脆不刷了」，
        而用户此刻正等着看到新收藏的表情。
        """
        with self._sticker_refresh_lock:
            self._sticker_refresh_waits += 1
            waits = self._sticker_refresh_waits
            self._last_sticker_refresh_at = time.time() - STICKER_REFRESH_KICK_INTERVAL_SECONDS
        return waits

    def _give_up_sticker_refresh(self) -> None:
        """连着被打断太多次就不顶了，等下一次自然触发（用户再开一次面板 / 20 分钟到点）。"""
        LOGGER.info(
            "表情库刷新连续 %d 次没能排上，先放下，等下一次触发",
            STICKER_REFRESH_YIELD_MAX_ATTEMPTS,
        )
        with self._sticker_refresh_lock:
            self._sticker_refresh_waits = 0

    def _schedule_sticker_refresh_retry(self) -> None:
        """让路之后隔一会儿再排一次。

        必须自己续期，不能只等下一次自然触发：自动刷新要等 20 分钟、踢一脚要等用户
        再打开一次表情面板 —— 两种都等于「这次让路 = 新收藏的表情多半今天就看不到了」，
        正好背离这次改动的目的。
        """
        timer = threading.Timer(STICKER_REFRESH_YIELD_RETRY_SECONDS, self._retry_sticker_refresh)
        timer.daemon = True
        timer.start()

    def _retry_sticker_refresh(self) -> None:
        """定时器到点：再排一次；这次要是还没排上（浏览器仍被占着），就接着等下一轮。"""
        if self._start_sticker_refresh(STICKER_REFRESH_KICK_INTERVAL_SECONDS):
            return
        if self._note_sticker_refresh_wait() >= STICKER_REFRESH_YIELD_MAX_ATTEMPTS:
            self._give_up_sticker_refresh()
            return
        self._schedule_sticker_refresh_retry()

    def call(self, coro, timeout: int = 120) -> Any:
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)

    def call_interactive(self, coro, timeout: int = 120) -> Any:
        """界面发起的调用（读会话、发消息、拉图……）。

        与 `call` 唯一的区别是在途期间把计数加上：推送轮询看到有人在等锁就先让路，
        不跟用户的操作抢浏览器。服务端只有一条连接上的两种来源，分开只为这一个目的。
        """
        with self._interactive_lock:
            self._interactive += 1
        try:
            return self.call(coro, timeout=timeout)
        finally:
            with self._interactive_lock:
                self._interactive -= 1

    def interactive_busy(self) -> bool:
        with self._interactive_lock:
            return self._interactive > 0

    def submit(self, fn, *args) -> None:
        """把阻塞操作丢给线程池，读循环立刻回去继续收帧。"""
        try:
            self._executor.submit(fn, *args)
        except RuntimeError:
            LOGGER.debug("线程池已关闭，丢弃任务")

    def register(self, session: "ClientSession") -> None:
        with self._sessions_lock:
            self._sessions.add(session)

    def unregister(self, session: "ClientSession") -> None:
        with self._sessions_lock:
            self._sessions.discard(session)

    @property
    def session_count(self) -> int:
        with self._sessions_lock:
            return len(self._sessions)

    # ------------------------------------------------------------------ 图片

    def scaled_image(self, source: str, key: str, px: int) -> tuple[str, bytes]:
        """取一张图并降到目标边长。

        `source` 是 "sticker"（本地缩略图，不碰网络）或 "media"（抖音 CDN，
        需要借用浏览器的登录态）。
        """
        cache_key = (source, key, px)
        with self._scaled_lock:
            cached = self._scaled_cache.get(cache_key)
        if cached is not None:
            return cached

        if source == "sticker":
            # 本地文件，不碰浏览器也不碰网络，所以直接同步读。
            # 把目标边长一并告诉它：派生小图按这个尺寸预生成，命中后连缩放都不用做。
            content_type, body, _ = self.bridge.sticker_thumb(key, px)
        else:
            # 走 Chromium 的登录态去抖音 CDN 取，是协程，必须投回事件循环里跑。
            # 用 call_interactive：用户正盯着这几张图，推送轮询该给它让路。
            content_type, body, _ = self.call_interactive(self.bridge.fetch_media(key), timeout=40)

        out_type, out_body = _downscale(content_type, body, px)
        # 只缓存降采样后的结果：原图动辄上百 KB，缓存它没有意义，
        # 真正会被反复要的是这些几十 KB 的小图。
        with self._scaled_lock:
            if len(self._scaled_cache) >= SCALED_CACHE_MAX:
                for stale in list(self._scaled_cache)[: SCALED_CACHE_MAX // 4]:
                    self._scaled_cache.pop(stale, None)
            self._scaled_cache[cache_key] = (out_type, out_body)
        return out_type, out_body


def _downscale(content_type: str, body: bytes, px: int) -> tuple[str, bytes]:
    """把图片缩到最长边 px。失败就原样返回 —— 宁可发大图，也不能让图消失。"""
    if px <= 0:
        return content_type, body
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - 依赖缺失时退化成不缩放
        return content_type, body

    try:
        with Image.open(io.BytesIO(body)) as image:
            image.load()
            if max(image.size) <= px:
                return content_type, body
            ratio = px / float(max(image.size))
            size = (max(1, round(image.width * ratio)), max(1, round(image.height * ratio)))
            resized = image.resize(size, Image.Resampling.LANCZOS)
            # 表情带透明通道，转成 JPEG 会把透明区糊成黑块，所以按有没有 alpha 分流。
            has_alpha = resized.mode in ("RGBA", "LA", "P")
            buffer = io.BytesIO()
            if has_alpha:
                resized.convert("RGBA").save(buffer, format="PNG", optimize=True)
                return "image/png", buffer.getvalue()
            resized.convert("RGB").save(buffer, format="JPEG", quality=82, optimize=True)
            return "image/jpeg", buffer.getvalue()
    except Exception:  # noqa: BLE001 - 任何解码异常都不该让这张图取不到
        LOGGER.debug("降采样失败，回退原图", exc_info=True)
        return content_type, body


class ClientSession:
    """一条手表连接。读循环在服务器分配的那个线程里跑，推送各自再开一条。"""

    def __init__(self, app: TcpBridgeServer, sock: socket.socket, addr) -> None:
        self.app = app
        self.sock = sock
        self.addr = addr
        self.reader = FrameReader(sock.recv)
        self.channel: PlainChannel | SecureChannel = PlainChannel()
        self._send_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._alive = True
        self._last_pong = time.monotonic()
        self._subscription: dict[str, Any] | None = None
        self._sub_lock = threading.Lock()
        # 连续「读了但内容没变」的轮数，只被推送线程读写，用来放宽轮询节奏
        self._unchanged_streak = 0
        self._push_thread: threading.Thread | None = None
        # 上次推状态时的表情库 mtime。连接建立时就记下基准：如果手表停在列表页
        # 期间电脑端重扫了表情库，推送线程首轮就能发现变化并补推状态。
        try:
            self._pushed_library_mtime: float | None = self.app.bridge.library_mtime()
        except Exception:  # noqa: BLE001 - 会话还没起完时读不到，首轮再建基准
            self._pushed_library_mtime = None
        self._ping_thread: threading.Thread | None = None
        self.label = f"{addr[0]}:{addr[1]}"

    # ------------------------------------------------------------------ 收发

    @property
    def alive(self) -> bool:
        return self._alive

    def send(self, frame_type: int, req_id: int, payload: bytes = b"", flags: int = 0) -> bool:
        """发一帧。返回是否成功；失败即认为这条连接已经废了。

        注意锁的范围要盖住**加密**，不能只包住 sendall。SecureChannel 的发送计数器
        是「读—用—加一」，而这条连接上天然会并发发送：推送线程每 1.5 秒推一次，
        同一时刻手表可能正在拉十几张表情图，那些应答来自线程池的多个 worker。
        两帧拿到同一个 nonce 就同时踩中 GCM 的两个致命后果 —— 明文异或泄露，
        以及对方从那一帧起接收计数器错位、之后每一帧都解不开（表现为「连接莫名断了」）。
        """
        with self._send_lock:
            if not self._alive:
                return False
            counter = getattr(self.channel, "_send_counter", -1)
            try:
                blob = self.channel.encrypt(payload)
                raw = pack_frame(frame_type, req_id, blob, flags)
            except Exception:  # noqa: BLE001 - 打包/加密失败属于本地 bug，不能让线程炸掉
                LOGGER.exception("打包帧失败（%s）", self.label)
                return False
            try:
                self.sock.sendall(raw)
                LOGGER.info(
                    "发→ %s type=0x%02x req=%d flags=0x%02x 密文=%d counter=%d 头=%s",
                    self.label, frame_type, req_id, flags, len(blob), counter, blob[:8].hex(),
                )
                return True
            except OSError:
                self._alive = False
                return False

    def send_json(self, frame_type: int, req_id: int, data: Any, flags: int = 0) -> bool:
        return self.send(frame_type, req_id, encode_json(data), flags)

    def send_error(self, req_id: int, message: str, op: str = "") -> None:
        self.send_json(
            TYPE_RES,
            req_id,
            {"op": op, "ok": False, "error": message},
            FLAG_ERROR,
        )

    def close(self) -> None:
        with self._close_lock:
            if not self._alive:
                return
            self._alive = False
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass
        self.app.unregister(self)

    # ------------------------------------------------------------------ 主流程

    def run(self) -> None:
        self.app.register(self)
        LOGGER.info("手表已连接：%s", self.label)
        try:
            if not self._handshake():
                return
            self.sock.settimeout(None)
            self._start_ping()
            self._read_loop()
        except (ConnectionError, OSError) as exc:
            LOGGER.info("手表断开：%s（%s）", self.label, exc)
        except ProtocolError as exc:
            LOGGER.warning("协议错误，断开 %s：%s", self.label, exc)
        except Exception:  # noqa: BLE001 - 单条连接的意外不能拖垮服务
            LOGGER.exception("连接处理异常：%s", self.label)
        finally:
            self.close()
            LOGGER.info("连接结束：%s（当前在线 %d）", self.label, self.app.session_count)

    def _handshake(self) -> bool:
        self.sock.settimeout(HELLO_TIMEOUT_SECONDS)
        try:
            frame_type, _, _, payload = self.reader.read_frame()
        except (ConnectionError, OSError, ProtocolError) as exc:
            LOGGER.info("握手失败（%s）：%s", self.label, exc)
            return False
        if frame_type != TYPE_HELLO:
            LOGGER.warning("握手失败（%s）：首帧类型是 0x%02x，应为 hello", self.label, frame_type)
            return False

        try:
            hello = unpack_json(payload)
        except ProtocolError as exc:
            self.send_json(TYPE_HELLO, 0, {"v": PROTOCOL_VERSION, "ok": False, "error": str(exc)})
            return False
        if not isinstance(hello, dict):
            self.send_json(TYPE_HELLO, 0, {"v": PROTOCOL_VERSION, "ok": False, "error": "握手内容必须是对象"})
            return False

        version = int(hello.get("v") or 0)
        if version != PROTOCOL_VERSION:
            self.send_json(
                TYPE_HELLO,
                0,
                {
                    "v": PROTOCOL_VERSION,
                    "ok": False,
                    "error": f"协议版本不一致：服务端 {PROTOCOL_VERSION}，客户端 {version}",
                },
            )
            return False

        provided = str(hello.get("token") or "")
        if not hmac.compare_digest(provided, self.app.token):
            # 令牌比对失败也要回一帧，否则手表只能看到一个「连接被关闭」，无从判断原因。
            self.send_json(
                TYPE_HELLO, 0, {"v": PROTOCOL_VERSION, "ok": False, "error": "令牌无效，请重新填写"}
            )
            LOGGER.warning("握手被拒（%s）：令牌无效", self.label)
            return False

        mode = str(hello.get("crypto") or "plain")
        if self.app.crypto == "plain":
            mode = "plain"
        elif self.app.crypto == "auto":
            # 排障用：客户端要什么给什么。手表端的加密实现万一在某个系统版本上
            # 跑不通，改手表一个开关就能继续用，不必同时重启服务端。
            # 代价是中间人可以改写握手把连接降级成明文，所以不作为默认。
            pass
        elif mode != "gcm":
            # 服务端要求加密时不允许被降级到明文，否则中间人只要改写握手就能拿明文。
            self.send_json(
                TYPE_HELLO,
                0,
                {"v": PROTOCOL_VERSION, "ok": False, "error": "服务端要求加密连接，请升级手表端应用"},
            )
            LOGGER.warning("握手被拒（%s）：服务端要求加密，客户端请求 %s", self.label, mode)
            return False

        if mode == "plain":
            self.channel = PlainChannel()
            self.send_json(TYPE_HELLO, 0, {"v": PROTOCOL_VERSION, "ok": True, "crypto": "plain"})
            LOGGER.warning("握手成功（%s）：明文模式，链路内容可被中转方读取", self.label)
            return True

        return self._handshake_secure(hello)

    def _handshake_secure(self, hello: dict[str, Any]) -> bool:
        try:
            client_pub = base64.b64decode(str(hello.get("pub") or ""), validate=True)
            client_nonce = base64.b64decode(str(hello.get("nonce") or ""), validate=True)
        except Exception:  # noqa: BLE001
            self.send_json(TYPE_HELLO, 0, {"v": PROTOCOL_VERSION, "ok": False, "error": "握手公钥格式不对"})
            return False
        if len(client_pub) != 32 or len(client_nonce) != 16:
            self.send_json(TYPE_HELLO, 0, {"v": PROTOCOL_VERSION, "ok": False, "error": "握手公钥长度不对"})
            return False

        server_private, server_public = generate_keypair()
        server_nonce = random_public_nonce()
        key = derive_session_key(server_private, client_pub, client_nonce, server_nonce)

        ok = self.send_json(
            TYPE_HELLO,
            0,
            {
                "v": PROTOCOL_VERSION,
                "ok": True,
                "crypto": "gcm",
                "pub": base64.b64encode(server_public).decode("ascii"),
                "nonce": base64.b64encode(server_nonce).decode("ascii"),
            },
        )
        if not ok:
            return False
        # 这一帧之后才切换：上面的 HELLO 应答必须是明文，因为对方此刻还没拿到密钥。
        self.channel = SecureChannel(key, DIR_SERVER_TO_CLIENT, DIR_CLIENT_TO_SERVER)
        LOGGER.info("握手成功（%s）：AES-256-GCM", self.label)
        return True

    def _read_loop(self) -> None:
        while self._alive:
            frame_type, req_id, flags, payload = self.reader.read_frame()
            if self.channel.enabled:
                counter = getattr(self.channel, "_recv_counter", -1)
                head = payload[:8].hex()
                try:
                    payload = self.channel.decrypt(payload)
                except Exception:  # noqa: BLE001 - 解不开说明链路已不可信
                    LOGGER.warning(
                        "解密失败，断开 %s（counter=%d 密文=%d 头=%s）",
                        self.label,
                        counter,
                        len(payload),
                        head,
                    )
                    return
                LOGGER.info(
                    "收← %s type=0x%02x req=%d 明文=%d counter=%d 头=%s",
                    self.label,
                    frame_type,
                    req_id,
                    len(payload),
                    counter,
                    head,
                )
            if frame_type == TYPE_PING:
                self.send(TYPE_PONG, req_id, payload)
                continue
            if frame_type == TYPE_PONG:
                self._last_pong = time.monotonic()
                continue
            if frame_type != TYPE_REQ:
                raise ProtocolError(f"客户端不应发送 {frame_type} 类型")
            self.app.submit(self._handle_request, req_id, payload)

    # ------------------------------------------------------------------ 请求分发

    def _handle_request(self, req_id: int, payload: bytes) -> None:
        try:
            request = unpack_json(payload)
        except ProtocolError as exc:
            self.send_error(req_id, str(exc))
            return
        if not isinstance(request, dict):
            self.send_error(req_id, "请求内容必须是对象")
            return
        op = str(request.get("op") or "")
        handler = {
            "status": self._op_status,
            "messages": self._op_messages,
            "send": self._op_send,
            "conversations": self._op_conversations,
            "image": self._op_image,
            # 批量取图：一次往返把整库缩略图给手表（表情网格快慢的关键，
            # 原因见 _op_sticker_thumbs 的注释）
            "sticker_thumbs": self._op_sticker_thumbs,
            # 手动刷新表情库（手表端「刷」按钮可以调）。平时由推送循环按间隔自动做，
            # 这里是「我刚收藏了一个，想立刻在表上看到」的快速通道。
            "refresh_stickers": self._op_refresh_stickers,
            # 手表打开表情面板时踢一脚的「保鲜」：非阻塞，刷完由推送通知手表
            "sync_stickers": self._op_sync_stickers,
            "watch": self._op_watch,
            "unwatch": self._op_unwatch,
        }.get(op)
        if handler is None:
            self.send_error(req_id, f"不认识的指令：{op}", op)
            return
        try:
            handler(req_id, request)
        except BridgeError as exc:
            self.send_error(req_id, str(exc), op)
        except TimeoutError:
            self.send_error(req_id, "操作超时，请重试", op)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("处理指令 %s 失败", op)
            self.send_error(req_id, str(exc) or exc.__class__.__name__, op)

    def _ensure_ready(self) -> None:
        if self.app.startup_error:
            raise BridgeError(self.app.startup_error)
        # 会话级的故障优先报：浏览器窗口被关掉时 ready 也是 False，先说「正在启动」
        # 会让用户一直等一个永远不会来的东西。
        broken = self.app.bridge.session_error
        if broken:
            raise BridgeError(broken)
        if not self.app.bridge.ready:
            raise BridgeError("抖音会话正在启动，请稍候")

    def _op_status(self, req_id: int, request: dict[str, Any]) -> None:
        self._ensure_ready()
        data = self.app.call_interactive(self.app.bridge.status(), timeout=30)
        data["ok"] = True
        data["op"] = "status"
        self.send_json(TYPE_RES, req_id, data)

    def _op_refresh_stickers(self, req_id: int, request: dict[str, Any]) -> None:
        """在服务里重扫表情面板，然后把最新状态回给手表。

        回的是完整 state 而不是一句 ok：手表端点「刷新」就是想立刻看到表情网格更新，
        让它拿到新库之后直接就能重画，少一次往返。
        注意这里会独占浏览器 15~40 秒（切分类栏 + 下载缩略图），是用户主动发起的，
        让他等；自动刷新那条路（_maybe_refresh_stickers）则只在空闲时做。
        """
        self._ensure_ready()
        refreshed = self.app.call_interactive(self.app.bridge.refresh_stickers(), timeout=300)
        data = self.app.call(self.app.bridge.status(), timeout=30)
        data["ok"] = True
        data["op"] = "status"
        data["refresh"] = {
            "items": refreshed.get("items"),
            "added": refreshed.get("added"),
            "before": refreshed.get("before"),
            "changed": bool(refreshed.get("changed")),
            "per_tab": refreshed.get("per_tab") or {},
        }
        self.send_json(TYPE_RES, req_id, data)

    def _op_messages(self, req_id: int, request: dict[str, Any]) -> None:
        self._ensure_ready()
        name = str(request.get("name") or "").strip()
        if not name:
            raise BridgeError("缺少 name")
        limit = _clamp(request.get("limit"), default=30, low=1, high=100)
        known_rev = str(request.get("rev") or "")
        data = self.app.call_interactive(self.app.bridge.read_chat(name, limit), timeout=60)
        rev = data.get("rev") or ""
        # 显式读一次就把订阅的基准同步上，免得推送线程紧接着又推一份一模一样的内容。
        self._sync_subscription_rev(name, rev)
        if known_rev and rev and known_rev == rev:
            data = {"friend": name, "rev": rev, "unchanged": True, "messages": []}
        data["ok"] = True
        data["op"] = "messages"
        self.send_json(TYPE_RES, req_id, data)

    def _op_send(self, req_id: int, request: dict[str, Any]) -> None:
        self._ensure_ready()
        name = str(request.get("name") or "").strip()
        if not name:
            raise BridgeError("缺少 name")
        raw_sticker = request.get("sticker")
        sticker = str(raw_sticker).strip() if isinstance(raw_sticker, str) and raw_sticker.strip() else None
        text = str(request.get("text") or "")
        result = self.app.call_interactive(self.app.bridge.send_message(name, text, sticker), timeout=180)
        rev = result.get("rev")
        if rev:
            self._sync_subscription_rev(name, str(rev))
        result["ok"] = True
        result["op"] = "send"
        self.send_json(TYPE_RES, req_id, result)

    def _op_conversations(self, req_id: int, request: dict[str, Any]) -> None:
        self._ensure_ready()
        limit = _clamp(request.get("limit"), default=30, low=1, high=100)
        items = self.app.call_interactive(self.app.bridge.read_conversations(limit), timeout=60)
        self.send_json(TYPE_RES, req_id, {"op": "conversations", "ok": True, "items": items})

    def _op_image(self, req_id: int, request: dict[str, Any]) -> None:
        kind = str(request.get("kind") or "")
        if kind == "sticker":
            ref = str(request.get("ref") or "").strip()
            if not ref:
                raise BridgeError("缺少 ref")
            source, key, default_px = "sticker", ref, self.app.sticker_px
        elif kind == "media":
            key = str(request.get("url") or "").strip()
            if not key:
                raise BridgeError("缺少 url")
            source, default_px = "media", self.app.media_px
        else:
            raise BridgeError(f"不认识的图片来源：{kind}")

        px = _clamp(request.get("px"), default=default_px, low=0, high=1024)
        content_type, body = self.app.scaled_image(source, key, px)

        # 先回一帧带 content-type 的应答，图片字节跟在后面的 IMAGE 帧里。
        # 分成两种帧是因为载荷一个是 JSON、一个是裸二进制，混在一起两边都要写特判。
        self.send_json(TYPE_RES, req_id, {"op": "image", "ok": True, "contentType": content_type, "bytes": len(body)})

        offset = 0
        while offset < len(body) or offset == 0:
            chunk = body[offset : offset + MAX_PAYLOAD]
            offset += len(chunk)
            more = FLAG_MORE if offset < len(body) else 0
            if not self.send(TYPE_IMAGE, req_id, chunk, more):
                return
            if not chunk:
                break

    # ------------------------------------------------------------------ 订阅推送

    def _op_sticker_thumbs(self, req_id: int, request: dict[str, Any]) -> None:
        """一次往返把整库表情缩略图交给手表。

        为什么要有这个批量接口：手表侧是低功耗 Wi-Fi，实测 RTT 32~117ms，
        连 16 字节的 PING/PONG 往返都要 280~780ms。原先「一张图一次 op=image」
        就等于「70 次往返」，并发 3 也只是把它压到 ~8 秒 —— 这就是表情网格
        一格一格往外蹦的全部原因。服务端本身处理一张只要 1ms（派生小图命中）。

        载荷格式（大端，紧跟一帧 JSON 应答之后的 IMAGE 帧里）：
            u16 条目数
            每条目：u16 id 长度 | u32 数据长度 | id(utf8) | 图片字节
        全库 70 张实测 643KB，一个帧装得下；真超了就走既有的分片（FLAG_MORE），
        手表端本来就是按分片累加的。
        """
        self._ensure_ready()
        px = _clamp(request.get("px"), default=self.app.sticker_px, low=0, high=1024)
        library = self.app.bridge.library()
        pieces: list[bytes] = []
        count = 0
        for item in library.enabled():
            if not item.thumb:
                continue
            try:
                _, body, _ = self.app.bridge.sticker_thumb(item.id, px)
            except BridgeError:
                # 单张图坏了不该让整批失败：跳过它，手表那边这一格退回文字按钮
                continue
            if not body:
                continue
            encoded = item.id.encode("utf-8")
            pieces.append(struct.pack(">HI", len(encoded), len(body)))
            pieces.append(encoded)
            pieces.append(body)
            count += 1
        blob = struct.pack(">H", count) + b"".join(pieces)

        self.send_json(
            TYPE_RES,
            req_id,
            {"op": "sticker_thumbs", "ok": True, "count": count, "bytes": len(blob), "px": px},
        )
        offset = 0
        while offset < len(blob) or offset == 0:
            chunk = blob[offset : offset + BULK_CHUNK]
            offset += len(chunk)
            more = FLAG_MORE if offset < len(blob) else 0
            if not self.send(TYPE_IMAGE, req_id, chunk, more):
                return
            if not chunk:
                break

    def _op_sync_stickers(self, req_id: int, request: dict[str, Any]) -> None:
        """手表打开表情面板时踢的一脚：排一次后台刷新，立刻答复、不等结果。

        刷新完库文件 mtime 变了，推送循环会自动把新状态推过去 —— 所以这里回
        `started` 就够，手表那边该干嘛干嘛（它拿到推送会自己重画网格）。
        """
        started = self.app.kick_sticker_refresh()
        self.send_json(TYPE_RES, req_id, {"op": "sync_stickers", "ok": True, "started": started})

    def _op_watch(self, req_id: int, request: dict[str, Any]) -> None:
        self._ensure_ready()
        name = str(request.get("name") or "").strip()
        if not name:
            raise BridgeError("缺少 name")
        limit = _clamp(request.get("limit"), default=30, low=1, high=100)
        active = bool(request.get("active", True))
        rev = str(request.get("rev") or "")
        with self._sub_lock:
            self._subscription = {"name": name, "limit": limit, "active": active, "rev": rev}
        self.send_json(TYPE_RES, req_id, {"op": "watch", "ok": True, "friend": name})
        self._start_pusher()
        LOGGER.info("订阅会话「%s」（%s）于 %s", name, "前台" if active else "后台", self.label)

    def _op_unwatch(self, req_id: int, request: dict[str, Any]) -> None:
        with self._sub_lock:
            self._subscription = None
        self.send_json(TYPE_RES, req_id, {"op": "unwatch", "ok": True})

    def _sync_subscription_rev(self, name: str, rev: str) -> None:
        if not rev:
            return
        with self._sub_lock:
            if self._subscription is not None and self._subscription["name"] == name:
                self._subscription["rev"] = rev

    def _current_subscription(self) -> dict[str, Any] | None:
        with self._sub_lock:
            return dict(self._subscription) if self._subscription is not None else None

    def _start_pusher(self) -> None:
        if self._push_thread is not None and self._push_thread.is_alive():
            return
        self._push_thread = threading.Thread(
            target=self._push_loop, name=f"push-{self.label}", daemon=True
        )
        self._push_thread.start()

    def _push_loop(self) -> None:
        """订阅期间反复读当前会话，内容真的变了才推。

        读一次要 0.3~0.6 秒（浏览器里翻 DOM），所以节奏是「读 → 歇 → 读」，
        而不是固定周期 —— 后者在慢的时候会把请求堆起来。
        """
        while self._alive:
            # 表情库在电脑上重扫过（scan_stickers.py 写完 watch_stickers.json），
            # 这里把新库推给手表，表情网格不用重连就刷新。stat 一次的开销可忽略。
            self._maybe_push_status()
            # 顺手看看要不要在服务里自己刷一次（收藏了表情就自动出现在手表上）。
            # 它只是排个队，真正的刷新在线程池里做，不占这个循环。
            self._maybe_refresh_stickers()
            sub = self._current_subscription()
            if sub is None:
                time.sleep(0.5)
                continue
            base = PUSH_ACTIVE_SECONDS if sub["active"] else PUSH_IDLE_SECONDS
            interval = max(base, PUSH_LAZY_SECONDS) if self._unchanged_streak >= PUSH_LAZY_AFTER else base
            # 有人正在等浏览器（用户点了、在发消息、在翻表情）就让这一轮让路。
            # 代价是消息最多晚 interval 秒才推过去，换来的是用户的操作不用排队 ——
            # 对着一块表，后者才是能被感觉到的那个。
            if self.app.interactive_busy():
                time.sleep(0.15)
                continue
            try:
                data = self.app.call(self.app.bridge.read_chat(sub["name"], sub["limit"]), timeout=60)
            except BridgeError as exc:
                # 会话切走了、页面在重载 —— 这些是暂时的，等下一轮再试。
                LOGGER.debug("推送读会话失败：%s", exc)
                time.sleep(interval)
                continue
            except Exception:  # noqa: BLE001
                LOGGER.debug("推送读会话异常", exc_info=True)
                time.sleep(interval)
                continue

            if not self._alive:
                return
            rev = str(data.get("rev") or "")
            current = self._current_subscription()
            if current is None or current["name"] != sub["name"]:
                continue
            if rev and rev != current["rev"]:
                self._unchanged_streak = 0
                if not current["rev"]:
                    # 首次读到内容时只记基准、不推送。订阅时客户端手上还没有 rev
                    # （它通常刚加载完一屏），这里要是推出去，用户会看到列表被内容
                    # 完全一样的一份「更新」重新刷一遍。
                    self._store_rev(rev)
                else:
                    self._store_rev(rev)
                    self.send_json(
                        TYPE_RES,
                        0,
                        {
                            "op": "messages",
                            "ok": True,
                            "friend": data.get("friend") or sub["name"],
                            "rev": rev,
                            "messages": data.get("messages") or [],
                        },
                        FLAG_PUSH,
                    )
            else:
                # 没变化就攒着。攒够了轮询节奏会放宽，用户的操作就少排队
                self._unchanged_streak += 1
            time.sleep(interval)

    def _maybe_refresh_stickers(self) -> None:
        """够久没刷过表情库就排一次刷新（不阻塞推送循环）。

        为什么放在推送循环里：这个循环只在「有客户端连着」时跑，正好是
        「用户在用表」的时段 —— 收藏完表情过一会儿看表，就已经在了。
        没人在用的时候不需要刷，也就不会白白打断（浏览器被独占十几秒）。
        """
        sub = self._current_subscription()
        self.app.maybe_refresh_stickers(sub_active=bool(sub and sub.get("active")))

    def _maybe_push_status(self) -> None:
        """表情库文件变了就推一份完整状态给手表。

        首轮只记基准：手表连上后自己会拉一次状态，再推就是重复内容。
        状态不碰浏览器（好友/快捷表情来自配置，表情库读本地文件），
        所以这里不走 call_interactive，不会跟用户的操作抢浏览器。
        """
        try:
            mtime = self.app.bridge.library_mtime()
        except Exception:  # noqa: BLE001 - 会话还没起完时library路径可能没就绪，下轮再说
            return
        if self._pushed_library_mtime is None:
            self._pushed_library_mtime = mtime
            return
        if mtime == self._pushed_library_mtime:
            return
        self._pushed_library_mtime = mtime
        try:
            data = self.app.call(self.app.bridge.status(), timeout=15)
        except Exception:  # noqa: BLE001
            LOGGER.debug("表情库更新后推状态失败", exc_info=True)
            return
        data["ok"] = True
        data["op"] = "status"
        self.send_json(TYPE_RES, 0, data, FLAG_PUSH)
        LOGGER.info("表情库有更新（mtime=%s），已向 %s 推送新状态", mtime, self.label)

    def _store_rev(self, rev: str) -> None:
        with self._sub_lock:
            if self._subscription is not None:
                self._subscription["rev"] = rev

    # ------------------------------------------------------------------ 心跳

    def _start_ping(self) -> None:
        self._ping_thread = threading.Thread(
            target=self._ping_loop, name=f"ping-{self.label}", daemon=True
        )
        self._ping_thread.start()

    def _ping_loop(self) -> None:
        """空闲时主动探活。

        中间经过穿透中转时，链路可能已经断了而本机 TCP 还以为连着 —— 手表那边
        表现为「一直停在旧消息、点发送没反应」。靠心跳把这种连接尽早判死。
        """
        while self._alive:
            time.sleep(PING_INTERVAL_SECONDS)
            if not self._alive:
                return
            if time.monotonic() - self._last_pong > PING_TIMEOUT_SECONDS:
                LOGGER.info("心跳超时，断开 %s", self.label)
                self.close()
                return
            if not self.send(TYPE_PING, 0):
                return


# ---------------------------------------------------------------- 基础设施


def _clamp(raw: Any, *, default: int, low: int, high: int) -> int:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def _make_server_class(family: int):
    """按地址族建一个 TCPServer 子类。

    监听 `::` 时关掉 IPV6_V6ONLY，这样 IPv4 的手表也能连上来 —— 家里宽带和
    蜂窝网络不一定都有 IPv6，双栈监听让两种客户端走同一个端口。
    """

    class _Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True
        daemon_threads = True
        request_queue_size = 64
        address_family = family

        def server_bind(self) -> None:
            if family == socket.AF_INET6:
                try:
                    self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                except OSError:
                    pass
            socketserver.TCPServer.server_bind(self)

    return _Server


def _make_handler(app: TcpBridgeServer):
    class _Handler(socketserver.BaseRequestHandler):
        def handle(self) -> None:
            try:
                # 消息都是几十到几百字节的小帧，攒包只会让手感变钝。
                self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            ClientSession(app, self.request, self.client_address).run()

        def finish(self) -> None:
            # socket 由 ClientSession.close() 负责关闭（它还要先标记存活状态、
            # 从在线表里摘掉自己），父类那份关闭会造成重复 close。
            pass

    return _Handler


__all__ = ["ClientSession", "TcpBridgeServer"]
