"""启动手表端桥接服务（裸 TCP，非 HTTP）。

用法：
    python scripts/watch_server.py                      # 默认 0.0.0.0:8787
    python scripts/watch_server.py --port 9000          # 换端口
    python scripts/watch_server.py --token mytoken      # 指定令牌
    python scripts/watch_server.py --crypto plain       # 关掉加密（排障用）
    python scripts/watch_server.py --host ::            # 双栈监听，顺带接受 IPv6
    python scripts/watch_server.py --setup              # 顺手在浏览器打开配置页

配置来源与优先级：命令行 > 环境变量 > `server_config.json` > 内置默认值。
`server_config.json` 由**服务端配置页**（`bridge/setup.html`）维护 —— 服务起来之后
浏览器打开它就能改端口/令牌/图片尺寸，不用记命令行参数。
页面上还有两块：**抖音账号**（`bridge/credentials.py`，看登录的是谁、扫码登录、退出登录）
和 **手表端要显示哪些好友**（`bridge/watch_friends.py` + `bridge/friend_scan.py` —— 名单可以
点一下从抖音私信列表里读出来勾选，写回 `config.json` 的 `friends`）。

`config.json` 现在只有一件事：`friends` 这份名单，决定手表上列出哪些会话。
本页只写这一个键，文件里别的键原样保留；写之前会验一遍，不合法一个字都不写。

启动后会打开一个抖音浏览器窗口（请保持打开）。手表端要填的是 `电脑IP:端口`
和访问令牌，这两样在启动日志和配置页里都会直接列出来。

关于外网访问：如果要让手表在离开家里 Wi-Fi 后也能连上，用穿透服务商的
**TCP 隧道**（不是 HTTP 隧道），走内地节点。本服务的应用协议不是 HTTP，
因此不落入「HTTP(S) 隧道 + 内地节点必须备案域名」那条限制。
不要把它绑到自有域名上做 CNAME —— 一旦域名指向内地节点就回到备案范围了。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import logging
from logging.handlers import RotatingFileHandler

from bridge import console as console_io
from bridge import credentials
from bridge import friend_scan
from bridge import server_config
from bridge import watch_friends
from bridge.netinfo import global_ipv6_addresses, lan_addresses

# 配置页「读取好友」一次最多收多少个会话。手表上列 200 个人本来也没法用，
# 这个数只是防着滚动停不下来。
FRIEND_SCAN_LIMIT = 200


DEFAULT_PORT = 8787
TOKEN_FILE = Path("artifacts/watch_token.txt")
FIRST_RUN_WAIT_SECONDS = 300


def _setup_logging() -> Path:
    """日志同时给控制台和滚动文件各一份。

    控制台那份写的是 `bridge.console` 换过的不阻塞队列流：Windows 上控制台一旦
    进入选择态（快速编辑模式被点一下、Ctrl+M、拖选）就会把写入冻住，而 logging
    是持锁调 handler 的 —— 一个线程卡在写控制台里，其它线程全会堆在锁上，
    整个服务看起来就是「卡死了，敲两下键盘才继续」。换成队列之后，被冻住的只有
    那条落屏线程。

    文件那份是给用户事后查问题的：以前只往控制台写，窗口一关就什么都没有了。
    """
    log_path = PROJECT_ROOT / "artifacts" / "tcp_server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(
            RotatingFileHandler(log_path, maxBytes=4 * 1024 * 1024, backupCount=3, encoding="utf-8")
        )
    except OSError as exc:  # 磁盘满、文件被独占……文件日志是锦上添花，不该拦住启动
        print(f"（文件日志不可用，只写控制台：{exc}）", flush=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )
    return log_path


def _report_console_stall(seconds: float, dropped: int) -> None:
    """控制台真的被冻住过：记进日志文件，用户回头能查到「刚才那几秒怎么了」。

    回调跑在落屏线程上，而它写的是队列流，所以这里不会又把自己卡住。
    """
    logging.getLogger("douyin_watch").warning(
        "控制台输出被冻结 %.1f 秒%s（服务本身没受影响）",
        seconds,
        f"，期间丢弃 {dropped} 段日志" if dropped else "",
    )


def _write_token_file(token: str) -> None:
    """把令牌同步到老位置 `artifacts/watch_token.txt`。

    配置页里换了令牌、或者命令行传了 `--token`，这份文件也要跟着变 ——
    文档、排查脚本（`scripts/probe_endpoint.py`）都还按它找令牌，
    留着旧值会让人拿着过期令牌查半天。
    """
    try:
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_FILE.write_text(token + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"（令牌文件无法写入，不影响运行：{exc}）", flush=True)


def _load_or_create_token(explicit: str | None, configured: str) -> str:
    """定下本次运行的令牌：命令行 > 环境变量 > 配置文件 > 老令牌文件 > 新生成。"""
    if explicit and explicit.strip():
        return explicit.strip()
    env_token = (os.getenv("WATCH_TOKEN") or "").strip()
    if env_token:
        return env_token
    if configured.strip():
        return configured.strip()
    if TOKEN_FILE.is_file():
        existing = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    return secrets.token_hex(4)


# ---------------------------------------------------------------------- 配置


def _apply_env_overrides(config: server_config.ServerConfig) -> list[str]:
    """环境变量覆盖。解析不了的记一条说明就跳过，别让一个手滑的 export 拦住启动。"""
    notes: list[str] = []
    if (value := (os.getenv("WATCH_HOST") or "").strip()):
        config.host = value
    if (value := (os.getenv("WATCH_PORT") or "").strip()):
        try:
            config.port = int(value)
        except ValueError:
            notes.append(f"WATCH_PORT={value} 不是数字，已忽略。")
    if (value := (os.getenv("WATCH_CRYPTO") or "").strip()):
        if value in server_config.CRYPTO_MODES:
            config.crypto = value
        else:
            notes.append(f"WATCH_CRYPTO={value} 无法识别，已忽略。")
    return notes


def _apply_cli_overrides(config: server_config.ServerConfig, args: argparse.Namespace) -> None:
    """命令行最优先 —— 用户当场敲的参数不该被配置文件盖掉。"""
    if args.host is not None:
        config.host = args.host
    if args.port is not None:
        config.port = args.port
    if args.crypto is not None:
        config.crypto = args.crypto
    if args.sticker_px is not None:
        config.sticker_px = args.sticker_px
    if args.media_px is not None:
        config.media_px = args.media_px


def _friend_store() -> "watch_friends.FriendStore":
    """定位 `config.json`。

    `app/config.py` 用的是相对当前工作目录的路径（`run_watch_server.bat` 会先 cd 到
    项目根，所以平时是对的）。但如果有人从别处 `python scripts/watch_server.py`，
    相对路径就落到别处去了 —— 端口、令牌都有配置文件兜底，好友名单不该因为
    「从哪儿启动」而变成另一份。所以这里相对路径一律按项目根解析。
    """
    raw = Path(os.getenv(watch_friends.ENV_CONFIG, watch_friends.CONFIG_NAME)).expanduser()
    if not raw.is_absolute():
        raw = PROJECT_ROOT / raw
    return watch_friends.FriendStore(raw)


def _friend_reader(box: dict[str, Any]):
    """给配置页的「读取好友」用：复用正在跑的那个抖音窗口读会话列表。

    为什么要复用而不是另开一个浏览器：同一账号再开一个既费资源又容易触发风控，
    而且用户已经把那个窗口登录好了。代价是这个按钮只在服务跑起来之后有用 ——
    还没就绪时给一句人话，而不是让 Playwright 抛一句英文。

    这个函数会被放在后台线程里调（见 `bridge/friend_scan.py`），
    `call_interactive` 本来就是跨线程用的（内部投递到桥接的事件循环）。
    超时给得比默认长：滚到底读一遍十几秒是正常的。
    """

    def read(should_stop):
        server = box.get("server")
        if server is None or not box.get("ready"):
            raise RuntimeError(
                "抖音会话尚未就绪 —— 请等待服务打开浏览器窗口、日志出现「抖音会话已就绪」后再次点击。"
            )
        return server.call_interactive(
            server.bridge.read_all_conversations(FRIEND_SCAN_LIMIT, should_stop=should_stop),
            timeout=180,
        )

    return read


def _enter_waiter(event: threading.Event) -> None:
    """首次启动向导的退路：用户不想点页面，在控制台敲个回车也能继续。

    没接控制台（计划任务/重定向）时 `input()` 立刻抛 EOFError，线程自己退出，
    向导就只等页面 —— 这是正常的，不该报错。
    """
    try:
        input()
    except (EOFError, OSError):
        return
    event.set()


def _info_provider(box: dict[str, Any], log_path: Path):
    """配置页每次轮询都会调它一次。只读、不许抛异常（异常在 setup_web 里兜住）。

    「登录的是哪个账号」不在这里 —— 那份状态由 `bridge/credentials.py` 自己报；
    「`config.json` 现在长什么样」也不在这里 —— 那一块由 `bridge/watch_friends.py` 报。
    同一个东西有两个来源，迟早会对不上。
    """

    def provide() -> dict[str, Any]:
        server = box.get("server")
        return {
            "lan": lan_addresses(),
            "ipv6": global_ipv6_addresses(),
            "service": {
                "backend_ready": bool(box.get("ready")),
                "startup_error": getattr(server, "startup_error", None),
                "sessions": getattr(server, "session_count", 0) if server is not None else 0,
                "log_path": str(log_path),
            },
        }

    return provide


# ---------------------------------------------------------------------- 主体


def main() -> int:
    parser = argparse.ArgumentParser(description="抖音手表端桥接服务（裸 TCP）")
    # 这几个默认值故意是 None：要和「用户明确传了这个参数」区分开，
    # 否则命令行永远盖住配置文件（`--port` 的 default 一旦填了值，配置文件里的端口就永远读不到）。
    parser.add_argument(
        "--host",
        default=None,
        help="监听地址，默认取配置文件（0.0.0.0，手表需要局域网访问）；填 :: 则同时监听 IPv6",
    )
    parser.add_argument("--port", type=int, default=None, help=f"监听端口，默认取配置文件（{DEFAULT_PORT}）")
    parser.add_argument("--token", default=None, help="访问令牌；不填则复用配置文件/artifacts/watch_token.txt 里的")
    parser.add_argument(
        "--crypto",
        choices=("gcm", "auto", "plain"),
        default=None,
        help="链路加密：gcm=强制 AEAD（默认），auto=跟随客户端，plain=明文（仅排障用）",
    )
    parser.add_argument("--sticker-px", type=int, default=None, help="表情缩略图下发时的最长边像素，默认 72")
    parser.add_argument("--media-px", type=int, default=None, help="聊天图片下发时的最长边像素，默认 128")
    parser.add_argument(
        "--config",
        default=None,
        help=f"服务端配置文件路径，默认 <项目根>/{server_config.CONFIG_NAME}",
    )
    parser.add_argument("--setup", action="store_true", help="启动时立刻在浏览器打开配置页")
    parser.add_argument("--no-setup-web", action="store_true", help="完全不启动配置页（无图形界面的机器）")
    parser.add_argument(
        "--setup-wait",
        type=int,
        default=FIRST_RUN_WAIT_SECONDS,
        help=f"首次启动等用户确认配置的最长秒数，默认 {FIRST_RUN_WAIT_SECONDS}；超时就用当前配置继续",
    )
    args = parser.parse_args()

    # 顺序要紧：先把 stdout/stderr 换成不阻塞的队列流，之后所有输出（含 logging
    # 的 handler）才都走队列。在此之前发生的写入仍然是直写控制台。
    console_io.install_nonblocking_output()
    for channel in console_io.channels():
        channel.set_stall_callback(_report_console_stall)

    log_path = _setup_logging()
    quick_edit = console_io.disable_quick_edit()

    if Path.cwd() != PROJECT_ROOT:
        # .env / storage-state.json / config.json 里的相对路径都是按**当前工作目录**
        # 解析的（app/config.py 的写法），而配置页里的路径按项目根算。两边不一致时
        # 页面改的可能不是服务真正读的那份 —— 与其让人事后对不上，不如当场说一句。
        print(f" 注意：当前工作目录不是项目根（{Path.cwd()}），而项目根是 {PROJECT_ROOT}。", flush=True)
        print("      .env / storage-state.json / config.json 将按当前目录解析，配置页按项目根计算。", flush=True)
        print("      建议用 run_watch_server.bat 启动，或者先 cd 到项目根。", flush=True)

    # ---------------------------------------------------------------- 配置
    config_path = Path(args.config).expanduser() if args.config else (PROJECT_ROOT / server_config.CONFIG_NAME)
    config, notes = server_config.load(config_path)
    for note in notes:
        print(f" 提示：{note}", flush=True)
    for note in _apply_env_overrides(config):
        print(f" 提示：{note}", flush=True)
    _apply_cli_overrides(config, args)

    first_run = not config.setup_done
    token = _load_or_create_token(args.token, config.token)
    config.token = token
    _write_token_file(token)

    print("=" * 64, flush=True)
    print(" 抖音手表助手 - 正在启动（请保持本窗口与浏览器窗口开启）", flush=True)
    print("=" * 64, flush=True)
    quick_edit_ok = quick_edit in ("已关闭", "本来就是关闭的", "没有附加控制台，跳过")
    print(f" 控制台快速编辑模式：{quick_edit}", flush=True)
    if not quick_edit_ok:
        print("   ⚠ 未能关闭：在此窗口中点击鼠标将导致输出冻结（服务不会中断，", flush=True)
        print("     日志里会留一条「控制台输出被冻结」的记录）。", flush=True)
    print(f" 运行日志：{log_path}", flush=True)
    print("", flush=True)

    # ---------------------------------------------------------------- 配置页
    box: dict[str, Any] = {"server": None, "ready": False}
    friends = _friend_store()
    account = credentials.AccountStore(project_root=PROJECT_ROOT)
    scanner = friend_scan.FriendScanner(_friend_reader(box))
    setup = None
    if not args.no_setup_web:
        from bridge import setup_web

        setup = setup_web.SetupWeb(
            config,
            info_provider=_info_provider(box, log_path),
            on_save=lambda cfg: _write_token_file(cfg.token),
            account_store=account,
            friend_store=friends,
            scanner=scanner,
        )
        url = setup.start()
        if url:
            print(f" 服务端配置页：{url} {setup.startup_note}".rstrip(), flush=True)
        else:
            print(f" 服务端配置页未能启动 {setup.startup_note}（不影响收发消息）", flush=True)
        print("", flush=True)

    # ------------------------------------------------------ 首次启动：确认配置
    if setup is not None and setup.running:
        open_now = args.setup or config.open_page == "always" or (first_run and config.open_page == "first")
        if config.open_page == "never" and not args.setup:
            open_now = False
        if first_run:
            print(" 首次启动的该步骤用于确认配置：端口与令牌决定手表端需要填写的内容。", flush=True)
            print(" 页面上还可直接从抖音读取会话并勾选手表端要显示的项（需服务启动后才能读取）。", flush=True)
        if open_now:
            try:
                webbrowser.open(setup.url)
                print(f" 已用浏览器打开配置页：{setup.url}", flush=True)
            except Exception as exc:  # noqa: BLE001 - 打不开浏览器不该拦住服务
                print(f" （浏览器未打开：{exc} —— 请手动复制上述地址到浏览器）", flush=True)
        if first_run:
            print(" 请在页面中点击「保存并启动服务」，或在此窗口按回车，直接以上述值启动。", flush=True)
            print("", flush=True)
        if first_run and open_now:
            threading.Thread(target=_enter_waiter, args=(setup.launch_event,), daemon=True).start()
            confirmed = setup.launch_event.wait(timeout=max(0, args.setup_wait))
            if confirmed:
                print(" 配置已确认，继续启动。", flush=True)
            else:
                print(f" 等待 {args.setup_wait} 秒未收到确认，以当前配置继续启动。", flush=True)
        elif first_run:
            # 页面没打开就别干等：没人会去点那个按钮。直接按当前值起，日志里有地址。
            print(" （未自动打开配置页，故不等待 —— 如需修改配置，请手动打开上述地址。）", flush=True)
            print("", flush=True)
        if first_run:
            # 用户可能刚在页面里改过端口/令牌 —— 重新读文件，但保住对象身份
            # （配置页握着的就是 config 这个引用，换对象等于把它写进黑洞）。
            reloaded, reload_notes = server_config.load(config_path)
            config.adopt(reloaded)
            for note in reload_notes:
                print(f" 提示：{note}", flush=True)
            _apply_env_overrides(config)
            _apply_cli_overrides(config, args)
            config.mark_setup_done()
            config.save()
            print("", flush=True)

    # ---------------------------------------------------------------- 起服务
    from bridge import TcpBridgeServer

    server = TcpBridgeServer(
        config.host,
        config.port,
        config.token,
        crypto=config.crypto,
        sticker_px=config.sticker_px,
        media_px=config.media_px,
    )
    box["server"] = server

    addresses = lan_addresses() or ["<电脑局域网IP>"]
    crypto_label = {
        "gcm": "AES-256-GCM（X25519 密钥交换，强制）",
        "auto": "跟随客户端（排障模式，可能落到明文）",
        "plain": "明文（不推荐）",
    }[config.crypto]

    try:
        server.start_backend()
        if server.startup_error:
            print(f"启动失败: {server.startup_error}", flush=True)
            print("常见原因：登录状态失效 —— 在配置页点「扫码登录」重新扫一次", flush=True)
            print("（命令行也行：scripts\\login_auto.py），或已有另一个任务在运行", flush=True)
            print("（运行 scripts\\clear_stale_lock.py 清理后重试）。", flush=True)
            return 1
        box["ready"] = True
        if setup is not None:
            # 「正在跑的那份值」从这里开始才有意义：服务真正开始监听之后，页面才能
            # 准确算出「你改的这些还没生效」。
            setup.runtime = config.copy()

        print("", flush=True)
        print("手表端「电脑地址」请填下面任意一个（同一 Wi-Fi 下）：", flush=True)
        for address in addresses:
            print(f"   {address}:{config.port}", flush=True)
        print("", flush=True)

        v6 = global_ipv6_addresses()
        if v6:
            print("检测到公网 IPv6 —— 手表用蜂窝网络时可以直接填下面这个地址，不用内网穿透：", flush=True)
            for address in v6:
                print(f"   [{address}]:{config.port}", flush=True)
            print("（要让它生效，路由器防火墙需放行该端口的入站连接）", flush=True)
            print("", flush=True)

        print(f"访问令牌: {config.token}", flush=True)
        print(f"链路加密: {crypto_label}", flush=True)
        who = account.describe()
        if who["exists"]:
            label = who["nickname"] or "（凭证里没有账号信息）"
            when = f"，登录于 {who['login_at']}" if who["login_at"] else ""
            print(f"抖音账号: {label}{when}", flush=True)
        else:
            print("抖音账号: 还没有登录凭证 —— 用配置页的「扫码登录」扫一次", flush=True)
        print("", flush=True)
        if setup is not None and setup.running:
            print(f"服务端配置页: {setup.url}（服务运行期间随时可开，改端口/令牌不用记参数）", flush=True)
            print("", flush=True)
        print("注意：这是裸 TCP 服务，不是网站。浏览器打开这个地址只会收到一堆乱码，", flush=True)
        print("      这是正常的 —— 手表端应用说的是同一套二进制帧协议。", flush=True)
        print("外网访问请用穿透服务商的 TCP 隧道（走内地节点，无需备案域名）。", flush=True)
        print("按 Ctrl+C 停止服务。", flush=True)
        print("", flush=True)

        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭…", flush=True)
    finally:
        server.shutdown()
        if setup is not None:
            setup.stop()
        # 收尾顺序不能反：logging.shutdown() 会把 handler 里攒着的记录刷进队列
        # （handler 现在写的是队列流），得先让它跑完，再等队列真正落屏，
        # 否则最后那几行会留在队列里没人送。
        logging.shutdown()
        console_io.flush_output(timeout=2.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
