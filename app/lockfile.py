"""跨进程互斥锁：同一时刻只允许一个服务持有浏览器会话。

浏览器 profile 和抖音登录态都只有一份，两个服务同时开着会互相抢页面、
把对方正在读的会话搅乱。进程存活期间持有 `artifacts/run.lock`。

被强杀（关控制台窗口、结束进程）时 `finally` 不会执行、锁会残留，
表现为后续启动一律报「已有任务正在运行」—— 用 `scripts/clear_stale_lock.py` 清理。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class AlreadyRunningError(RuntimeError):
    pass


@contextmanager
def run_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise AlreadyRunningError(f"已有服务正在运行；如确认没有进程，请删除 {path}") from exc
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        os.close(descriptor)
        yield
    finally:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
