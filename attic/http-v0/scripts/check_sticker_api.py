"""表情库接口的回归测试：/api/stickers 与 /api/sticker_thumb。

刻意不走 start_backend（那会打开浏览器去连抖音），只起一个空的 HTTP 服务。
所以 /api/status 会是 503 —— 这正是要验证的点：表情库接口本来就不该依赖
抖音会话是否就绪，会话没起来时表情图标照样出得来。

    python scripts/check_sticker_api.py

占用 127.0.0.1:8799，跑完即释放。全部通过时退出码为 0。
"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.models import Settings
from bridge.server import BridgeServer
from bridge.sticker_store import LibraryItem, StickerLibrary

PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)
TOKEN = "testtoken"
BASE = "http://127.0.0.1:8799"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'✓' if ok else '×'} {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def request(path: str, headers: dict[str, str] | None = None):
    req = urllib.request.Request(BASE + path, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="sticker_api_"))
    thumb_dir = tmp / "artifacts" / "stickers"
    thumb_dir.mkdir(parents=True)
    (thumb_dir / "aaaa111111.png").write_bytes(PNG_1PX)

    lib = StickerLibrary(tmp / "watch_stickers.json", thumb_dir)
    lib.replace_all([
        LibraryItem(id="aaaa111111", name="表情", label="可爱", category="我的表情", thumb="aaaa111111.png"),
        LibraryItem(id="bbbb222222", name="续火花", label="续火花", enabled=False),
    ])
    lib.save()

    server = BridgeServer("127.0.0.1", 8799, TOKEN)
    server.bridge.settings = Settings(
        task_config_path=tmp / "config.json",
        storage_state=None,
        cookie=None,
        headless=True,
        browser_path=None,
        artifacts_dir=tmp / "artifacts",
        trace=False,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    # ---- 鉴权
    status, _, body = request("/api/stickers")
    check("无令牌访问 /api/stickers 返回 401", status == 401, f"实际 {status} {body[:80]!r}")

    # ---- 表情库
    status, headers, body = request(f"/api/stickers?token={TOKEN}")
    check("带令牌访问 /api/stickers 返回 200", status == 200, f"实际 {status}")
    payload = json.loads(body.decode("utf-8"))
    items = payload.get("items", [])
    check("只返回 enabled 的项（禁用的那条被过滤）", len(items) == 1, f"实际 {len(items)} 项")
    if items:
        item = items[0]
        check("项字段齐全", set(item) == {"id", "name", "label", "category", "has_thumb"}, str(item))
        check("has_thumb 为真", item.get("has_thumb") is True)
    check("stats 报告总数与启用数", payload.get("stats", {}).get("items") == 2, str(payload.get("stats")))

    # ---- 缩略图
    status, headers, body = request(f"/api/sticker_thumb?id=aaaa111111&token={TOKEN}")
    check("按 id 取缩略图返回 200", status == 200, f"实际 {status}")
    check("Content-Type 是 image/png", headers.get("Content-Type") == "image/png", str(headers.get("Content-Type")))
    check("返回的正是那张图", body == PNG_1PX, f"{len(body)} 字节")
    etag = headers.get("ETag")
    check("带 ETag", bool(etag), str(etag))
    check("长缓存头", "max-age=86400" in (headers.get("Cache-Control") or ""), str(headers.get("Cache-Control")))

    status, _, body = request(
        f"/api/sticker_thumb?id=aaaa111111&token={TOKEN}", {"If-None-Match": etag or ""}
    )
    check("同 ETag 再请求返回 304", status == 304, f"实际 {status}")

    status, _, body = request(f"/api/sticker_thumb?name={quote('续火花')}&token={TOKEN}")
    check("按名字查也能定位（但该项被禁用，应 404 无图）", status == 404, f"实际 {status} {body[:80]!r}")

    status, _, body = request(f"/api/sticker_thumb?id={quote('不存在的id')}&token={TOKEN}")
    check("未知 id 返回 404", status == 404, f"实际 {status} {body[:80]!r}")

    status, _, body = request(f"/api/stickers?token=wrongtoken")
    check("错误令牌返回 401", status == 401, f"实际 {status}")

    # 没起 backend，用不了 server.shutdown()（它会去等一个没跑起来的事件循环）
    if server._httpd is not None:
        server._httpd.shutdown()
        server._httpd.server_close()
    print()
    if failures:
        print(f"× {len(failures)} 项失败: {failures}")
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
