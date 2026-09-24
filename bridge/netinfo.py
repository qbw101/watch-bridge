"""本机地址探测：给「手表端该填什么」这个问题提供数据。

两个函数都不做任何网络请求，纯 `socket.getaddrinfo` 本地查询，毫秒级。
放在独立模块是因为启动日志和配置页都要用 —— 让启动入口去 import 配置页模块拿地址
说不通，反过来也一样。
"""

from __future__ import annotations

import socket


def _default_route_address() -> str | None:
    """走默认路由的那张网卡的地址（「手表该填哪个 IP」的正确答案）。

    UDP 是无连接的，这句不会真的发包 —— 只是让内核按路由表挑一条出口，
    然后问它「这条路的源地址是谁」。装了 WSL / Hyper-V / 虚拟网卡时，
    `getaddrinfo` 列出来的一串地址里第一个往往不是这张（实测过 28.0.0.1），
    所以要以路由表为准。
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        address = sock.getsockname()[0]
        sock.close()
        return address or None
    except OSError:
        return None


def _rank(address: str) -> int:
    """越可能是「手表能连上的那张网卡」排越前。

    别用「走默认路由的那张网卡」当唯一答案：这台机器上它返回的是 28.0.0.1 ——
    装了代理 / VPN / 加速器之后，去 8.8.8.8 的路确实是从那张虚拟网卡出去的，
    但手表在同一 Wi-Fi 下根本到不了那个地址。真正能连的是 192.168.x / 10.x
    这类私网地址，所以按「私网优先」排。
    """
    if address.startswith("192.168."):
        return 0
    if address.startswith("10."):
        return 1
    parts = address.split(".")
    if len(parts) == 4 and parts[0] == "172" and parts[1].isdigit() and 16 <= int(parts[1]) <= 31:
        return 2
    if address.startswith("169.254."):  # APIPA：DHCP 没拿到地址时的自分配，没法用
        return 9
    return 5  # 公网 / 隧道 / 虚拟网卡，手表多半到不了


def lan_addresses() -> list[str]:
    """本机可被手表访问到的 IPv4，**最可能的那张排最前**（排除回环）。

    第一个是配置页默认选中的那个 —— 填错的话用户会拿着一个连不上的地址去手表上
    折腾半天，值得按上面的规则认真排。
    """
    candidates: list[str] = []
    route = _default_route_address()
    if route:
        candidates.append(route)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = info[4][0]
            if address not in candidates and not address.startswith("127."):
                candidates.append(address)
    except OSError:
        pass
    # 稳定排序：同档次保持原顺序（路由探到的那项在同档次里仍然靠前）
    indexed = sorted(enumerate(candidates), key=lambda pair: (_rank(pair[1]), pair[0]))
    return [address for _, address in indexed]


def global_ipv6_addresses() -> list[str]:
    """本机的公网 IPv6（不含链路本地和回环）。

    有公网 IPv6 的话，手表在蜂窝网络下可以直接连过来，完全不需要内网穿透 ——
    顺便也就没有备案这回事（这跟「用域名指向内地节点」是两条完全不同的路：
    域名一指向内地节点就落回备案范围，而直连 IPv6 没有域名参与）。
    """
    found: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET6):
            address = info[4][0]
            lowered = address.lower()
            # fe80:: 是链路本地，::1 是回环；只有 2xxx/3xxx 开头才是可路由的全球单播
            if lowered.startswith(("fe80", "::1")) or address in found:
                continue
            if lowered[0] in "23":
                found.append(address)
    except OSError:
        pass
    return found
