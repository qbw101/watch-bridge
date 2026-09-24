"""在真实控制台上验证 disable_quick_edit() 真的能翻转标志位。

自动化环境（WorkBuddy 的 Bash / PowerShell 工具）里子进程没有附加控制台，
所以 `disable_quick_edit()` 只会走到「没有附加控制台，跳过」这条分支 ——
那验证不了它到底有没有用。这里主动 AllocConsole() 造一个真的控制台出来，
读一次模式字、调一次禁用、再读回来看 QUICK_EDIT 位是不是真的从 1 变成 0。

窗口在 AllocConsole 之后立刻隐藏，不打扰用户桌面；结束时 FreeConsole 收掉。

用法：
    python scripts/console_mode_probe.py [输出文件]
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bridge.console import (  # noqa: E402
    ENABLE_EXTENDED_FLAGS,
    ENABLE_QUICK_EDIT_MODE,
    _console_input,
    _kernel32,
    disable_quick_edit,
)

SW_HIDE = 0


def _console_mode() -> int | None:
    """读当前控制台的输入模式字；没有控制台返回 None。"""
    loaded = _kernel32()
    if loaded is None:
        return None
    ctypes, wintypes, kernel32 = loaded
    handle, owned, mode = _console_input(ctypes, wintypes, kernel32)
    if owned and handle:
        kernel32.CloseHandle(handle)
    return mode


def _describe(mode: int | None) -> str:
    if mode is None:
        return "（没有控制台）"
    return (
        f"0x{mode:04X}  QuickEdit={'开' if mode & ENABLE_QUICK_EDIT_MODE else '关'}"
        f"  ExtendedFlags={'开' if mode & ENABLE_EXTENDED_FLAGS else '关'}"
    )


def main() -> int:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else PROJECT_ROOT / "artifacts" / "console_mode_probe.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # 结果一律写文件：AllocConsole 之后 sys.stdout 未必还指向有效流
    report: list[str] = []
    problems: list[str] = []
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32.AllocConsole.restype = wintypes.BOOL
    kernel32.GetConsoleWindow.restype = wintypes.HWND
    kernel32.FreeConsole.restype = wintypes.BOOL

    # 本进程可能已经挂着一个（没用的）控制台，先摘掉才能新建
    kernel32.FreeConsole()
    allocated = bool(kernel32.AllocConsole())
    hwnd = kernel32.GetConsoleWindow()
    if hwnd:
        # 立刻藏起来：只是借这个控制台读模式字，不该在桌面上闪一个窗口
        user32.ShowWindow(wintypes.HWND(hwnd), SW_HIDE)

    report.append(f"AllocConsole={allocated}  hwnd={hwnd}")
    if not allocated:
        problems.append("AllocConsole 失败，无法在真实控制台上验证")
        out_path.write_text("\n".join(report) + "\n", encoding="utf-8")
        return 1

    try:
        before = _console_mode()
        report.append(f"禁用之前：{_describe(before)}")
        if before is None:
            problems.append("新建的控制台读不到输入模式，环境不正常")
        elif not before & ENABLE_QUICK_EDIT_MODE:
            # 系统的 HKCU\\Console\\QuickEdit 是 0x1，新控制台理应默认开启
            report.append("注意：新控制台默认就没开快速编辑，翻转路径测不到")

        status = disable_quick_edit()
        report.append(f"disable_quick_edit() → {status}")

        after = _console_mode()
        report.append(f"禁用之后：{_describe(after)}")

        if after is None:
            problems.append("禁用之后读不到控制台模式")
        else:
            if after & ENABLE_QUICK_EDIT_MODE:
                problems.append("调用之后 QuickEdit 位仍然是 1 —— 禁用没生效")
            if not after & ENABLE_EXTENDED_FLAGS:
                problems.append("EXTENDED_FLAGS 被清掉了 —— 后续再改这个位会被系统忽略")
            if before is not None and not before & ENABLE_QUICK_EDIT_MODE:
                report.append("（本轮无法证明翻转，只能证明调用不破坏现有模式）")
            else:
                report.append("结论：QuickEdit 位已从 1 翻到 0")

        report.append(f"第二次调用 → {disable_quick_edit()}（应为「本来就是关闭的」）")
    finally:
        kernel32.FreeConsole()

    report.append("")
    report.append("发现问题：" if problems else "全部通过")
    for item in problems:
        report.append(f"  ✗ {item}")
    out_path.write_text("\n".join(report) + "\n", encoding="utf-8")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
