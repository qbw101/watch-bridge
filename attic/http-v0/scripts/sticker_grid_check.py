"""验证发送卡片里的表情缩略图网格：图能不能出来、会不会越出圆屏。

为什么值得单独跑一遍：表情格是固定高度的可滚动网格，塞在圆屏中央的卡片里，
一旦高度算错，就会把输入框顶到圆外（圆屏上四角是物理不存在的区域）。
光看代码看不出这个，必须真的在 466×466 的圆屏视口里量一遍。

做法：起一个 mock 服务（提供 watch.html + 几个假接口 + 真 PNG），用浏览器以
手表 UA 打开，点进会话 → 打开发送卡片 → 用 scripts/safe_area_check.js 同一套
几何算法逐个元素量「离圆心的最远点」。

    python scripts/sticker_grid_check.py
"""
from __future__ import annotations

import json
import struct
import sys
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
HTML = ROOT / "bridge" / "watch.html"
SAFE_AREA_JS = ROOT / "scripts" / "safe_area_check.js"

PORT = 8798
BASE = f"http://127.0.0.1:{PORT}"
WATCH_UA = (
    "Mozilla/5.0 (Linux; Android 12; HarmonyOS 4.0; HUAWEI Watch 5 Build/HUAWEIWATCH5) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/114 Mobile Safari/537.36"
)

# 8 个带图 + 4 个纯文字（模拟「雪碧图抓不到缩略图」的那些），外加 presets 里
# 一个库里没有的「续火花」—— 它应该被合并成一个纯文字格子。
LIBRARY = [
    {"id": f"tile{i:02d}", "name": f"表情{i}", "label": f"表情{i}", "category": "我的表情", "has_thumb": i < 8}
    for i in range(12)
]
PRESETS = ["续火花", "嗨"]

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"{'✓' if ok else '×'} {label}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures.append(label)


