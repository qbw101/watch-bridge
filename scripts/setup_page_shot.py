"""把服务端配置页渲染成一张图（不启动抖音、不占 8787）。

用途：改 `bridge/setup.html` 的时候不用真跑一遍服务就能看效果 ——
它用一个假的状态（两张网卡、一台手表已连接、一个登录好的抖音账号、一份读出来的好友
名单）+ 真 `config.json` 的副本起真页面，用真 Chromium 打开，把 JS 的报错打出来，
最后落一张 PNG。

**端口是让系统临时分配的**（`start(0)`）：绝不能去抢 8788 —— 那上面可能正跑着用户
自己的服务，撞上去会把对方的实例当成自己的用。

跑法：
    python scripts/setup_page_shot.py                 # 存 artifacts/debug/setup_page.png
    python scripts/setup_page_shot.py --state ready    # 渲染「服务已运行」的样子
    python scripts/setup_page_shot.py --state first --click   # 顺便点一次「保存并启动服务」
    python scripts/setup_page_shot.py --scan           # 顺便点「从抖音读取好友」再勾一个，看动态部分
    python scripts/setup_page_shot.py --account missing  # 没登录时长什么样
    python scripts/setup_page_shot.py --account real     # 用真凭证的副本（昵称头像更真实）

`--click` / `--scan` 会真发一次 POST，配置写进临时目录，
**不碰真实的 server_config.json，也不碰真实的 config.json / storage-state.json**。
`--account real` 是唯一会读真文件的地方，而且只读（复制一份再渲染）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bridge import credentials, friend_scan, server_config, setup_web, watch_friends  # noqa: E402

FAKE_UID = "MS4wLjABAAAA9Xk2pQ7vR3mL5tZ8wY1cB4nD6eF0gH2jK4mN6pQ8rS0tU2vW"
# 假头像：内联 SVG，画个蓝底白字的「q」。用 data: 是为了截图不依赖网络，
# 也不会把真头像的 CDN 地址发出去。
FAKE_AVATAR = ("data:image/svg+xml;utf8,"
               "<svg xmlns='http://www.w3.org/2000/svg' width='64' height='64'>"
               "<rect width='64' height='64' rx='32' fill='%230A84FF'/>"
               "<text x='32' y='43' font-size='32' fill='%23ffffff' text-anchor='middle' "
               "font-family='sans-serif'>q</text></svg>")


# 「从抖音读取好友」的假结果：截图 / 走查用，不碰浏览器。
# 刻意做成「不齐」的名单，一张图里三种状态都能看见：已启用且对得上（多数）、
# 读到但没勾（「新来的人」）、启用了但这次没读到（故意漏掉的「小陈」）。
FAKE_ROSTER = [
    {"name": "小明", "preview": "晚安"},
    {"name": "阿华", "preview": "在吗"},
    {"name": "小林", "preview": "[图片]"},
    {"name": "小周", "preview": "明天见"},
    {"name": "小吴", "preview": "哈哈"},
    {"name": "小郑", "preview": "到了"},
    {"name": "新来的人", "preview": "你好呀"},
]


def fake_info(mode: str):
    """假的状态。两种形态对应页面两种主要样子：等确认 / 已运行。"""
    ready = mode == "ready"

    def provide() -> dict:
        return {
            "lan": ["192.168.1.80", "28.0.0.1", "192.168.72.1"],
            "ipv6": ["2408:8214:1a2b::5c6d"],
            "service": {
                "backend_ready": ready,
                "startup_error": None,
                "sessions": 1 if ready else 0,
                "log_path": str(PROJECT_ROOT / "artifacts" / "tcp_server.log"),
                "task": {"ok": True, "mode": "shared", "friends": 7, "messages": 2, "stickers": 2},
            },
        }

    return provide


def write_fake_credential(path: Path, *, nickname: str = "示例账号", uid: str = FAKE_UID,
                          login_ms: int = 1789477366888) -> None:
    """造一份 storage-state.json（假 cookie，只为让页面有东西可显示）。"""
    payload = {
        "cookies": [
            {"name": "sessionid", "value": "fake", "domain": ".douyin.com", "path": "/"},
            {"name": "login_time", "value": str(login_ms), "domain": ".douyin.com", "path": "/"},
        ],
        "origins": [{
            "origin": "https://www.douyin.com",
            "localStorage": [{
                "name": "user_info",
                "value": json.dumps({"uid": uid, "nickname": nickname,
                                     "avatarUrl": FAKE_AVATAR},
                                    ensure_ascii=False),
            }],
        }],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def prepare_credential(path: Path, mode: str) -> None:
    """按 `--account` 摆好凭证：真文件副本 / 造的假账号 / 没有 / 坏了。

    默认走 `fake`：截图内容确定、不依赖网络、不把真的昵称头像写进产物。
    想看真账号长什么样就显式 `--account real`（只读复制，不改真文件）。
    """
    if mode == "missing":
        return
    if mode == "broken":
        path.write_text("{ 这不是 JSON", encoding="utf-8")
        return
    if mode == "real":
        real = PROJECT_ROOT / credentials.CONFIG_NAME
        if real.is_file():
            shutil.copyfile(real, path)
            return
    write_fake_credential(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="渲染服务端配置页")
    parser.add_argument("--state", choices=("first", "ready"), default="first",
                        help="first=首次启动向导（默认），ready=服务已运行")
    parser.add_argument("--friends", choices=("demo", "real", "missing", "broken"), default="demo",
                        help="好友名单那块渲染成什么样子：内置示例名单（默认）/ 本机真 config.json 的副本 / 没有这份文件 / 文件坏了")
    parser.add_argument("--account", choices=("fake", "real", "missing", "broken"), default="fake",
                        help="抖音账号那块：造的假账号（默认）/ 真凭证的副本 / 没登录 / 凭证文件坏了")
    parser.add_argument("--out", default=None, help="输出 PNG，默认 artifacts/debug/setup_page_<state>.png")
    parser.add_argument("--click", action="store_true", help="顺便点一次主按钮（真发 POST）")
    parser.add_argument("--scan", action="store_true", help="顺便点一次「从抖音读取好友」并勾一个，看动态渲染")
    parser.add_argument("--width", type=int, default=1000)
    parser.add_argument("--height", type=int, default=1500)
    args = parser.parse_args()

    out = Path(args.out) if args.out else (PROJECT_ROOT / "artifacts" / "debug" / f"setup_page_{args.state}.png")
    out.parent.mkdir(parents=True, exist_ok=True)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("没装 playwright（这个脚本只在需要看页面样子时用）：pip install playwright")
        return 1

    with tempfile.TemporaryDirectory(prefix="watch-setup-shot-") as tmp:
        cfg_path = Path(tmp) / "server_config.json"
        config, _ = server_config.load(cfg_path)
        config.token = "a1b2c3d4"
        if args.state == "ready":
            config.setup_done = True
            config.save()

        # 好友名单默认用内置示例。传 --friends real 才会拿本机那份 config.json 的
        # 副本来渲染（写进临时目录，这个脚本永远不碰项目根那份）——
        # 但那样截图里会带上真实的好友名字，别直接发出去。
        task_path = Path(tmp) / "config.json"
        real_task = PROJECT_ROOT / "config.json"
        if args.friends == "demo":
            task_path.write_text(
                json.dumps({"friends": ["小明", "阿华", "小林", "小陈", "小周", "小吴"]},
                           ensure_ascii=False, indent=2),
                encoding="utf-8")
        elif args.friends == "real" and real_task.is_file():
            task_path.write_text(real_task.read_text(encoding="utf-8"), encoding="utf-8")
        elif args.friends == "broken":
            task_path.write_text("{ 这不是 JSON", encoding="utf-8")

        cred_path = Path(tmp) / "storage-state.json"
        prepare_credential(cred_path, args.account)

        app = setup_web.SetupWeb(
            config,
            info_provider=fake_info(args.state),
            friend_store=watch_friends.FriendStore(task_path),
            scanner=friend_scan.FriendScanner(lambda should_stop: FAKE_ROSTER),
            account_store=credentials.AccountStore(cred_path, project_root=PROJECT_ROOT),
        )
        # 端口传 0：让系统分临时端口。绝不能从 8788 试起 —— 那上面可能正跑着用户
        # 自己的服务，撞上去会把对方的实例当成自己的用（真踩过，写坏了真实配置）。
        url = app.start(0)
        if not url:
            print("配置页起不来")
            return 1
        probe = app.state()
        if probe.get("config_path") != str(cfg_path):
            print(f"× 这个实例对不上（config_path={probe.get('config_path')}）—— 停手。")
            app.stop()
            return 1
        if args.state == "ready":
            app.runtime = config.copy()

        errors: list[str] = []
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True)
                page = browser.new_page(viewport={"width": args.width, "height": args.height},
                                        device_scale_factor=1)
                page.on("console", lambda msg: errors.append(f"{msg.type}: {msg.text}")
                        if msg.type in ("error", "warning") else None)
                page.on("pageerror", lambda exc: errors.append(f"pageerror: {exc}"))
                page.goto(url, wait_until="load")
                page.wait_for_timeout(1200)
                # 状态轮询跑两轮，把「服务已就绪」这类异步文案也带出来
                page.wait_for_timeout(2500)
                if args.scan:
                    # 顺便把「点读取 → 列表多出没勾的人 → 勾上他」这条路径走一遍
                    page.click("#btnScan")
                    page.wait_for_timeout(1200)
                    page.click("#friendList .friendrow:last-child input")
                    page.wait_for_timeout(400)
                if args.click:
                    page.click("#btnSave")
                    page.wait_for_timeout(1500)
                page.screenshot(path=str(out), full_page=True)

                token_text = page.inner_text("#tokenOut")
                addr_text = page.inner_text("#addrOut")
                badge = page.inner_text("#svcText")
                button = page.inner_text("#btnSave")
                friend_badge = page.inner_text("#friendBadge")
                scan_note = page.inner_text("#scanNote")
                acct_name = page.inner_text("#acctName")
                acct_badge = page.inner_text("#acctBadge")
                acct_meta = page.inner_text("#acctMeta")
                rows = page.eval_on_selector_all("#friendList .friendrow", "els => els.length")
                checked = page.eval_on_selector_all("#friendList input:checked", "els => els.length")
                listed = page.eval_on_selector("#friendCount", "el => el.textContent")
                browser.close()
        finally:
            app.stop()

        print(f"截图: {out}  ({out.stat().st_size // 1024} KB)")
        print(f"  状态徽章: {badge}")
        print(f"  电脑地址: {addr_text}")
        print(f"  令牌:     {token_text}")
        print(f"  主按钮:   {button}")
        print(f"  抖音账号: {acct_name}（{acct_badge}）{acct_meta}")
        print(f"  好友块:   {friend_badge} · 已启用 {listed} 位")
        print(f"  列表读回: {rows} 行（勾上 {checked} 个）")
        print(f"  读取提示: {scan_note}")
        if args.click:
            after, _ = server_config.load(cfg_path)
            print(f"  点击后落盘: port={after.port} setup_done={after.setup_done}")
            if task_path.is_file():
                saved = json.loads(task_path.read_text(encoding="utf-8"))
                print(f"  好友名单落盘: {json.dumps(saved.get('friends'), ensure_ascii=False)}")
                print(f"  文件里剩下的键: {sorted(saved)}")
        # 头像 CDN 在无头环境里加载失败会记一条 "Failed to load resource"，页面本身
        # 已经处理了（露出首字母），不算问题。
        noisy = [line for line in errors if "Failed to load resource" in line]
        real = [line for line in errors if line not in noisy]
        if real:
            print("浏览器控制台有动静：")
            for line in real:
                print("   ", line)
            return 1
        print("浏览器控制台干净" + (f"（忽略 {len(noisy)} 条头像加载失败）" if noisy else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
