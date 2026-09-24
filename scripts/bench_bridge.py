"""桥接服务性能体检：逐个接口计时，验证增量协议与图片缓存。

只做只读请求（status / messages / media），不发消息。
用法：python scripts/bench_bridge.py [token] [好友名] [第二个好友名]
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8787"


class Reply:
    __slots__ = ("elapsed", "status", "body", "headers")

    def __init__(self, elapsed: float, status: int, body: bytes, headers) -> None:
        self.elapsed = elapsed
        self.status = status
        self.body = body
        self.headers = headers

    @property
    def json(self):
        try:
            return json.loads(self.body.decode("utf-8"))
        except Exception:
            return None

    @property
    def size(self) -> int:
        return len(self.body)


def call(path: str, token: str, params: dict | None = None, body: dict | None = None,
         method: str = "GET", headers: dict | None = None) -> Reply:
    query = dict(params or {})
    query["token"] = token
    url = f"{BASE}{path}?" + urllib.parse.urlencode(query)
    data = None
    request_headers = {"X-Auth-Token": token}
    if headers:
        request_headers.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=request_headers, method=method)
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            payload = response.read()
            return Reply(time.perf_counter() - start, response.status, payload, dict(response.headers))
    except urllib.error.HTTPError as exc:
        return Reply(time.perf_counter() - start, exc.code, exc.read(), dict(exc.headers or {}))


def describe(payload, limit: int = 3) -> str:
    if not isinstance(payload, dict):
        return ""
    if "error" in payload:
        return f"错误: {payload['error']}"
    if payload.get("unchanged"):
        return "内容未变（unchanged）"
    if "messages" in payload:
        messages = payload["messages"]
        if not messages:
            return "0 条消息"
        tail = messages[-limit:]
        text = " / ".join(f"{m['side']}:{m['type']}" for m in tail)
        return f"{len(messages)} 条，末尾 {text}"
    if "friends" in payload:
        return f"好友 {len(payload['friends'])} 个，ready={payload.get('ready')}"
    if "media_cache" in payload:
        return f"图片缓存 {payload['media_cache']}"
    return ""


def line(label: str, reply: Reply, extra: str = "") -> None:
    note = describe(reply.json)
    size = f"{reply.size}B"
    print(f"  {reply.elapsed:6.2f}s  {size:>7}  {label}" + (f"  -> {note}" if note else ""))
    if extra:
        print(f"          {extra}")


def main() -> int:
    token = sys.argv[1] if len(sys.argv) > 1 else ""
    if not token:
        from pathlib import Path
        token = Path("artifacts/watch_token.txt").read_text(encoding="utf-8").strip()
    friend = sys.argv[2] if len(sys.argv) > 2 else "小明"
    other = sys.argv[3] if len(sys.argv) > 3 else "小吴"

    print("=" * 74)
    print(f"桥接服务性能体检   好友={friend} / {other}")
    print("=" * 74)

    print("\n[1] 状态 / 健康")
    line("/api/status", call("/api/status", token))
    line("/api/status", call("/api/status", token))
    line("/health", call("/health", token))

    print(f"\n[2] 冷启动：在抖音里搜索并打开「{friend}」")
    cold = call("/api/messages", token, {"name": friend, "limit": 30})
    line(f"读「{friend}」", cold)
    rev = (cold.json or {}).get("rev", "")
    messages = (cold.json or {}).get("messages", [])

    print("\n[3] 热读：内容未变时应走增量协议")
    for i in range(3):
        line(f"第 {i + 2} 次（带 rev）", call("/api/messages", token, {"name": friend, "limit": 30, "rev": rev}))
    warm = [call("/api/messages", token, {"name": friend, "limit": 30}).elapsed for _ in range(3)]
    print(f"          热读平均 {sum(warm) / len(warm):.2f}s（不带 rev，走全量）")

    print(f"\n[4] 切换好友（含搜索/点列表 + 等渲染）")
    line(f"切到「{other}」", call("/api/messages", token, {"name": other, "limit": 10}))
    line(f"切回「{friend}」", call("/api/messages", token, {"name": friend, "limit": 30}))

    print("\n[5] 表情图片：首次拉取 vs 内存缓存命中 vs ETag 协商")
    media_urls = [m["media"] for m in messages if m.get("media")]
    unique = []
    for url in media_urls:
        if url not in unique:
            unique.append(url)
    if not unique:
        print("  没有带图消息，跳过")
    else:
        target = unique[0]
        first = call("/api/media", token, {"url": target})
        print(f"  {first.elapsed:6.2f}s  {first.size:>7}  首次拉取  -> {first.headers.get('Content-Type')}")
        etag = first.headers.get("ETag", "")
        second = call("/api/media", token, {"url": target})
        print(f"  {second.elapsed:6.2f}s  {second.size:>7}  内存缓存命中")
        third = call("/api/media", token, {"url": target}, headers={"If-None-Match": etag})
        print(f"  {third.elapsed:6.2f}s  {third.size:>7}  ETag 协商（期望 304）-> HTTP {third.status}")
        total = 0.0
        for url in unique:
            reply = call("/api/media", token, {"url": url})
            total += reply.elapsed
        print(f"  {total:6.2f}s  {'':>7}  整段会话 {len(unique)} 张不重复图片（首轮）")

    print("\n[6] 会话列表")
    line("/api/conversations", call("/api/conversations", token))
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
