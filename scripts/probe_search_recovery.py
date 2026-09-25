"""端到端诊断：搜不到好友之后，会话列表会不会被污染。

用法：
    python scripts/probe_search_recovery.py

复现的场景：
    ① 读一次会话列表（基线）
    ② 读一个不存在的会话 —— 这一步会失败，并把页面留在搜索态
    ③ 再读一次列表 —— 清场不干净的话，这里会返回一份**错的**名单
    ④ 读一个真实好友 —— 确认那次失败没把后续操作带坏

为什么需要它：搜不到好友这条路径曾经有两个叠加的问题（2026-09-25 修）——
单次要 14.5 秒（13.2 秒花在「回退扫左侧 188 条会话行」上），而且失败后页面停在
搜索态，导致之后每一次读列表都拿到错名单（44 条、名字是杂串）。两套自检
（`search_path_check.py` / `setup_page_check.py`）都碰不到真实页面，所以留这个
脚本，改完搜索逻辑后跑一遍。

只读，不发送任何消息。输出 artifacts/probe_search_recovery.txt。
好友名只记长度，不落明文。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bridge.session import DouyinBridge  # noqa: E402

# 一个几乎不可能存在的名字。用固定值而不是随机串：日志里能一眼认出是探针跑的。
MISSING = "这个名字肯定不存在ZqX9Kk"


async def main() -> None:
    lines: list[str] = []

    def add(text: str = "") -> None:
        print(text, flush=True)
        lines.append(text)

    config_path = PROJECT_ROOT / "config.json"
    probe_friend = None
    if config_path.is_file():
        friends = [
            str(n).strip()
            for n in json.loads(config_path.read_text(encoding="utf-8")).get("friends", [])
            if str(n).strip()
        ]
        probe_friend = friends[0] if friends else None

    def shape(items: list[dict]) -> str:
        """只记可比较的形状，不落名字。"""
        lens = [len(str(i.get("name") or "")) for i in items[:14]]
        return f"{len(items)} 条  名字长度 {lens}"

    session = DouyinBridge()
    try:
        started = time.perf_counter()
        await session.start()
        add(f"会话已就绪（{time.perf_counter() - started:.1f}s）")
        add()

        add("① 基线：读会话列表")
        started = time.perf_counter()
        base = await session.read_conversations(limit=100)
        add(f"    {shape(base)}   （{time.perf_counter() - started:.2f}s）")
        add()

        add(f"② 读一个不存在的会话（{MISSING!r}）")
        started = time.perf_counter()
        try:
            await session.read_chat(MISSING, limit=5)
            add("    ！居然成功了，场景没复现")
        except Exception as exc:  # noqa: BLE001
            add(f"    失败({type(exc).__name__}) {exc}   （{time.perf_counter() - started:.2f}s）")
        add()

        add("③ 再读一次列表 —— 清场不干净的话，这里返回的是被污染的名单")
        started = time.perf_counter()
        after = await session.read_conversations(limit=100)
        add(f"    {shape(after)}   （{time.perf_counter() - started:.2f}s）")
        base_names = {str(i.get("name")) for i in base}
        after_names = {str(i.get("name")) for i in after}
        same = base_names == after_names
        add(f"    与基线的名字集合一致: {same}")
        add(f"    基线里少了: {len(base_names - after_names)} 个；多出来: {len(after_names - base_names)} 个")
        if not same:
            add("    ❌ 名单被污染了 —— 检查 _ensure_out_of_search / leave_search_mode")
        add()

        if probe_friend:
            add("④ 读一个真实好友 —— 确认没把后续操作带坏")
            started = time.perf_counter()
            try:
                data = await session.read_chat(probe_friend, limit=5)
                count = len(data.get("messages") or [])
                add(f"    成功，读到 {count} 条   （{time.perf_counter() - started:.2f}s）")
            except Exception as exc:  # noqa: BLE001
                add(f"    失败({type(exc).__name__}) {exc}   （{time.perf_counter() - started:.2f}s）")
            add()
        else:
            add("④ 跳过：config.json 里没有好友（那是运行数据，不进版本库）")
            add()

        status = await session.status()
        add(f"会话状态: ready={status.get('ready')} "
            f"current_chat={'<有>' if status.get('current_chat') else None}")
    finally:
        await session.stop()

    out = PROJECT_ROOT / "artifacts" / "probe_search_recovery.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    add(f"（已写入 {out}）")


if __name__ == "__main__":
    asyncio.run(main())
