"""清理残留的死锁文件 artifacts/run.lock。

运行被强制中断（关闭控制台窗口、结束进程）时，run_lock 的 finally 不会执行，
锁文件会残留，导致后续启动一律报「已有服务正在运行」。

本脚本读取锁里的 PID：
- 进程已不存在 -> 删除锁文件（死锁）
- 进程仍存活   -> 保留锁文件（服务确实在跑，锁是对的）

用法：python scripts/clear_stale_lock.py [锁文件路径，默认 artifacts/run.lock]
"""
from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

DEFAULT_LOCK = Path("artifacts/run.lock")
STILL_ACTIVE = 259
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return True  # 查询失败时保守认为是活的
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def main() -> int:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_LOCK
    if not path.is_file():
        print(f"[lock] 无锁文件，正常。 ({path})")
        return 0

    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
        pid = int(raw)
    except (OSError, ValueError):
        # 锁内容无法解析：无有效进程信息，视为死锁
        path.unlink(missing_ok=True)
        print(f"[lock] 锁内容无效，已删除：{path}")
        return 0

    if _pid_alive(pid):
        print(f"[lock] 检测到进程 {pid} 仍在运行，保留锁文件（服务确实在跑）。")
        return 0

    path.unlink(missing_ok=True)
    print(f"[lock] 进程 {pid} 已不存在，清理残留锁文件：{path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
