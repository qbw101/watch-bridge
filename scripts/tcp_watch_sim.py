"""把手表端的调用时序在电脑上复现一遍，逐条验证服务端的契约。

与 tcp_smoke.py 的分工：smoke 回答「这条链路通不通」，本脚本回答「手表端那份
ArkTS 实现依赖的每一条约定，服务端是不是真的这样做」。改完手表端代码、又没法
立刻插表的时候，先跑这个。

覆盖的都是**新的手表端实现引入、而冒烟脚本没测到**的路径：

1. **并发取图**。表情网格一挂载就是十几格同时要图（view/StickerCell.ets），
   服务端在同一秒里要发十几组「RES 头 + IMAGE 帧」。这里验证每一组字节都
   回到自己那个 reqId 上 —— 错配的表现是「表情看起来是别人」。
2. **错误帧**。手表端现在把 FLAG_ERROR 变成 Promise reject；如果服务端哪天
   改成回一个 {"ok": false} 的普通应答，那边就会把它当成功解析下去。
3. **订阅基准**。手表订阅时会把已有的 rev 带过去，服务端必须因此**不**立刻推
   一份一模一样的过来（否则用户一进会话就看到列表被重刷一遍）。
4. **unwatch**。退出会话后服务端必须停掉推送线程。

「读一个不存在的会话」那条慢路径默认不跑（要去抖音里搜完整一轮，几十秒），
需要时用 `--slow 100` 单独验：它验证的是「服务端超时之后必须回一帧错误」——
没有这条兜底，手表那边只会看到一个请求永远挂着。

用法：
    python scripts/tcp_watch_sim.py
    python scripts/tcp_watch_sim.py --friend 小陈 --concurrency 16
    python scripts/tcp_watch_sim.py --slow 100
"""

from __future__ import annotations

import argparse
import hashlib
import io
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from tcp_smoke import BridgeTcpClient  # noqa: E402

# 等一帧推送的上限。服务端在内容连续不变时会把轮询节奏放宽到 2.5 秒，
# 再算上一次读 DOM 的时间，8 秒足够；给到 12 是留穿透场景的余量。
PUSH_WAIT_SECONDS = 12


