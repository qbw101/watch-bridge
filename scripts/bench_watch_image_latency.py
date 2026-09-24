"""量「手表按现在的时序拉全部表情缩略图」到底要多久。

`bench_sticker_images.py` 量的是服务端处理一张图要多久（结论：70 张共 0.89s，
不是瓶颈）。真正决定手表体感的是**往返次数**：手表端每张图发一次 `op=image`，
而并发闸门 `ImageCache.MAX_CONCURRENT` 是 3 —— 于是总时间 ≈
    （张数 / 并发）× 单次往返
本脚本用 `BridgeTcpClient`（含和手表一样的发送锁）复现这个时序，把两个因子都量出来：

- 单张往返：串行拉 10 张，取中位数（这是链路 RTT + 服务端处理）；
- 全量：按指定并发拉完整库，报总耗时和每张的分布。

用法：
    python scripts/bench_watch_image_latency.py                    # 默认并发 3、全库
    python scripts/bench_watch_image_latency.py --concurrency 1    # 看串行下界
    python scripts/bench_watch_image_latency.py --concurrency 6
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from tcp_smoke import BridgeTcpClient  # noqa: E402  (放这里才能先改 sys.path)


def _fetch_one(client: BridgeTcpClient, ref: str, px: int) -> tuple[float, int]:
    started = time.perf_counter()
    result = client.request("image", kind="sticker", ref=ref, px=px, need_image=True)
    body = client.image_bytes(result)
    return time.perf_counter() - started, len(body)


def main() -> int:
    parser = argparse.ArgumentParser(description="量手表端时序下的表情取图耗时")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--token", default=None, help="不填则读 artifacts/watch_token.txt")
    parser.add_argument("--px", type=int, default=72)
    parser.add_argument("--concurrency", type=int, default=3)
    parser.add_argument("--serial-samples", type=int, default=10, help="串行单张往返取样数")
    parser.add_argument("--limit", type=int, default=0, help="只拉前 N 张（0 = 全部）")
    args = parser.parse_args()

    token = args.token
    if not token:
        token = (PROJECT_ROOT / "artifacts" / "watch_token.txt").read_text(encoding="utf-8").strip()

    client = BridgeTcpClient(args.host, args.port, token)
    client.connect()
    try:
        status = client.request("status")
        stickers = status.get("watch_stickers") or []
        refs = [str(item.get("id")) for item in stickers if item.get("has_thumb")]
        if args.limit:
            refs = refs[: args.limit]
        print(f"表情库 {len(stickers)} 项，其中带缩略图 {len(refs)} 项；单张目标边长 {args.px}px")

        # 1) 串行单张：量「一次往返」本身
        samples: list[float] = []
        sizes: list[int] = []
        for ref in refs[: args.serial_samples]:
            elapsed, size = _fetch_one(client, ref, args.px)
            samples.append(elapsed)
            sizes.append(size)
        if samples:
            print(
                f"\n串行单张往返：中位 {statistics.median(samples) * 1000:.0f}ms，"
                f"最快 {min(samples) * 1000:.0f}ms，最慢 {max(samples) * 1000:.0f}ms，"
                f"平均 {statistics.mean(sizes) / 1024:.1f}KB/张"
            )

        # 2) 全量：按手表端的并发闸门拉一遍
        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
            results = list(pool.map(lambda ref: _fetch_one(client, ref, args.px), refs))
        total = time.perf_counter() - started
        latencies = sorted(r[0] for r in results)
        payload = sum(r[1] for r in results)
        print(
            f"\n并发 {args.concurrency} 拉全量 {len(refs)} 张：\n"
            f"  总耗时 {total:.2f}s（每张平均 {total / max(1, len(refs)) * 1000:.0f}ms 摊销）\n"
            f"  单张耗时 中位 {statistics.median(latencies) * 1000:.0f}ms / "
            f"P90 {latencies[int(len(latencies) * 0.9) - 1] * 1000:.0f}ms / "
            f"最慢 {latencies[-1] * 1000:.0f}ms\n"
            f"  传输 {payload / 1024:.0f}KB"
        )
        print(
            "\n判读：总耗时 ≈ 张数/并发 × 单张往返。单张往返是链路 RTT 主导的"
            "（服务端只花 ~13ms），所以「减少往返次数」比「提高并发」更有效。"
        )
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
