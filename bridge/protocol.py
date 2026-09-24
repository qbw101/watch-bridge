"""手表桥接的二进制帧协议（非 HTTP）。

为什么不用 HTTP：

- 穿透服务商是按「应用协议」判定的。走 HTTP(S) 隧道要备案域名，走 TCP 隧道
  但应用协议是 HTTP 又只能用非内地节点（境外节点速度差）。这是一个自研的二进制
  帧协议，服务商侧看不出任何 HTTP 特征，因此可以用内地节点的 TCP 隧道。
- HTTP 的短连接 + 2 秒轮询在手表上是纯浪费：每条请求都要重新握手、带一整套
  请求头，而大部分轮询的结果是「什么都没变」。
- 一条长连接天然支持服务端主动推送，这才是把轮询换掉的前提。

帧格式（帧头固定 8 字节，大端）：

    偏移  长度  字段      含义
    0     4     长度      载荷字节数（不含帧头本身）
    4     1     类型      见下面的 TYPE_*
    5     2     请求 id   0 表示服务端主动推送、无对应请求
    7     1     标志      见下面的 FLAG_*

载荷在加密通道上还会再套一层 AEAD（见 bridge/crypto.py），帧头保持明文 ——
它只泄露长度，换来的是分帧可以完全不依赖加密层状态。
"""

from __future__ import annotations

import json
import struct
from typing import Any

# ---------------------------------------------------------------- 帧头

HEADER = struct.Struct(">IBHB")
HEADER_SIZE = HEADER.size  # 8

# ---------------------------------------------------------------- 类型

TYPE_HELLO = 0x01  # 握手：客户端带 token，服务端回 ok
TYPE_REQ = 0x02  # 客户端 → 服务端
TYPE_RES = 0x03  # 服务端 → 客户端（应答或主动推送）
TYPE_IMAGE = 0x04  # 服务端 → 客户端：图片二进制
TYPE_PING = 0x05  # 任一方都可发
TYPE_PONG = 0x06

TYPE_NAMES = {
    TYPE_HELLO: "hello",
    TYPE_REQ: "req",
    TYPE_RES: "res",
    TYPE_IMAGE: "image",
    TYPE_PING: "ping",
    TYPE_PONG: "pong",
}

# ---------------------------------------------------------------- 标志

FLAG_MORE = 0x01  # IMAGE：后面还有属于同一张图的帧
FLAG_ERROR = 0x02  # RES：载荷里是错误信息，不是数据
FLAG_PUSH = 0x04  # RES：服务端主动推送，不是对某个请求的应答

# 协议版本。握手双方不一致时直接拒绝，避免新旧实现按不同的约定解析同一串字节。
PROTOCOL_VERSION = 1

# 单帧载荷上限。表情原图实测 7-27KB，放大的聊天图经过服务端降采样后也只有几十 KB；
# 给到 4MB 是留足余量，同时防止对端报一个离谱的长度把内存吃光。
MAX_PAYLOAD = 4 * 1024 * 1024

# 握手必须在这么久内完成，否则断开 —— 否则任何人都能靠建连不发言占满线程。
HELLO_TIMEOUT_SECONDS = 10.0


class ProtocolError(RuntimeError):
    """协议层面的错误：帧头非法、载荷超限、对端发了不该发的类型。"""


# ---------------------------------------------------------------- 打包


def pack_frame(frame_type: int, req_id: int, payload: bytes = b"", flags: int = 0) -> bytes:
    """把一帧拼成字节串。载荷超限时当场抛错，别指望发送端会检查。"""
    if len(payload) > MAX_PAYLOAD:
        raise ProtocolError(f"载荷过大：{len(payload)} 字节（上限 {MAX_PAYLOAD}）")
    if not 0 <= req_id <= 0xFFFF:
        raise ProtocolError(f"请求 id 超出范围：{req_id}")
    return HEADER.pack(len(payload), frame_type, req_id, flags) + payload


def encode_json(data: Any) -> bytes:
    """统一的 JSON 载荷编码：UTF-8、不转义中文、不留空格。

    加密层是在载荷这一级做加解密的，所以调用方要的是「编码后的字节」而不是
    整帧。单独暴露出来，免得每处都手写一遍 dumps 参数。
    """
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def pack_json(frame_type: int, req_id: int, data: Any, flags: int = 0) -> bytes:
    """载荷是 JSON 时打包成一整帧（不加密的场合用）。"""
    return pack_frame(frame_type, req_id, encode_json(data), flags)


def unpack_json(payload: bytes) -> Any:
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("载荷不是合法 JSON") from exc


def parse_header(raw: bytes) -> tuple[int, int, int, int]:
    """解析 8 字节帧头，返回 (长度, 类型, 请求 id, 标志)。"""
    if len(raw) != HEADER_SIZE:
        raise ProtocolError(f"帧头长度应为 {HEADER_SIZE}，实际 {len(raw)}")
    length, frame_type, req_id, flags = HEADER.unpack(raw)
    if length > MAX_PAYLOAD:
        raise ProtocolError(f"对端申报的载荷过大：{length}")
    if frame_type not in TYPE_NAMES:
        raise ProtocolError(f"未知帧类型：0x{frame_type:02x}")
    return length, frame_type, req_id, flags


# ---------------------------------------------------------------- 解包
#
# TCP 是字节流：一次 recv 可能拿到半帧，也可能一次拿到三帧半。所有读取都必须
# 经过这里，绝不能假设「一次 recv = 一帧」—— 那是这类协议最常见的 bug 来源。


def read_exactly(recv, size: int) -> bytes:
    """从 recv 回调里精确读满 size 字节；对端提前关闭时抛 ConnectionError。"""
    if size == 0:
        return b""
    chunks: list[bytes] = []
    got = 0
    while got < size:
        chunk = recv(size - got)
        if not chunk:
            raise ConnectionError("连接已被对端关闭")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


class FrameReader:
    """按帧读取的游标。持有 recv 回调，屏蔽掉半包 / 粘包的细节。"""

    def __init__(self, recv) -> None:
        self._recv = recv

    def read_frame(self) -> tuple[int, int, int, bytes]:
        """读出一整帧，返回 (类型, 请求 id, 标志, 载荷)。"""
        length, frame_type, req_id, flags = parse_header(read_exactly(self._recv, HEADER_SIZE))
        payload = read_exactly(self._recv, length) if length else b""
        return frame_type, req_id, flags, payload


__all__ = [
    "FLAG_ERROR",
    "FLAG_MORE",
    "FLAG_PUSH",
    "FrameReader",
    "HEADER_SIZE",
    "HELLO_TIMEOUT_SECONDS",
    "MAX_PAYLOAD",
    "PROTOCOL_VERSION",
    "ProtocolError",
    "TYPE_HELLO",
    "TYPE_IMAGE",
    "TYPE_NAMES",
    "TYPE_PING",
    "TYPE_PONG",
    "TYPE_REQ",
    "TYPE_RES",
    "encode_json",
    "pack_frame",
    "pack_json",
    "read_exactly",
    "unpack_json",
]
