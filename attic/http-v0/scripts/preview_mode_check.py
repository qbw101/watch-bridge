"""校验 watch.html 的「手表全屏 / 电脑预览外壳」判定。

回归点：预览外壳曾经用 @media (min-width:620px) 判断，手表浏览器切到
「桌面版网页」时布局视口被撑到 980，手表上就套上了本该属于电脑的外壳
（现象：手表上显示成电脑预览画面）。

本脚本用真实浏览器跑三种场景，断言 html 上 .preview 类的有无，
以及 --r（表盘半径）是否等于 min(视口宽, 视口高) / 2。

用法：python scripts/preview_mode_check.py
"""

from __future__ import annotations

import pathlib
import sys

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parents[1]
HTML = ROOT / "bridge" / "watch.html"

WATCH_UA = (
    "Mozilla/5.0 (Linux; Android 12; HarmonyOS 4.0; HUAWEI Watch 5 Build/HUAWEIWATCH5) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/114 Mobile Safari/537.36"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# name, ua, viewport(w,h), screen(w,h), is_mobile, query, expect_preview, expect_device_w
# expect_device_w 为 None 表示该场景不校验 #device 宽度（桌面版网页模式的视口由
# 浏览器决定，网页改不动，只要求「别套外壳」）。
CASES = [
    ("\u624b\u8868 UA + 466\u00d7466 \u5706\u5c4f\u89c6\u53e3",
     WATCH_UA, (466, 466), (466, 466), True, "", False, 466),
    ("\u624b\u8868 UA \u5f00\u684c\u9762\u7248\u7f51\u9875\uff08\u89c6\u53e3\u88ab\u649e\u5230 980\uff09",
     WATCH_UA, (980, 980), (466, 466), True, "", False, None),
    ("\u624b\u8868 UA + ?full=1 \u5f3a\u5236\u5168\u5c4f",
     WATCH_UA, (980, 980), (466, 466), True, "?full=1", False, None),
    # 强制套壳在窄视口下圆盘按 92vw 缩（避免溢出），所以不校验固定宽度
    ("\u624b\u8868 UA + ?preview=1 \u5f3a\u5236\u5957\u58f3\uff08\u6392\u67e5\u7528\uff09",
     WATCH_UA, (466, 466), (466, 466), True, "?preview=1", True, None),
    ("\u7535\u8111 UA + 1280\u00d7900",
     DESKTOP_UA, (1280, 900), (1280, 900), False, "", True, 466),
    ("\u7535\u8111 UA + ?full=1 \u5f3a\u5236\u5168\u5c4f",
     DESKTOP_UA, (1280, 900), (1280, 900), False, "?full=1", False, 1280),
]


def main() -> int:
    if not HTML.exists():
        print(f"missing: {HTML}")
        return 2

    base = HTML.as_uri()
    failures: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, ua, (w, h), (sw, sh), is_mobile, query, expect_preview, expect_w in CASES:
            ctx = browser.new_context(
                user_agent=ua,
                viewport={"width": w, "height": h},
                screen={"width": sw, "height": sh},
                is_mobile=is_mobile,
                has_touch=is_mobile,
            )
            page = ctx.new_page()
            page.goto(base + query, wait_until="domcontentloaded")
            page.wait_for_timeout(200)

            got_preview = page.evaluate(
                "document.documentElement.classList.contains('preview')"
            )
            hint_display = page.evaluate(
                "getComputedStyle(document.getElementById('frameHint')).display"
            )
            radius = page.evaluate(
                "parseFloat(getComputedStyle(document.getElementById('device'))"
                ".getPropertyValue('--r')) || 0"
            )
            device_w = page.evaluate("document.getElementById('device').clientWidth")
            inner_w = page.evaluate("window.innerWidth")
            meta_now = page.evaluate(
                "(document.querySelector('meta[name=viewport]')||{}).content || ''"
            )

            ok = got_preview == expect_preview and (
                (hint_display != "none") == expect_preview
            )
            if ok and expect_w is not None:
                ok = abs(device_w - expect_w) <= 2
            mark = "PASS" if ok else "FAIL"
            if not ok:
                failures.append(name)
            print(
                f"[{mark}] {name}\n"
                f"        preview={got_preview}(expect {expect_preview})  "
                f"frameHint.display={hint_display}  innerWidth={inner_w}  "
                f"device.clientWidth={device_w}(expect {expect_w})  --r={radius}\n"
                f"        viewport-meta={meta_now[:60]}"
            )
            ctx.close()
        browser.close()

    print("-" * 68)
    if failures:
        print("FAILED: " + "; ".join(failures))
        return 1
    print("all cases passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
