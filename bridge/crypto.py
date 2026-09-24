"""应用层加密：X25519 密钥交换 + HKDF-SHA256 + AES-256-GCM。

为什么用应用层加密而不是 TLS：

- TLS 要证书，证书要 SAN。手表可能通过局域网 IP、也可能通过穿透服务商给的域名
  连过来，SAN 在签发时根本无法预知；写错就要让用户在手表上重配。X25519 没有这个
  问题 —— 不管对端用 IP 还是域名，密钥交换都照常成立。
- HarmonyOS 穿戴端的 `cryptoFramework` 对 X25519 / AES-GCM 是一等公民，
  比 `TLSSocket` 传自定义 `ca` 的路径更不容易踩到平台差异。
- 不引入证书生命周期（签发、过期、轮换），也不依赖系统根证书。

安全性质：这是一份**个人设备间**的通道加密，认证靠预共享的 token；token 不参与
密钥派生，只在握手阶段做一次常数时间比对。每次连接都换一对临时密钥，所以录下的
流量事后拿到 token 也解不开（前向保密）。

    C → S   HELLO  {v, token, crypto:"gcm", pub, nonce}
    S → C   HELLO  {v, ok:true, crypto:"gcm", pub, nonce}
    ---- 此后所有帧的载荷都是 密文 ‖ tag(16)，nonce 不上链路 ----
    C → S   HELLO  {v, crypto:"plain"}

nonce 不进密文，两端各自按「方向前缀 + 计数器」推导 —— 省下每帧 12 字节。代价是
计数器必须严格同步，所以：

**调用方必须保证同一时刻只有一个线程在加密、也只有一个线程在解密。**
`SecureChannel` 的计数器是「读—用—加一」，两个线程同时进来会拿到同一个 nonce。
GCM 下同 key 同 nonce 的后果是双重的：两段明文的异或直接泄露，而且对方从那一帧
起接收计数器错位、后面每一帧都解不开 —— 表现为「连接莫名其妙断了」。
服务端见 tcp_server.py::ClientSession.send（把加密也包进了发送锁），
手表端见 entry/src/main/ets/service/BridgeLink.ets::sendFrame（promise 链）。

`plain` 是留的逃生舱：日常不用，但一旦穿戴端在某个系统版本上跑不通加密，
不至于整条链路都连不上。
"""

from __future__ import annotations

import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

# 防止把密钥派生的用途搞混：换个 info 就是另一个独立的密钥空间。
HKDF_INFO = b"douyin-watch-bridge/v1"

# nonce = 4 字节方向 + 8 字节计数器。方向前缀让两个方向共用一个密钥也不会撞 nonce，
# 这正是 GCM 最容易踩的坑（同 key + 同 nonce 会直接泄露明文异或）。
DIR_SERVER_TO_CLIENT = 1
DIR_CLIENT_TO_SERVER = 2

_COUNTER_BYTES = 8
# GCM 认证标签长度。密文最短就是这么长（明文为空时），用它当地板值 ——
# 拿 nonce 的长度当门槛是错的，会让 12~15 字节的坏帧漏进 aead.decrypt 里去抛 InvalidTag。
_TAG_SIZE = 16


def random_public_nonce() -> bytes:
    """握手时带上的随机数，参与盐的构造。"""
    return os.urandom(16)


def generate_keypair() -> tuple[bytes, bytes]:
    """生成一对临时 X25519 密钥，返回 (私钥 32 字节, 公钥 32 字节)。"""
    private = X25519PrivateKey.generate()
    return (
        private.private_bytes_raw(),
        private.public_key().public_bytes_raw(),
    )


def derive_session_key(
    private_bytes: bytes,
    peer_public_bytes: bytes,
    client_nonce: bytes,
    server_nonce: bytes,
) -> bytes:
    """双方各算一次，得到同一个 32 字节会话密钥。

    盐同时包含双方的随机数 —— 只绑一方的随机数的话，攻击者可以固定自己那半
    来压缩搜索空间。
    """
    if len(private_bytes) != 32:
        raise ValueError("X25519 私钥必须是 32 字节")
    if len(peer_public_bytes) != 32:
        raise ValueError("X25519 公钥必须是 32 字节")
    shared = X25519PrivateKey.from_private_bytes(private_bytes).exchange(
        X25519PublicKey.from_public_bytes(peer_public_bytes)
    )
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=client_nonce + server_nonce,
        info=HKDF_INFO,
    ).derive(shared)


class PlainChannel:
    """不加密。接口与 SecureChannel 完全一致，调用方不用分支。"""

    enabled = False

    def encrypt(self, plaintext: bytes) -> bytes:
        return plaintext

    def decrypt(self, blob: bytes) -> bytes:
        return blob


class SecureChannel:
    """AES-256-GCM 通道。"""

    enabled = True

    def __init__(self, key: bytes, send_dir: int, recv_dir: int) -> None:
        if len(key) != 32:
            raise ValueError("AES-256 密钥必须是 32 字节")
        self._aead = AESGCM(key)
        self._send_prefix = send_dir.to_bytes(4, "big")
        self._recv_prefix = recv_dir.to_bytes(4, "big")
        self._send_counter = 0
        self._recv_counter = 0

    def encrypt(self, plaintext: bytes) -> bytes:
        """加密一帧。**调用方必须串行调用**，见模块头部的说明。"""
        nonce = self._send_prefix + self._send_counter.to_bytes(_COUNTER_BYTES, "big")
        self._send_counter += 1
        return self._aead.encrypt(nonce, plaintext, None)

    def decrypt(self, blob: bytes) -> bytes:
        """解密一帧。**调用方必须串行调用**，见模块头部的说明。"""
        if len(blob) < _TAG_SIZE:
            raise ValueError("密文过短，连认证标签都不够")
        nonce = self._recv_prefix + self._recv_counter.to_bytes(_COUNTER_BYTES, "big")
        plaintext = self._aead.decrypt(nonce, blob, None)
        # 只有解成功才推进计数器：解失败说明这一帧不可信，计数器继续往前推反而
        # 会把后续正常的帧全部带偏。
        self._recv_counter += 1
        return plaintext


__all__ = [
    "DIR_CLIENT_TO_SERVER",
    "DIR_SERVER_TO_CLIENT",
    "InvalidTag",
    "PlainChannel",
    "SecureChannel",
    "derive_session_key",
    "generate_keypair",
    "random_public_nonce",
]
