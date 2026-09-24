"""探测一个 IP:端口 上跑的到底是不是我们的桥接服务、能不能真的干活。

排查「手表连不上某台电脑」时，这个脚本在电脑侧把范围一次收窄。它按四层往下走，
因为下面这几种病象在手表上看起来**都**是「连不上」，但修法完全不同：

    1. TCP 不通          → 服务没跑 / 防火墙 / 地址填错
    2. 不是我们的服务     → 对面是旧版网页版，或压根是别的程序
    3. 握手过不去         → 令牌不对 / 协议版本不一致
    4. 握手过了但业务全废 → **服务活着，会话却被锁死了**（最阴的一种）

第 4 层是最容易漏的：握手和心跳都不碰浏览器，所以连接看起来完全正常，
手表却什么都读不出来。只做握手的探测会把它误报成「可用」—— 所以这里必须
真的发一次 status，看服务端自己怎么说。

用法：
    python scripts/probe_endpoint.py 192.168.1.15:8787
    python scripts/probe_endpoint.py 192.168.1.15:8787 --token a1b2c3d4
    python scripts/probe_endpoint.py a.example.com:8787 --timeout 8

不填 --token 时读本机的 artifacts/watch_token.txt（也就是「这台机器上服务端
认的那个令牌」）。把它拿去探另一台机器，就能验证两边令牌是不是同一个。
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.tcp_smoke import BridgeTcpClient  # noqa: E402


def local_token() -> str:
    path = PROJECT_ROOT / "artifacts" / "watch_token.txt"
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


def tcp_latency(host: str, port: int, timeout: float) -> tuple[bool, float, str]:
    t0 = time.time()
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True, (time.time() - t0) * 1000, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, (time.time() - t0) * 1000, f"{type(exc).__name__}: {exc}"
    finally:
        sock.close()


def http_probe(host: str, port: int, timeout: float) -> str:
    """发一个 HTTP 请求：如果对面是网页服务就会回 HTTP 响应。

    我们的裸 TCP 服务会把 "GET " 当成帧长度解析，发现超过上限直接断连，
    所以「被 RST」= 是我们的服务；「回 HTTP」= 对面是旧版网页版服务端。
    """
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        sock.sendall(b"GET / HTTP/1.1\r\nHost: probe\r\nConnection: close\r\n\r\n")
        try:
            data = sock.recv(64)
        except socket.timeout:
            return "无回应（像裸 TCP 服务：要等 HELLO 才说话）"
        except ConnectionResetError:
            return "连接被重置（像裸 TCP 服务：把 GET 当成非法帧长度丢掉了）"
        if data[:4] == b"HTTP":
            return "回 HTTP 响应 → 对面是网页版服务端（旧版 http-v0？）"
        return f"回了不认识的东西：{data[:48]!r}"
    except Exception as exc:  # noqa: BLE001
        return f"连接失败：{type(exc).__name__}: {exc}"
    finally:
        sock.close()


# 服务端把「会话已废」的原因放在 status.error 里，这段前缀用来认出来。
_SESSION_BROKEN_HINTS = ("已被关闭", "已失效", "安全验证", "重启桥接服务")


def business_roundtrip(client: BridgeTcpClient) -> tuple[bool, list[str]]:
    """握手之后真的发一次 status，判断这台机器「能不能干活」。

    只握手是**不够**的：握手、心跳、取图都不碰浏览器，浏览器死了它们照样通。
    只有 status（以及 watch/messages/send）才会暴露会话已经废掉。

    返回 (是否可用, 要打印的说明行)。
    """
    lines: list[str] = []
    try:
        started = time.time()
        status = client.request("status")
        elapsed = (time.time() - started) * 1000
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "不认识的指令" in message:
            lines.append("  业务往返    ✗ 对面不认识 status → 代码版本比本机旧")
            lines.append("  => 把本机的 watch-bridge 代码同步过去再重启服务。")
            return False, lines
        if "正在启动" in message:
            lines.append("  业务往返    ✗ 会话还在启动中，过几秒再探一次")
            return False, lines
        if "超时" in message:
            lines.append("  业务往返    ✗ 超时（服务可能正忙，或被浏览器卡住）")
            return False, lines
        lines.append(f"  业务往返    ✗ {type(exc).__name__}: {message[:120]}")
        return False, lines

    error = status.get("error")
    ready = status.get("ready")
    if error:
        # ★ 最关键的一格：连接一切正常，但服务端自己说会话废了。
        lines.append(f"  业务往返    ✗ 会话已被锁死（{elapsed:.0f}ms）")
        lines.append(f"  服务端自述  「{error}」")
        lines.append("  => 服务进程活着、端口通、握手也过，但**所有碰浏览器的请求都会被拒**，")
        lines.append("     手表那边的表现就是「能连上但什么都不出来」，很容易被误判为网络问题。")
        if any(hint in str(error) for hint in _SESSION_BROKEN_HINTS):
            lines.append("     这类原因只在进程启动时复位一次，**不会自愈**：")
            lines.append("     到那台电脑上重启一次 run_watch_server.bat 即可（不要只关窗口）。")
        return False, lines

    if not ready:
        lines.append(f"  业务往返    ✗ 会话未就绪（ready=False 且没有原因，可能还在启动）")
        return False, lines

    friends = status.get("friends") or []
    stickers = status.get("watch_stickers") or []
    chat = status.get("current_chat")
    lines.append(f"  业务往返    ✓ 会话可用（status {elapsed:.0f}ms）")
    lines.append(
        f"  服务端状态  好友 {len(friends)} 个 / 表情 {len(stickers)} 个"
        + (f" / 当前会话 {chat}" if chat else "")
    )
    return True, lines


def check_code_version(client: BridgeTcpClient) -> str:
    """用 2026-09 新增的 sticker_thumbs 判断对面代码新旧。

    这个 op 绕不过就绪检查，所以只有会话可用时才有意义；探不出就返回空串。
    """
    try:
        result = client.request("sticker_thumbs")
    except Exception as exc:  # noqa: BLE001
        message = str(exc)
        if "不认识的指令" in message:
            return "  => 对面是**旧版代码**：批量取图 / 收藏同步这些新能力在那台上没生效，"
            "建议把本机代码同步过去。"
        return ""
    count = result.get("count")
    size = result.get("bytes")
    if count:
        return f"  代码版本    ✓ 含批量取图（{count} 项 / {size} 字节）→ 与新版一致"
    return ""


def probe(target: str, token: str, timeout: float) -> bool:
    host, _, port_text = target.rpartition(":")
    port = int(port_text)

    print(f"===== {host}:{port} =====")

    ok, ms, note = tcp_latency(host, port, timeout)
    if not ok:
        print(f"  TCP 连接    ✗ {note}（{ms:.0f}ms）")
        print("  => 端口不通。先查服务是否在跑、防火墙是否放行、地址/端口填对没有。")
        return False
    print(f"  TCP 连接    ✓ {ms:.0f}ms")
    print(f"  HTTP 探针   {http_probe(host, port, timeout)}")

    if not token:
        print("  握手        跳过（没有可用令牌：artifacts/watch_token.txt 不存在）")
        return False

    client = BridgeTcpClient(host, port, token, timeout=max(timeout, 20))
    try:
        client.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"  握手        ✗ {type(exc).__name__}: {exc}")
        message = str(exc)
        if "令牌" in message:
            print("  => 令牌不对。对面是另一台机器，它有自己的 watch_token.txt，")
            print("     手表里存的还是旧那台的令牌 → 永远握手被拒。把对面的令牌填进手表。")
        elif "版本" in message:
            print("  => 协议版本不一致：两边的 bridge/protocol.py 不是同一份代码。")
        else:
            print("  => 对面没按我们的协议应答，或握手超时。")
        return False
    print(f"  握手        ✓ 通过（服务端：{client.server_info}）")

    try:
        usable, lines = business_roundtrip(client)
        for line in lines:
            print(line)
        if usable:
            extra = check_code_version(client)
            if extra:
                print(extra)
            print("  => 这台机器对手表是**可用**的。")
            print("     如果手表仍旧连不上，问题在手表到这里的网络路径、手表里填的地址，")
            print("     或手表侧被系统挂起（熄屏 / 深度省电）。")
    finally:
        client.close()

    return usable


def main() -> int:
    parser = argparse.ArgumentParser(description="探测桥接端点（TCP / HTTP / 握手 / 业务可用性）")
    parser.add_argument("targets", nargs="+", help="形如 192.168.1.15:8787")
    parser.add_argument("--token", default=None, help="不填则读 artifacts/watch_token.txt")
    parser.add_argument("--timeout", type=float, default=6.0)
    args = parser.parse_args()

    token = args.token if args.token is not None else local_token()
    print(f"使用的令牌：{token!r}")
    print()

    results = []
    for target in args.targets:
        results.append((target, probe(target, token, args.timeout)))
        print()

    print("小结：", ", ".join(f"{t}={'可用' if ok else '不可用'}" for t, ok in results))
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
