"""辅助脚本：打开抖音登录页，自动检测扫码登录完成并保存 storage-state.json。

与 scripts/login.py 的区别：无需在终端按 Enter，轮询检测登录成功后自动保存。
用法：python scripts/login_auto.py [最长等待秒数，默认300]
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from playwright.async_api import async_playwright

DOUYIN_URL = "https://www.douyin.com/"


async def login(timeout_seconds: int) -> None:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=False)
        context = await browser.new_context(locale="zh-CN")
        page = await context.new_page()
        await page.goto(DOUYIN_URL, wait_until="domcontentloaded")

        login_el = page.get_by_text("登录", exact=True)
        if await login_el.count():
            try:
                await login_el.first.click(timeout=10_000)
            except Exception:
                pass
        qr_login = page.get_by_text("扫码登录", exact=True)
        if await qr_login.count():
            try:
                await qr_login.first.click(timeout=5_000)
            except Exception:
                pass

        print("浏览器已打开，请在页面中扫码登录，登录成功后会自动保存凭证…", flush=True)

        # 轮询检测：登录按钮消失且能看到首页内容视为登录成功
        deadline = asyncio.get_event_loop().time() + timeout_seconds
        logged_in = False
        while asyncio.get_event_loop().time() < deadline:
            try:
                login_el = page.get_by_text("登录", exact=True)
                count = await login_el.count()
                if count == 0:
                    logged_in = True
                    break
                # 登录弹窗里的按钮不可见/已关闭也视为已登录
                if not await login_el.first.is_visible():
                    logged_in = True
                    break
            except Exception:
                pass
            await asyncio.sleep(3)

        if not logged_in:
            await browser.close()
            raise RuntimeError(f"等待 {timeout_seconds} 秒后仍未检测到登录成功")

        # 再等几秒让页面状态稳定
        await asyncio.sleep(5)
        await context.storage_state(path="storage-state.json.tmp")
        await browser.close()
        Path("storage-state.json.tmp").replace("storage-state.json")
        print("登录状态已保存到 storage-state.json", flush=True)


if __name__ == "__main__":
    timeout = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    asyncio.run(login(timeout))
