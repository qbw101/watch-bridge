"""桥接协议的参考实现 + 端到端冒烟测试。

两个用途：

1. **协议参考**。手表端（ArkTS）要实现的握手、分帧、加解密、请求/应答配对，
   这里有一份用标准库写出来的对照实现。两边行为对不上时，先跑这个脚本 ——
   它能确定问题在服务端还是手表端。
2. **端到端验证**。手表没在手边时，这是唯一能证明「读会话 / 发消息 / 取图片」
   整条链路真的通的方式。

用法：
    python scripts/tcp_smoke.py                          # 全量冒烟（只读，不发消息）
    python scripts/tcp_smoke.py --friend 小陈 --sticker 续火花
    python scripts/tcp_smoke.py --watch 30               # 订阅 30 秒，看推送
    python scripts/tcp_smoke.py --save-thumb out.png     # 存一张表情缩略图出来看
"""

from __future__ import annotations

import argparse
import base64
import io
import queue
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bridge.crypto import (  # noqa: E402
    DIR_CLIENT_TO_SERVER,
    DIR_SERVER_TO_CLIENT,
    PlainChannel,
    SecureChannel,
    derive_session_key,
    generate_keypair,
    random_public_nonce,
)
from bridge.protocol import (  # noqa: E402
    FLAG_ERROR,
    FLAG_MORE,
    FLAG_PUSH,
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


class BridgeTcpClient:
    """与服务端配对的最小客户端。"""

    def __init__(self, host: str, port: int, token: str, *, crypto: str = "gcm", timeout: float = 30.0) -> None:
        self.host = host
        self.port = port
        self.token = token
        self.crypto = crypto
        self.timeout = timeout
        self.sock: socket.socket | None = None
        self.channel: PlainChannel | SecureChannel = PlainChannel()
        self.reader: FrameReader | None = None
        self._lock = threading.Lock()
        self._pending: dict[int, dict[str, Any]] = {}
        self._next_id = 1
        self._reader_thread: threading.Thread | None = None
        self._alive = False
        self.pushes: queue.Queue[dict[str, Any]] = queue.Queue()
        self.server_info: dict[str, Any] = {}

    # ------------------------------------------------------------------ 连接

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = sock
        self.reader = FrameReader(sock.recv)
        self._handshake()
        sock.settimeout(None)
        self._alive = True
        self._reader_thread = threading.Thread(target=self._read_loop, name="smoke-reader", daemon=True)
        self._reader_thread.start()

    def close(self) -> None:
        self._alive = False
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _handshake(self) -> None:
        assert self.sock is not None and self.reader is not None
        hello: dict[str, Any] = {"v": PROTOCOL_VERSION, "token": self.token, "crypto": self.crypto}
        private = public = None
        client_nonce = b""
        if self.crypto == "gcm":
            private, public = generate_keypair()
            client_nonce = random_public_nonce()
            hello["pub"] = base64.b64encode(public).decode("ascii")
            hello["nonce"] = base64.b64encode(client_nonce).decode("ascii")

        self.sock.sendall(pack_frame(TYPE_HELLO, 0, encode_json(hello)))
        frame_type, _, _, payload = self.reader.read_frame()
        if frame_type != TYPE_HELLO:
            raise ProtocolError(f"期望握手应答，收到帧类型 {frame_type}")
        reply = unpack_json(payload)
        if not isinstance(reply, dict) or not reply.get("ok"):
            raise ProtocolError(str(reply.get("error") if isinstance(reply, dict) else "握手被拒"))
        if int(reply.get("v") or 0) != PROTOCOL_VERSION:
            raise ProtocolError("协议版本不一致")

        mode = str(reply.get("crypto") or "plain")
        if mode == "plain":
            self.channel = PlainChannel()
            self.server_info = reply
            return
        if mode != "gcm":
            raise ProtocolError(f"服务端选的加密方式不认识：{mode}")
        server_pub = base64.b64decode(str(reply["pub"]))
        server_nonce = base64.b64decode(str(reply["nonce"]))
        assert private is not None
        key = derive_session_key(private, server_pub, client_nonce, server_nonce)
        self.channel = SecureChannel(key, DIR_CLIENT_TO_SERVER, DIR_SERVER_TO_CLIENT)
        self.server_info = reply

    # ------------------------------------------------------------------ 收发

    def _send(self, frame_type: int, req_id: int, payload: bytes = b"", flags: int = 0) -> None:
        if self.sock is None:
            raise ConnectionError("尚未连接")
        self.sock.sendall(pack_frame(frame_type, req_id, self.channel.encrypt(payload), flags))

    def _read_loop(self) -> None:
        assert self.reader is not None
        while self._alive:
            try:
                frame_type, req_id, flags, payload = self.reader.read_frame()
            except (OSError, ConnectionError, ProtocolError):
                break
            if self.channel.enabled:
                try:
                    payload = self.channel.decrypt(payload)
                except Exception:  # noqa: BLE001
                    break
            if frame_type == TYPE_PING:
                self._send(TYPE_PONG, req_id, payload)
                continue
            if frame_type == TYPE_PONG:
                continue
            if frame_type == TYPE_IMAGE:
                entry = self._pending.get(req_id)
                if entry is not None:
                    entry["image"].extend(payload)
                    if not flags & FLAG_MORE:
                        entry["image_done"] = True
                        entry["event"].set()
                continue
            if frame_type == TYPE_RES:
                data = unpack_json(payload)
                if flags & FLAG_PUSH or req_id == 0:
                    self.pushes.put(data)
                    continue
                entry = self._pending.get(req_id)
                if entry is not None:
                    entry["res"] = data
                    entry["error"] = bool(flags & FLAG_ERROR)
                    # 图片请求要等 IMAGE 帧收全才算完，普通请求这一帧就够。
                    # 判据是**调用方声明的 need_image**，不是应答里的 op 名：
                    # 认 op 名的话，每加一个带字节的接口（比如 sticker_thumbs）
                    # 就得回来补一次名单，漏了的表现是「字节数报了几百 KB、实收 0」
                    # —— 一个看起来像服务端 bug 的测试假阳性。
                    if not (entry["need_image"] and not entry["error"]):
                        entry["event"].set()
                continue
        self._alive = False
        # 唤醒所有还在等的人，否则调用方会一直等到超时
        for entry in list(self._pending.values()):
            entry["event"].set()

    def request(self, op: str, *, need_image: bool = False, timeout: float | None = None, **fields: Any) -> dict[str, Any]:
        request_id = self._reserve_id()
        entry: dict[str, Any] = {"event": threading.Event(), "res": None, "error": False, "image": bytearray(), "image_done": False, "need_image": need_image}
        with self._lock:
            self._pending[request_id] = entry
        try:
            payload = {"op": op, **fields}
            self._send(TYPE_REQ, request_id, encode_json(payload))
            if not entry["event"].wait(timeout or self.timeout):
                raise TimeoutError(f"{op} 超时{'（图片未收全）' if need_image else ''}")
            if entry["res"] is None:
                raise ConnectionError("连接已断开")
            if entry["error"]:
                raise RuntimeError(str(entry["res"].get("error") or "未知错误"))
            # 图片字节附在应答上一起返回。键名带下划线是提醒这是本地挂上去的，
            # 不是服务端 JSON 里的字段。
            entry["res"]["_image"] = bytes(entry["image"])
            return entry["res"]
        finally:
            with self._lock:
                self._pending.pop(request_id, None)

    def _reserve_id(self) -> int:
        with self._lock:
            value = self._next_id
            self._next_id = 1 if value >= 0xFFFF else value + 1
            return value

    def image_bytes(self, last_result: dict[str, Any]) -> bytes:
        return last_result.get("_image") or b""


# ---------------------------------------------------------------- 冒烟流程


def _banner(text: str) -> None:
    print(f"\n=== {text} ===", flush=True)


def _frame_count(raw: bytes) -> int:
    """图片有几帧。> 1 说明是动图，降采样后只剩首帧，这点必须让用户知道。"""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as image:
            return int(getattr(image, "n_frames", 1))
    except Exception:  # noqa: BLE001
        return 1


def _timed(fn, *args, **kwargs):
    started = time.perf_counter()
    value = fn(*args, **kwargs)
    return value, (time.perf_counter() - started) * 1000


def run_smoke(client: BridgeTcpClient, args: argparse.Namespace) -> int:
    problems: list[str] = []

    _banner("握手")
    print(f"加密方式：{client.server_info.get('crypto')}", flush=True)

    _banner("状态")
    status, ms = _timed(client.request, "status")
    friends = status.get("friends") or []
    library = status.get("watch_stickers") or []
    print(f"ready={status.get('ready')} 好友 {len(friends)} 个，表情库 {len(library)} 项（{ms:.0f}ms）", flush=True)
    print(f"好友：{friends[:8]}", flush=True)
    if not friends:
        problems.append("没有好友：config.json 里的 friends 是空的")

    friend = args.friend or (friends[0] if friends else "")
    if not friend:
        return _report(problems)

    _banner(f"读会话「{friend}」")
    messages, ms = _timed(client.request, "messages", name=friend, limit=30)
    items = messages.get("messages") or []
    media_count = sum(1 for item in items if item.get("media"))
    print(f"{len(items)} 条消息，其中 {media_count} 条带图，rev={messages.get('rev')}（{ms:.0f}ms）", flush=True)
    for item in items[-4:]:
        text = (item.get("text") or "")[:18]
        print(f"   [{item.get('side')}/{item.get('type')}] {text}", flush=True)

    _banner("增量读取（带 rev 应回 unchanged）")
    again, ms = _timed(client.request, "messages", name=friend, limit=30, rev=messages.get("rev"))
    print(f"unchanged={again.get('unchanged')} （{ms:.0f}ms）", flush=True)
    if again.get("unchanged") is not True:
        problems.append("带 rev 重复读取没有返回 unchanged —— 内容指纹失效会让手表反复重绘")

    if library:
        # 抽样必须铺满整个库，不能取前 N 项：库是按扫描顺序排的，前面几十项全是
        # 几 KB 的静态 PNG，最后几十项才是几百 KB 的（动图）WebP。只测开头会得出
        # 「总共几百 KB」的错误结论，而真实体积差一个数量级。
        step = max(1, len(library) // args.sample)
        sample = library[::step][: args.sample]
        old_total = status.get("sticker_bytes") or sum(item.get("thumbBytes") or 0 for item in library)
        _banner(f"表情缩略图（库共 {len(library)} 项，抽样 {len(sample)} 项）")
        original_total = 0
        scaled_total = 0
        max_raw = 0
        animated = 0
        failures = 0
        with_thumb = 0
        for item in sample:
            if not item.get("has_thumb"):
                continue
            try:
                original = client.request("image", kind="sticker", ref=item["id"], px=0, need_image=True)
                raw = client.image_bytes(original)
                scaled = client.request("image", kind="sticker", ref=item["id"], px=args.sticker_px, need_image=True)
                body = client.image_bytes(scaled)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"   取图失败 {item.get('label')}: {exc}", flush=True)
                continue
            with_thumb += 1
            original_total += len(raw)
            scaled_total += len(body)
            max_raw = max(max_raw, len(raw))
            if _frame_count(raw) > 1:
                animated += 1
            if args.save_thumb and with_thumb == 1:
                Path(args.save_thumb).write_bytes(body)
                print(f"   首张已存到 {args.save_thumb}", flush=True)

        if with_thumb > 0:
            avg_raw = original_total / with_thumb
            avg_scaled = scaled_total / with_thumb
            print(
                f"原图平均 {avg_raw / 1024:.0f}KB（抽样最大 {max_raw / 1024:.0f}KB）"
                f" → 缩到 {args.sticker_px}px 后平均 {avg_scaled / 1024:.0f}KB"
                f"，压掉 {100 - int(avg_scaled * 100 / max(1, avg_raw))}%",
                flush=True,
            )
            print(
                f"全量推一遍：{args.sticker_px}px 缩略图约 {avg_scaled * len(library) / 1048576:.2f}MB"
                f"（原图 {old_total / 1048576:.2f}MB）",
                flush=True,
            )
        if animated:
            print(
                f"其中 {animated}/{with_thumb} 张原图是多帧动图 —— 降采样后只剩首帧。"
                "PixelMap 不支持动图播放，要动效得自己按帧轮播。",
                flush=True,
            )
        if failures:
            problems.append(f"{failures} 张表情取图失败")

    media_item = next((item for item in reversed(items) if item.get("media")), None)
    if media_item is not None:
        _banner("聊天图片")
        try:
            result = client.request("image", kind="media", url=media_item["media"], px=args.media_px, need_image=True)
            body = client.image_bytes(result)
            print(f"{len(body)} 字节（{result.get('contentType')}）", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"失败：{exc}", flush=True)

    # 订阅放在发送之前：这样发送引起的内容变化会走推送回来，一次跑完就能证明
    # 「服务端主动推送」真的工作 —— 否则光是订阅、没人发消息，看到的永远是 0 次推送。
    if args.watch > 0:
        _banner(f"订阅推送 {args.watch} 秒")
        client.request("watch", name=friend, limit=30, active=True)
        # 先把基准对齐：服务端只在 rev 变化时才推，不对齐的话它会先把当前内容推一遍。
        baseline = client.request("messages", name=friend, limit=30)
        print(f"基准 rev={baseline.get('rev')}", flush=True)

    if args.send_text or args.sticker:
        _banner("发送")
        if args.watch > 0:
            # 订阅着的时候，发送刻意走**另一条连接**：同一条连接上发消息时，服务端
            # 会把订阅基准同步过去（发送应答里已经带了最新列表），于是推送不会触发 ——
            # 那样就测不出「别处来的新消息」这个真正的推送场景。
            peer = BridgeTcpClient(args.host, args.port, client.token, crypto=args.crypto, timeout=200)
            peer.connect()
            try:
                result = peer.request(
                    "send", name=friend, text=args.send_text or "", sticker=args.sticker or "", timeout=200
                )
            finally:
                peer.close()
            print("（经第二条连接发送）", flush=True)
        else:
            result = client.request(
                "send", name=friend, text=args.send_text or "", sticker=args.sticker or "", timeout=200
            )
        print(f"ok={result.get('ok')} 回读 {len(result.get('messages') or [])} 条，rev={result.get('rev')}", flush=True)

    if args.watch > 0:
        deadline = time.time() + args.watch
        seen = 0
        while time.time() < deadline:
            try:
                push = client.pushes.get(timeout=1.0)
            except queue.Empty:
                continue
            seen += 1
            print(f"   收到推送 rev={push.get('rev')} 消息 {len(push.get('messages') or [])} 条", flush=True)
        print(f"共收到 {seen} 次推送", flush=True)
        client.request("unwatch")

    return _report(problems)


def _report(problems: list[str]) -> int:
    _banner("结论")
    if problems:
        for item in problems:
            print(f"  问题：{item}", flush=True)
        return 1
    print("   全部通过", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="桥接协议参考客户端 / 冒烟测试")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--token", default=None, help="不填则读 artifacts/watch_token.txt")
    parser.add_argument("--crypto", choices=("gcm", "plain"), default="gcm")
    parser.add_argument("--friend", default=None)
    parser.add_argument("--sticker", default=None, help="要发送的表情名字（会真的发出去）")
    parser.add_argument("--send-text", default=None, help="要发送的文字（会真的发出去）")
    parser.add_argument("--sticker-id", default=None, help="用来测缩略图的 id，默认取库里第一项")
    parser.add_argument("--sticker-px", type=int, default=72)
    parser.add_argument("--media-px", type=int, default=128)
    parser.add_argument("--sample", type=int, default=12, help="表情缩略图的抽样张数（均匀铺满整个库）")
    parser.add_argument("--save-thumb", default=None, help="把 72px 缩略图存到这个路径以便肉眼确认")
    parser.add_argument("--watch", type=int, default=0, help="订阅推送的秒数")
    args = parser.parse_args()

    token = args.token
    if not token:
        token_file = PROJECT_ROOT / "artifacts/watch_token.txt"
        if token_file.is_file():
            token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        print("没有令牌：用 --token 指定，或先启动一次服务生成 artifacts/watch_token.txt", flush=True)
        return 2

    client = BridgeTcpClient(args.host, args.port, token, crypto=args.crypto, timeout=60)
    try:
        client.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"连接失败：{exc}", flush=True)
        return 2
    try:
        return run_smoke(client, args)
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
