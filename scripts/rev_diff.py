"""查 rev 为什么每 1.5 秒就变：两次读取之间到底哪个字段在动。"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.tcp_smoke import BridgeTcpClient  # noqa: E402

FRIEND = sys.argv[1] if len(sys.argv) > 1 else "小陈"
# 传第二个参数就在中途从**另一条连接**发一条表情：发送会引起页面重绘，
# 之前订阅时看到的 rev 抖动就发生在发送之后，这里把它复现出来。
SEND_STICKER = sys.argv[2] if len(sys.argv) > 2 else None


def main() -> int:
    token = (Path(r"F:\Desktop\watch-bridge\artifacts\watch_token.txt")).read_text(encoding="utf-8").strip()
    client = BridgeTcpClient("127.0.0.1", 8787, token, timeout=60)
    client.connect()
    try:
        # 先切到别的会话，再切回来 —— 这样才能复现「刚打开一个会话」的场景。
        # 稳定状态下 rev 是不变的，之前看到每 1.5 秒就变一次，怀疑是图片在
        # 逐个懒加载（media 字段从空变成 URL），这里量一下多久才收敛。
        others = client.request("status").get("friends") or []
        other = next((name for name in others if name != FRIEND), None)
        if other:
            print(f"先切到「{other}」再切回「{FRIEND}」")
            client.request("messages", name=other, limit=10)

        seen: list[str] = []
        first_items: list[dict] = []
        prev_items: list[dict] = []
        started = time.perf_counter()
        for index in range(14):
            if index == 3 and SEND_STICKER:
                peer = BridgeTcpClient("127.0.0.1", 8787, token, timeout=200)
                peer.connect()
                try:
                    print(f"  >>> 从第二条连接发送「{SEND_STICKER}」")
                    peer.request("send", name=FRIEND, text="", sticker=SEND_STICKER, timeout=200)
                finally:
                    peer.close()
            data = client.request("messages", name=FRIEND, limit=30)
            rev = str(data.get("rev"))
            items = data.get("messages") or []
            elapsed = time.perf_counter() - started
            if seen and rev != seen[-1]:
                change = _diff(prev_items, items)
                print(f"  {elapsed:5.1f}s  rev={rev}  变了：{change}")
            else:
                print(f"  {elapsed:5.1f}s  rev={rev}  （未变）")
            if not seen:
                first_items = items
            seen.append(rev)
            prev_items = items
            time.sleep(1.5)

        unique = len(set(seen))
        print(f"\n14 次读取里 rev 变了 {unique - 1} 次")
        if unique == 1:
            print("rev 全程稳定")
    finally:
        client.close()
    return 0


def _diff(old: list[dict], new: list[dict]) -> str:
    """指出两份消息列表第一处差异，用来定位 rev 为什么会变。"""
    if len(old) != len(new):
        return f"条数 {len(old)} → {len(new)}"
    for index in range(len(old)):
        for field in ("side", "type", "text", "time", "media"):
            if old[index].get(field) != new[index].get(field):
                return f"[{index}].{field}: {str(old[index].get(field))[:48]} → {str(new[index].get(field))[:48]}"
    return "字段都一样（说明只有 key/data-index 变了）"


if __name__ == "__main__":
    raise SystemExit(main())
