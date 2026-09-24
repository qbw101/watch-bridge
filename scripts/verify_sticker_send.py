"""端到端验证「预览的是哪张、发出去的就是哪张」。

回答的是那个最难受的 bug：手机上新收藏一个表情之后，手表上点 A 发出去的却是 B。
根因是定位方式 —— 收藏会把面板里的项整体顺移，按「第几栏第几个」定位就指向了邻居。
现在发送端先按**资源名**（图本身）找，库里的 source_key 就是同一套规则算出来的。

所以这个脚本的判据很直接：
    手表上那一格的 label / id → 库里的 source_key
    → 点下去之后真正出现在聊天里的那条消息，它的图片资源名必须与之一致。

用法（手表上先点好那一格，再跑）：
    python scripts/verify_sticker_send.py --expect "V我50"
    python scripts/verify_sticker_send.py            # 自己列出最近一条发出的表情
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit

PROJECT_ROOT = Path(__file__).resolve().parents[1]
for extra in (PROJECT_ROOT, PROJECT_ROOT / "scripts"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from app.selectors import sticker_resource_key  # noqa: E402
from tcp_smoke import BridgeTcpClient  # noqa: E402


def _library(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8")).get("items") or []


def main() -> int:
    parser = argparse.ArgumentParser(description="核对「点的是哪张 / 发的是哪张」")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--token", default=None)
    parser.add_argument("--friend", default=None)
    parser.add_argument("--expect", default=None, help="手表上点的那一格的 label 或 id")
    parser.add_argument("--library", default=str(PROJECT_ROOT / "watch_stickers.json"))
    args = parser.parse_args()

    token = args.token
    if not token:
        token_file = PROJECT_ROOT / "artifacts/watch_token.txt"
        if token_file.is_file():
            token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        print("没有令牌：先启动一次服务生成 artifacts/watch_token.txt")
        return 2

    items = _library(Path(args.library))

    client = BridgeTcpClient(args.host, args.port, token, crypto="gcm", timeout=60)
    client.connect()
    try:
        status = client.request("status")
        friend = args.friend or (status.get("friends") or [""])[0]
        data = client.request("messages", name=friend, limit=30)
        messages = data.get("messages") or []
    finally:
        client.close()

    outgoing = [m for m in messages if m.get("side") == "me"]
    stickers = [m for m in outgoing if m.get("type") == "sticker"]
    if not stickers:
        print(f"「{friend}」里没有已发出的表情消息，先在手表上发一个")
        return 1

    latest = stickers[-1]
    media = latest.get("media") or ""
    sent_key = sticker_resource_key(str(media))
    print(f"会话「{friend}」")
    print(f"最后一条已发送表情：text={latest.get('text')!r} time={latest.get('time')}")
    print(f"  图片地址资源名 = {sent_key or '(没有图片地址)'}")

    # 反查：这条消息的图片落在库里哪一项上
    hit = next((item for item in items if item.get("source_key") == sent_key), None)
    if hit is None:
        print("  !! 资源名在库里找不到对应项 —— 这条表情不是手表表情库发出去的")
        return 1
    print(f"  库中对应项：label={hit.get('label')!r} id={hit.get('id')!r} "
          f"tab={hit.get('tab_index')} index={hit.get('fallback_index')}")

    if not args.expect:
        print("\n没有给 --expect，只报告这一条落在库里哪一项上。")
        return 0

    want = next(
        (item for item in items
         if args.expect in (item.get("label"), item.get("id"), item.get("name"))),
        None,
    )
    if want is None:
        print(f"\n!! 库里没有 label/id/name 等于「{args.expect}」的项")
        return 1
    print(f"\n手表上点的那一格：label={want.get('label')!r} id={want.get('id')!r} "
          f"tab={want.get('tab_index')} index={want.get('fallback_index')}")
    print(f"  source_key = {want.get('source_key')}")

    if want.get("id") == hit.get("id"):
        print("\n结论：一致 —— 点的是这张，发出去的也是这张。")
        return 0
    print(
        "\n结论：不一致！\n"
        f"  点的是 {want.get('label')!r}（{want.get('source_key')}）\n"
        f"  发出去的是 {hit.get('label')!r}（{hit.get('source_key')}）"
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
