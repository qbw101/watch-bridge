"""验证 SecureChannel 的一条硬性使用约定：**同一时刻只能有一个线程在加密**。

为什么值得单独写一个脚本：`SecureChannel.encrypt` 里的发送计数器是「读—用—加一」，
nonce 完全由它推导。两个线程同时进来就会拿到同一个 nonce，而 GCM 下**同 key 同
nonce** 的后果是双重的：

    1. 两段明文的异或可以直接算出来 —— 加密本身被击穿；
    2. 对端的接收计数器从那一帧起错位，之后每一帧都解不开。

第 2 条正是「连接莫名其妙就断了」这一类现场的真凶，而且它不会在日志里留下任何
「解密失败」的痕迹 —— 对端只是单方面把连接关了。

检测手法很直接：**同一段明文、同一个 key，nonce 不重复时密文必然不重复。** 于是
把同一段明文从多个线程并发加密，数一数密文的重复个数，就等于撞 nonce 的次数。

服务端（bridge/tcp_server.py::ClientSession.send）和手表端
（entry/src/main/ets/service/BridgeLink.ets::sendFrame）都把加密包进了各自的发送锁，
两边都必须保持这个形状。

用法：
    python scripts/crypto_concurrency_check.py
    python scripts/crypto_concurrency_check.py --threads 16 --frames 500
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import threading
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bridge.crypto import DIR_CLIENT_TO_SERVER, DIR_SERVER_TO_CLIENT, SecureChannel  # noqa: E402

KEY = b"\x07" * 32
PLAINTEXT = b"same-plaintext-for-every-thread"


class GuardedChannel:
    """给 SecureChannel 套一层可选的调用方锁。

    `lock=None` 就是「调用方忘了加锁」的样子 —— 也就是修复前的服务端。
    """

    def __init__(self, lock: threading.Lock | None) -> None:
        self._channel = SecureChannel(KEY, DIR_SERVER_TO_CLIENT, DIR_CLIENT_TO_SERVER)
        self._lock = lock

    def encrypt(self, data: bytes) -> bytes:
        if self._lock is None:
            return self._channel.encrypt(data)
        with self._lock:
            return self._channel.encrypt(data)


def count_collisions(guarded: GuardedChannel, threads: int, frames: int) -> int:
    digests: list[str] = []
    lock = threading.Lock()
    start = threading.Barrier(threads)

    def worker() -> None:
        local: list[str] = []
        start.wait()
        for _ in range(frames):
            blob = guarded.encrypt(PLAINTEXT)
            local.append(hashlib.sha256(blob).hexdigest())
        with lock:
            digests.extend(local)

    workers = [threading.Thread(target=worker, daemon=True) for _ in range(threads)]
    for item in workers:
        item.start()
    for item in workers:
        item.join()

    return len(digests) - len(set(digests))


def compare(threads: int, frames: int) -> int:
    print(f"线程 {threads} × 每线程 {frames} 帧，明文完全相同\n", flush=True)

    unlocked = count_collisions(GuardedChannel(None), threads, frames)
    total = threads * frames
    print(f"  调用方没加锁：{unlocked} 次 nonce 重用 / {total} 帧", flush=True)
    if unlocked:
        print("    ↑ 这就是「两段明文异或泄露 + 对端计数器错位」的那一帧", flush=True)
    else:
        print(
            "    ↑ 这次没撞上。GIL 让窗口很小，但窗口存在 —— 撞不上不代表安全，"
            "只是运气好；真实链路上一旦撞上，症状是连接直接断，且日志里查不到原因。",
            flush=True,
        )

    locked = count_collisions(GuardedChannel(threading.Lock()), threads, frames)
    print(f"  调用方加了锁：{locked} 次 nonce 重用 / {total} 帧", flush=True)

    if locked != 0:
        print("\n结论：加了锁还撞 —— 说明计数器还有别的入口，必须查清", flush=True)
        return 1
    print("\n结论：约定成立。加锁之后计数严格单调，与并发度无关。", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="验证 SecureChannel 的串行调用约定")
    parser.add_argument("--threads", type=int, default=8)
    parser.add_argument("--frames", type=int, default=300)
    args = parser.parse_args()
    return compare(max(2, args.threads), max(1, args.frames))


if __name__ == "__main__":
    raise SystemExit(main())