class WatchLikeClient(BridgeTcpClient):
    """发送加锁的参考客户端。

    手表端（entry/src/main/ets/service/BridgeLink.ets::sendFrame）用一条 promise 链
    把发送串行化了，原因不只是 GCM 的 nonce 计数器 —— `sendall` 本身在多个线程
    并发时也可能把两帧的字节交错写进流里。这里保持一致，测出来的才是手表端
    真正会遇到的行为。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._send_lock = threading.Lock()

    def _send(self, frame_type: int, req_id: int, payload: bytes = b"", flags: int = 0) -> None:
        with self._send_lock:
            super()._send(frame_type, req_id, payload, flags)


def _banner(text: str) -> None:
    print(f"\n=== {text} ===", flush=True)


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()[:12]


def _decodable(raw: bytes) -> bool:
    """能不能真的解成一张图 —— 只看字节数会被「一串长度对的垃圾」骗过去。"""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(raw)) as image:
            image.load()
            return image.width > 0 and image.height > 0
    except Exception:  # noqa: BLE001
        return False


def check_errors(client: WatchLikeClient, problems: list[str]) -> None:
    _banner("错误帧（必须变成 reject，而不是一个 ok=false 的普通应答）")
    # 这些路径都必须**快速**失败：它们全都在解请求体的时候就检出来了，不该碰浏览器。
    cases: list[tuple[str, dict[str, Any]]] = [
        ("不认识的指令", {"op": "__no_such_op__"}),
        ("messages 缺 name", {"op": "messages", "limit": 5}),
        ("messages 名字全空白", {"op": "messages", "name": "   ", "limit": 5}),
        ("image 缺 ref", {"op": "image", "kind": "sticker"}),
        ("image 未知来源", {"op": "image", "kind": "__bad__"}),
    ]
    for label, payload in cases:
        op = str(payload.pop("op"))
        try:
            client.request(op, **payload)
        except RuntimeError as exc:
            print(f"   {label} → {exc}", flush=True)
        except Exception as exc:  # noqa: BLE001
            # 超时说明服务端压根没回错误帧，那是真的坏了
            problems.append(f"{label}：期望错误帧，实际是 {exc.__class__.__name__}（{exc}）")
            print(f"   {label} → 异常类型不对：{exc.__class__.__name__}（{exc}）", flush=True)
        else:
            problems.append(f"{label}：服务端居然回了成功")
            print(f"   {label} → 居然成功了", flush=True)


def check_missing_friend(client: WatchLikeClient, timeout: float, problems: list[str]) -> None:
    """读一个不存在的会话。

    这条路径必须慢（服务端要去抖音里搜一遍），所以它单独放一个函数、也不在默认
    流程里跑 —— 它唯一的期望是「**最终**要回一帧错误」，而不是永远沉默。手表端
    拿不到好友名字以外的东西，正常用不会走到这里，但「服务端超时之后必须有个
    交代」这件事本身是契约的一部分：没有它，手表那边只会看到一个请求永远挂着。
    """
    _banner(f"读不存在的会话（允许慢，上限 {timeout:.0f}s）")
    started = time.perf_counter()
    try:
        client.request("messages", name="__不存在的会话__", limit=5, timeout=timeout)
    except RuntimeError as exc:
        print(f"   {time.perf_counter() - started:.1f}s 后收到错误帧：{exc}", flush=True)
        return
    except Exception as exc:  # noqa: BLE001
        problems.append(
            f"读不存在的会话在 {timeout:.0f}s 内既没成功也没回错误帧（{exc.__class__.__name__}）"
            " —— 服务端的超时兜底没生效"
        )
        print(f"   {timeout:.0f}s 内没有任何交代：{exc.__class__.__name__}（{exc}）", flush=True)
        return
    problems.append("读不存在的会话居然成功了")
    print("   居然成功了", flush=True)


def check_concurrent_images(
    client: WatchLikeClient, library: list[dict[str, Any]], px: int, concurrency: int, problems: list[str]
) -> None:
    with_thumb = [item for item in library if item.get("has_thumb") and item.get("id")]
    if not with_thumb:
        problems.append("表情库里没有带缩略图的项，并发取图测不了")
        return

    # 均匀铺满整个库。取前 N 项的话抽到的全是几 KB 的静态 PNG，测不出大小图混合
    # 交错的真实情况。
    step = max(1, len(with_thumb) // concurrency)
    picks = with_thumb[::step][:concurrency]
    _banner(f"并发取图（{len(picks)} 张同时要，模拟表情网格首屏）")

    started = time.perf_counter()
    results: dict[str, bytes] = {}
    errors: list[str] = []

    def fetch(item: dict[str, Any]) -> tuple[str, bytes]:
        reply = client.request("image", kind="sticker", ref=item["id"], px=px, need_image=True)
        return str(item["id"]), client.image_bytes(reply)

    with ThreadPoolExecutor(max_workers=len(picks)) as pool:
        for item, outcome in zip(picks, pool.map(fetch, picks)):
            try:
                ident, raw = outcome
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{item.get('label')}: {exc}")
                continue
            results[ident] = raw
    elapsed = (time.perf_counter() - started) * 1000

    total = sum(len(raw) for raw in results.values())
    print(
        f"   {len(results)}/{len(picks)} 张成功，合计 {total / 1024:.0f}KB，"
        f"耗时 {elapsed:.0f}ms（单张平均 {total / max(1, len(results)) / 1024:.0f}KB）",
        flush=True,
    )
    for message in errors:
        problems.append(f"并发取图失败：{message}")

    # 一、每张都要真的能解码。字节数对但内容是别人的图，只会在这里露馅
    broken = [ident for ident, raw in results.items() if not _decodable(raw)]
    if broken:
        problems.append(f"{len(broken)} 张图解不开（字节回到了错误的请求上？）：{broken[:4]}")
    # 二、不同表情本该是不同的图。65 项里有少量重复（雪碧图），所以只要求
    #     大多数不同就行，全同说明所有请求拿到的是同一份字节
    digests = {_digest(raw) for raw in results.values()}
    print(f"   去重后指纹 {len(digests)} 个 / {len(results)} 张", flush=True)
    if len(results) > 3 and len(digests) == 1:
        problems.append("并发取回来的图全是同一份字节 —— 应答与请求错配了")

    # 三、同一张图串行再取一次，必须与并发那次逐字节相同（服务端有降采样缓存）
    if results:
        ident = next(iter(results))
        again_reply = client.request("image", kind="sticker", ref=ident, px=px, need_image=True)
        again = client.image_bytes(again_reply)
        if again != results[ident]:
            problems.append(f"同一张图两次取回的字节不一致（{_digest(results[ident])} vs {_digest(again)}）")
        else:
            print(f"   复取校验一致（{_digest(again)}）", flush=True)


def _parse_sticker_thumbs(blob: bytes) -> tuple[int, dict[str, bytes], int]:
    """按**手表端那份 ArkTS 实现**的规则拆批量载荷。

    刻意在这里重写一遍而不是 import 服务端的打包代码：这个函数要回答的问题正是
    「服务端打的那包、手表解不解得开」。共用一份实现的话，两边一起错也照样通过。

    载荷格式（大端）：
        u16 张数 | 每张 { u16 id 长度 | u32 数据长度 | id(utf8) | 字节 }
    对应 BridgeLink.ets::parseStickerThumbs。

    返回 (声明的张数, id→字节, 解析结束时的游标)。
    """
    if len(blob) < 2:
        return 0, {}, 0
    count = int.from_bytes(blob[0:2], "big")
    items: dict[str, bytes] = {}
    at = 2
    for _ in range(count):
        if at + 6 > len(blob):
            break
        id_len = int.from_bytes(blob[at : at + 2], "big")
        data_len = int.from_bytes(blob[at + 2 : at + 6], "big")
        at += 6
        if at + id_len + data_len > len(blob):
            break
        ident = blob[at : at + id_len].decode("utf-8")
        at += id_len
        items[ident] = blob[at : at + data_len]
        at += data_len
    return count, items, at


def check_bulk_thumbs(
    client: WatchLikeClient, library: list[dict[str, Any]], px: int, problems: list[str]
) -> None:
    """整库缩略图一次往返（op=sticker_thumbs）。

    这条是「表情网格一格一格往外蹦」的正解：手表端把 70 次往返压成 1 次。
    契约有三条，缺一条手表那边就退化（拆不开就整屏没图，字节错位就显示别人的脸）：

      1. 载荷能被**手表那份解析器**逐字节拆完，声明的张数 = 实际装进来的张数，
         游标必须正好停在末尾 —— 末尾多出几个字节就说明格式和解析器对不上；
      2. 每一张都与单独 op=image 取回来的**逐字节相同**（服务端有降采样缓存，
         同 px 同图必然同字节）；错配的表现是「表情看起来是别人」；
      3. 每一张都真的能解码，且 id 落在库的集合里。
    """
    with_thumb = [item for item in library if item.get("has_thumb") and item.get("id")]
    if not with_thumb:
        problems.append("表情库里没有带缩略图的项，批量取图测不了")
        return
    _banner(f"整库缩略图一次往返（{len(with_thumb)} 张，px={px}）")

    started = time.perf_counter()
    reply = client.request("sticker_thumbs", px=px, need_image=True)
    blob = client.image_bytes(reply)
    elapsed = (time.perf_counter() - started) * 1000

    declared = int(reply.get("count") or 0)
    reported = int(reply.get("bytes") or 0)
    print(
        f"   服务端声明 {declared} 张 / {reported / 1024:.0f}KB，实收 {len(blob) / 1024:.0f}KB，"
        f"耗时 {elapsed:.0f}ms",
        flush=True,
    )
    if reported and reported != len(blob):
        problems.append(f"应答里报的字节数（{reported}）与实际收到的（{len(blob)}）不一致")
    if declared != len(with_thumb):
        problems.append(
            f"声明 {declared} 张，但库里带缩略图的有 {len(with_thumb)} 张"
            "（单张坏掉会被服务端跳过，所以这里只作提示）"
        )

    count, items, at = _parse_sticker_thumbs(blob)
    print(f"   解析出 {len(items)} 张，游标停在 {at}/{len(blob)}", flush=True)
    if count != declared:
        problems.append(f"载荷头部声明 {count} 张，JSON 应答里却是 {declared} 张")
    if len(items) != count:
        problems.append(f"载荷声明 {count} 张，实际只拆出 {len(items)} 张 —— 中途越界了")
    if at != len(blob):
        problems.append(
            f"解析完还剩 {len(blob) - at} 字节没被消费 —— 载荷格式与解析器不一致"
            "（手表那边会静默丢掉尾巴）"
        )

    known = {str(item["id"]) for item in with_thumb}
    unknown = sorted(set(items) - known)
    if unknown:
        problems.append(f"返回了库里没有的 id：{unknown[:4]}")

    broken = [ident for ident, raw in items.items() if not _decodable(raw)]
    if broken:
        problems.append(f"{len(broken)} 张图解不开：{broken[:4]}")

    # 抽样与逐张接口对照。全比一遍会把这 70 次往返又付回去，而抽到的样本里
    # 已经覆盖「静态小图 / 大动图 / 库末尾」三类差异最大的项。
    probes = [with_thumb[0], with_thumb[len(with_thumb) // 2], with_thumb[-1]]
    mismatched: list[str] = []
    for item in probes:
        ident = str(item["id"])
        if ident not in items:
            mismatched.append(f"{ident}（批量里没有这一项）")
            continue
        single = client.image_bytes(
            client.request("image", kind="sticker", ref=ident, px=px, need_image=True)
        )
        if items[ident] != single:
            mismatched.append(
                f"{ident}（批量 {_digest(items[ident])} vs 单张 {_digest(single)}）"
            )
    if mismatched:
        problems.append(f"批量与单张取回的字节不一致：{'；'.join(mismatched)}")
    else:
        print(f"   抽样 {len(probes)} 张与单张接口逐字节一致", flush=True)

    # 同一批再取一次必须完全一样（服务端有降采样缓存，不该每次重建）
    again = client.image_bytes(client.request("sticker_thumbs", px=px, need_image=True))
    if again != blob:
        problems.append("同一批缩略图两次取回的字节不一致 —— 缓存没命中或内容不确定")
    else:
        print(f"   复取校验一致（{_digest(blob)}）", flush=True)

    if declared:
        total = sum(len(raw) for raw in items.values())
        print(
            f"   平均 {total / max(1, len(items)) / 1024:.1f}KB/张；"
            f"逐张要 {len(items)} 次往返，批量 1 次",
            flush=True,
        )


def check_sync_stickers(client: WatchLikeClient, problems: list[str]) -> None:
    """打开表情面板时踢的那一脚（op=sync_stickers）。

    期望：**立刻**答复、并且能重复踢（节流由服务端自己拿捏，不该在接口上报错）。
    这个接口是「手机刚收藏的表情要马上出现在手表上」的全部依靠 —— 它要是慢或者
    报错，手表那边只会看到一次没用的往返。

    注意：真的踢成了会去独占浏览器十几秒（重扫表情面板），所以只验「答复快、
    格式对」，不去等刷新结果。
    """
    _banner("表情同步踢一脚（op=sync_stickers）")
    for attempt in (1, 2):
        started = time.perf_counter()
        reply = client.request("sync_stickers", timeout=20)
        elapsed = (time.perf_counter() - started) * 1000
        print(
            f"   第 {attempt} 次：ok={reply.get('ok')} started={reply.get('started')}，{elapsed:.0f}ms",
            flush=True,
        )
        if not reply.get("ok"):
            problems.append(f"sync_stickers 没有返回 ok：{reply}")
        if "started" not in reply:
            problems.append("sync_stickers 的应答里没有 started 字段（手表靠它判断有没有真的排上）")
        if elapsed > 5_000:
            problems.append(f"sync_stickers 花了 {elapsed:.0f}ms —— 必须立刻答复，不能等刷新跑完")


def check_long_lived(
    client: WatchLikeClient, library: list[dict[str, Any]], px: int, seconds: int, problems: list[str]
) -> None:
    """长连接 + 持续并发取图，跑够一次心跳周期。

    对手表来说这几乎就是最真实的负载：用户一边滑表情网格（十几张图同时要），
    链路上一边还有服务端每 15 秒一次的心跳，以及订阅推送。这一项专门要把
    「心跳 / 推送与图片应答并发发送」跑出来 —— 加密计数器一旦错位，表现就是
    连接直接断，而日志里不会有任何「解密失败」（对端只是单方面把连接关了）。

    跑满 20 秒是刻意选的：服务端 PING_INTERVAL_SECONDS = 15，短于这个数就
    可能一次心跳都碰不上，测了等于没测。
    """
    ids = [str(item["id"]) for item in library if item.get("has_thumb") and item.get("id")]
    if not ids:
        problems.append("没有缩略图，长连接压测跑不了")
        return

    _banner(f"长连接稳定性（{seconds}s，期间持续 8 路并发取图，必然跨过至少一次心跳）")
    started = time.perf_counter()
    rounds = 0
    failed_round = 0

    def fetch(ref: str) -> int:
        reply = client.request("image", kind="sticker", ref=ref, px=px, need_image=True)
        return len(client.image_bytes(reply))

    with ThreadPoolExecutor(max_workers=8) as pool:
        while time.perf_counter() - started < seconds:
            picks = [ids[(rounds * 5 + index) % len(ids)] for index in range(8)]
            try:
                sizes = list(pool.map(fetch, picks))
            except Exception as exc:  # noqa: BLE001
                failed_round = rounds + 1
                problems.append(
                    f"长连接压测在第 {rounds + 1} 轮失败（{exc.__class__.__name__}: {exc}）"
                    " —— 并发发送把链路搞坏了"
                )
                print(f"   第 {rounds + 1} 轮失败：{exc.__class__.__name__}（{exc}）", flush=True)
                break
            if any(size == 0 for size in sizes):
                problems.append(f"长连接压测第 {rounds + 1} 轮有 0 字节的图")
                break
            rounds += 1

    elapsed = time.perf_counter() - started
    if failed_round == 0:
        print(f"   {rounds} 轮 / {rounds * 8} 张图，耗时 {elapsed:.1f}s，链路一直可用", flush=True)
        # 压完再要一次状态：连接还能用，说明上面那些帧没有被心跳或推送插坏
        try:
            client.request("status")
            print("   压测后连接仍可用", flush=True)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"压测后连接不可用：{exc}")
            print(f"   压测后连接不可用：{exc}", flush=True)


def check_subscription(
    client: WatchLikeClient, friend: str, rev: str, quiet_seconds: int, problems: list[str]
) -> None:
    _banner(f"订阅基准：带 rev 订阅后 {quiet_seconds} 秒内不该有推送")
    reply = client.request("watch", name=friend, limit=30, active=True, rev=rev)
    if not reply.get("ok"):
        problems.append(f"watch 没有返回 ok：{reply}")
        return

    deadline = time.time() + quiet_seconds
    seen = 0
    while time.time() < deadline:
        try:
            push = client.pushes.get(timeout=0.5)
        except Exception:  # noqa: BLE001 - queue.Empty
            continue
        seen += 1
        print(f"   收到推送 rev={push.get('rev')}", flush=True)
    print(f"   {quiet_seconds} 秒内收到 {seen} 次推送", flush=True)
    if seen > 0:
        problems.append(
            "带 rev 订阅后立刻收到了推送 —— 手表刚加载完一屏就会再被刷一遍（服务端首推抑制失效）"
        )

    # 正向投递。不去找人发消息（那是副作用），而是用一个**故意过期**的 rev 订阅：
    # 服务端一比就知道基准不对，于是走一次完整的推送路径。这条链路一旦断了，
    # 手表的表现是「界面一切正常，但再也不会有新消息」—— 恰恰是最难被发现的那种坏。
    _banner("推送投递：拿一个过期 rev 订阅，应当收到一帧")
    stale_rev = f"stale-{int(time.time())}"
    reply = client.request("watch", name=friend, limit=30, active=True, rev=stale_rev)
    if not reply.get("ok"):
        problems.append(f"二次 watch 没有返回 ok：{reply}")
    else:
        deadline = time.time() + PUSH_WAIT_SECONDS
        got = 0
        while time.time() < deadline and got == 0:
            try:
                push = client.pushes.get(timeout=0.5)
            except Exception:  # noqa: BLE001 - queue.Empty
                continue
            got += 1
            messages = push.get("messages") or []
            print(f"   收到推送 rev={push.get('rev')} 消息 {len(messages)} 条", flush=True)
            if push.get("friend") != friend:
                problems.append(f"推送的 friend 不是订阅的会话：{push.get('friend')} != {friend}")
        if got == 0:
            problems.append(
                f"{PUSH_WAIT_SECONDS} 秒内没等到任何推送 —— 推送链路断了"
                "（手表会一直停在旧内容上，且不会有任何报错）"
            )
        else:
            print("   推送投递正常", flush=True)

    _banner("取消订阅")
    reply = client.request("unwatch")
    if not reply.get("ok"):
        problems.append(f"unwatch 没有返回 ok：{reply}")
    else:
        print("   unwatch ok", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="按手表端时序验证服务端契约")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--token", default=None, help="不填则读 artifacts/watch_token.txt")
    parser.add_argument("--crypto", choices=("gcm", "plain"), default="gcm")
    parser.add_argument("--friend", default=None)
    parser.add_argument("--sticker-px", type=int, default=72)
    parser.add_argument("--concurrency", type=int, default=12, help="并发取图的张数")
    parser.add_argument("--quiet", type=int, default=5, help="订阅后观察是否误推的秒数")
    parser.add_argument("--soak", type=int, default=20, help="长连接压测的秒数（0 表示跳过）")
    parser.add_argument(
        "--slow", type=int, default=0,
        help="额外跑「读不存在的会话」那条慢路径，值是要给的超时秒数（0 表示不跑）",
    )
    args = parser.parse_args()

    token = args.token
    if not token:
        token_file = PROJECT_ROOT / "artifacts/watch_token.txt"
        if token_file.is_file():
            token = token_file.read_text(encoding="utf-8").strip()
    if not token:
        print("没有令牌：用 --token 指定，或先启动一次服务生成 artifacts/watch_token.txt", flush=True)
        return 2

    problems: list[str] = []
    client = WatchLikeClient(args.host, args.port, token, crypto=args.crypto, timeout=60)
    try:
        client.connect()
    except Exception as exc:  # noqa: BLE001
        print(f"连接失败：{exc}", flush=True)
        return 2

    try:
        _banner("握手")
        print(f"   加密方式 {client.server_info.get('crypto')}", flush=True)

        status = client.request("status")
        friends = status.get("friends") or []
        library = status.get("watch_stickers") or []
        print(f"   ready={status.get('ready')} 好友 {len(friends)} 表情 {len(library)}", flush=True)
        friend = args.friend or (friends[0] if friends else "")
        if not friend:
            problems.append("没有可用的好友")
            return _report(problems)

        check_errors(client, problems)
        check_bulk_thumbs(client, library, args.sticker_px, problems)
        check_sync_stickers(client, problems)
        check_concurrent_images(client, library, args.sticker_px, args.concurrency, problems)

        if args.slow > 0:
            check_missing_friend(client, float(args.slow), problems)

        read = client.request("messages", name=friend, limit=30)
        rev = str(read.get("rev") or "")
        print(f"\n   会话「{friend}」rev={rev}", flush=True)
        check_long_lived(client, library, args.sticker_px, args.soak, problems)
        check_subscription(client, friend, rev, args.quiet, problems)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"未预期异常：{exc.__class__.__name__}: {exc}")
    finally:
        client.close()

    return _report(problems)


def _report(problems: list[str]) -> int:
    _banner("结论")
    if problems:
        for item in problems:
            print(f"   问题：{item}", flush=True)
        return 1
    print("   全部通过", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