def solid_png(size: int, rgb: tuple[int, int, int]) -> bytes:
    """生成一张纯色 PNG。手写是为了不在这个项目里引入 Pillow。"""
    raw = b"".join(b"\x00" + bytes(rgb) * size for _ in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


THUMB_PNG = solid_png(32, (254, 44, 85))


class MockHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音
        pass

    def _reply(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parsed = urlsplit(self.path)
        if parsed.path in ("/", "/index.html"):
            self._reply(200, HTML.read_bytes(), "text/html; charset=utf-8")
            return
        if parsed.path == "/api/status":
            payload = {
                "ready": True,
                "current_chat": None,
                "friends": ["小明", "阿华"],
                "stickers": [],
                "presets": PRESETS,
                "watch_stickers": LIBRARY,
            }
            self._reply(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/messages":
            payload = {"friend": (parse_qs(parsed.query).get("name") or [""])[0], "rev": "r1", "messages": []}
            self._reply(200, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")
            return
        if parsed.path == "/api/sticker_thumb":
            self._reply(200, THUMB_PNG, "image/png")
            return
        self._reply(404, b'{"error":"not found"}', "application/json; charset=utf-8")


def main() -> int:
    if not HTML.is_file():
        print(f"missing: {HTML}")
        return 2

    server = ThreadingHTTPServer(("127.0.0.1", PORT), MockHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()

    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(
                user_agent=WATCH_UA,
                viewport={"width": 466, "height": 466},
                screen={"width": 466, "height": 466},
                is_mobile=True,
                has_touch=True,
            )
            page = ctx.new_page()
            page.goto(f"{BASE}/?token=testtoken", wait_until="domcontentloaded")

            page.wait_for_selector("#friendList .friend", timeout=10_000)
            check("好友列表渲染出来了", True)
            page.click("#friendList .friend")
            page.wait_for_timeout(400)

            # FAB 的位置是不是真的可点：圆屏下沿的可点区域随角度收窄，这里顺手量一下。
            fab = page.evaluate(
                """() => {
                    const b = document.getElementById('composeBtn');
                    const r = b.getBoundingClientRect();
                    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
                    const hit = document.elementFromPoint(cx, cy);
                    return {
                        rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
                        hit: hit ? (hit.id || String(hit.className)) : null
                    };
                }"""
            )
            print(f"      发送按钮 rect={fab['rect']}，该点最上层元素={fab['hit']}")
            check("发送按钮所在位置没有被别的层盖住", fab["hit"] == "composeBtn", f"实际命中 {fab['hit']}")

            # 弹出卡片用内联 onclick 直接调函数，不走 playwright 的 click：
            # 手机模拟下 playwright 会因为「需要先滚动到视口内」而对固定定位的 FAB
            # 报「被 #app 挡住」，滚完坐标又变了 —— 那套判定在圆屏绝对定位下不可靠。
            # 真正该验的是「按钮那个点上最上层是不是它自己」，也就是上面的
            # elementFromPoint 断言，那比 playwright 的抽象检查更贴近真机。
            page.evaluate("openComposer()")
            page.wait_for_timeout(600)
            composer_open = page.evaluate("!document.getElementById('composer').classList.contains('hidden')")
            check("发送卡片打开了", composer_open)

            tiles = page.eval_on_selector_all(
                ".sticker",
                """els => els.map(e => ({
                    label: (e.querySelector('span') || {}).textContent || '',
                    hasImg: !!e.querySelector('img'),
                    imgOk: !!e.querySelector('img') && e.querySelector('img').naturalWidth > 0
                }))""",
            )
            check("表情格子数量 = 库 12 项 + presets 补 2 项", len(tiles) == 14, f"实际 {len(tiles)} 个")
            labels = [t["label"] for t in tiles]
            check("库里没有的 presets 也进来了", "续火花" in labels and "嗨" in labels, str(labels[-3:]))
            check("同名项没有重复（库里的空 category 不产生第二个格子）", len(labels) == len(set(labels)), str(labels))

            with_img = [t for t in tiles if t["hasImg"]]
            check("8 个有缩略图的项都渲染了 img", len(with_img) == 8, f"实际 {len(with_img)} 个")
            loaded = [t for t in with_img if t["imgOk"]]
            check("缩略图真的加载出来了（naturalWidth > 0）", len(loaded) == len(with_img), f"{len(loaded)}/{len(with_img)} 张成功")

            grid = page.evaluate(
                """() => {
                    const g = document.getElementById('stickerGrid');
                    const card = document.querySelector('.sheet-card');
                    return {
                        gridClientH: g.clientHeight,
                        gridScrollH: g.scrollHeight,
                        cardH: card.getBoundingClientRect().height,
                        cardW: card.getBoundingClientRect().width,
                        inputVisible: !!document.getElementById('textInput').getClientRects().length,
                        sendVisible: !!document.getElementById('sendBtn').getClientRects().length
                    };
                }"""
            )
            check("表情区确实在滚动（内容高于可视高度）", grid["gridScrollH"] > grid["gridClientH"],
                  f"scrollH={grid['gridScrollH']} clientH={grid['gridClientH']}")
            check("输入框和发送按钮还在（没被表情挤走）", grid["inputVisible"] and grid["sendVisible"])
            print(f"      卡片 {grid['cardW']:.0f}×{grid['cardH']:.0f}，表情区可视高 {grid['gridClientH']}")

            report = json.loads(page.evaluate(SAFE_AREA_JS.read_text(encoding="utf-8")))
            over = report.get("failures", [])
            check("圆屏安全区无越界元素", not over, str(over[:5]))
            card_row = [c for c in report.get("chrome", []) if c["el"].startswith("sheet-card")]
            if card_row:
                check("发送卡片本体在圆内（有余量）", card_row[0]["margin"] > 0,
                      f"margin={card_row[0]['margin']}px")
            print(f"      表盘半径 {report['radius']}px，检查了 {len(report.get('chrome', []))} 个外框 + "
                  f"{len(report.get('inner', []))} 个内部元素")

            browser.close()
    finally:
        server.shutdown()
        server.server_close()

    print()
    if failures:
        print(f"× {len(failures)} 项失败: {failures}")
        return 1
    print("✓ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
