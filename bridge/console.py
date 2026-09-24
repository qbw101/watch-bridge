"""Windows 控制台的两个坑，以及针对它们的处理。

## 坑一：快速编辑模式会把整个服务冻住

Windows 控制台（conhost）默认开着「快速编辑模式」。窗口里被点一下鼠标，控制台
就进入选择状态；此时**任何**想往这个控制台写数据的进程都会卡在 WriteFile 里，
直到按一下键盘（或再点一下）取消选择。

本来这只是「终端卡一下」，但 Python 的 logging 会给 handler 加锁：一个线程卡在
写控制台里，其它所有线程再调 `LOGGER.*` 就全部堆在锁上。于是浏览器的协程、推送
线程、连接线程一起停摆 —— 手表那边看到的就是「服务端跑到一半卡死了」，而敲两下
键盘立刻恢复。本项目里最常见的「卡死」就是这一条。

（实测环境：`HKCU\\Console\\QuickEdit = 0x1`、`ForceV2 = 0x1`，即默认开启、
且是会被冻住的那套 conhost 实现。）

`disable_quick_edit()` 把当前控制台的这个标志清掉。只影响本进程所在的那一个
控制台窗口，不改注册表、不需要管理员权限，窗口关掉即失效。

## 坑二：关掉标志也不能只靠它

Ctrl+M 会手动进入标记（Mark）选择态；在窗口属性里把快速编辑勾回来也一样；
Windows Terminal 里拖选同理。所以不能只依赖「开关没被打开」这个前提，还要让
**写控制台这件事本身不可能阻塞服务**：

`install_nonblocking_output()` 把 `sys.stdout` / `sys.stderr` 换成写进队列的
包装流，真正落盘/落屏由一条守护线程负责。控制台被冻住时，那条线程独自卡着、
队列满了就丢日志（并记账，恢复后打一行提示），而业务线程一次都不会被挡住 ——
服务照常收发，手表那边完全无感。

一句话：卡住的代价从「服务停摆」降级成「屏幕上少几行日志」。
"""

from __future__ import annotations

import atexit
import io
import queue
import sys
import threading
import time
from typing import Any, Callable

# ------------------------------------------------------------------ Windows 常量

STD_INPUT_HANDLE = -10
ENABLE_QUICK_EDIT_MODE = 0x0040
# 清快速编辑位时必须同时置上 EXTENDED_FLAGS，否则这个位会被系统忽略
ENABLE_EXTENDED_FLAGS = 0x0080

# 队列上限。正常一秒最多几十行，4096 段足够扛住好几分钟的阻塞；
# 满了就丢（见 _Channel.write），绝不反压业务线程。
_QUEUE_SIZE = 4096
# 一次最多合批多少段，避免把一大坨输出攒成一次超长写
_BATCH_MAX = 256
# 单批写超过这个秒数就认为是「控制台被冻住了」，恢复后提示一次
_STALL_NOTICE_SECONDS = 1.0

# 队列里的停机哨兵
_STOP = object()


# CreateFileW 用到的常量（就这几个，不值得再引依赖）
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_OPEN_EXISTING = 3


def _kernel32() -> tuple[Any, Any, Any] | None:
    """返回 (ctypes, wintypes, kernel32)，argtypes 已配好；不可用时返回 None。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
        kernel32.GetStdHandle.restype = wintypes.HANDLE
        kernel32.GetConsoleMode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetConsoleMode.restype = wintypes.BOOL
        kernel32.SetConsoleMode.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.SetConsoleMode.restype = wintypes.BOOL
        kernel32.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel32.CreateFileW.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
    except Exception:  # noqa: BLE001
        return None
    return ctypes, wintypes, kernel32


def _console_input(ctypes: Any, wintypes: Any, kernel32: Any) -> tuple[int, bool, int | None]:
    """拿到控制台输入句柄，并顺带读一次它的模式字。

    返回 `(句柄, 是不是我们自己开的（要负责 CloseHandle）, 模式字或 None)`。

    为什么不能只用 GetStdHandle：进程的 stdin 一旦被重定向（`< nul`、管道、
    某些启动器），`GetStdHandle(STD_INPUT_HANDLE)` 拿到的就不是控制台句柄，
    可进程其实照样挂着一个真的控制台。那种情况下直接打开 `CONIN$` 才拿得到 ——
    而控制台模式是挂在**输入缓冲区**上的，从 CONIN$ 改和从 stdin 改是同一件事。
    """
    handle = kernel32.GetStdHandle(STD_INPUT_HANDLE)
    if handle:
        mode = wintypes.DWORD()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return handle, False, mode.value

    opened = kernel32.CreateFileW(
        "CONIN$",
        _GENERIC_READ | _GENERIC_WRITE,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE,
        None,
        _OPEN_EXISTING,
        0,
        None,
    )
    if not opened:
        return 0, False, None
    mode = wintypes.DWORD()
    if not kernel32.GetConsoleMode(opened, ctypes.byref(mode)):
        kernel32.CloseHandle(opened)
        return 0, False, None
    return opened, True, mode.value


def disable_quick_edit() -> str:
    """关掉当前控制台的快速编辑模式。

    返回一句可以直接打给用户看的状态说明。任何情况下都不抛异常 ——
    这个功能失败不该阻止服务启动，但失败原因要能看得见。
    """
    loaded = _kernel32()
    if loaded is None:
        return "非 Windows 平台或控制台接口不可用，跳过"
    ctypes, wintypes, kernel32 = loaded

    handle, owned, mode_value = _console_input(ctypes, wintypes, kernel32)
    if not handle or mode_value is None:
        return "没有附加控制台，跳过"
    try:
        if not mode_value & ENABLE_QUICK_EDIT_MODE:
            return "本来就是关闭的"
        new_mode = (mode_value | ENABLE_EXTENDED_FLAGS) & ~ENABLE_QUICK_EDIT_MODE
        if not kernel32.SetConsoleMode(handle, new_mode):
            return f"设置失败（WinError {ctypes.get_last_error()}）"
        return "已关闭"
    finally:
        if owned:
            kernel32.CloseHandle(handle)


# ------------------------------------------------------------ 不阻塞的输出通道


class _Channel(io.TextIOBase):
    """包住真实输出流：write 只是入队，落屏由单独一条线程做。

    调用方（业务线程、logging）永远不会因为对端写不动而卡住。
    """

    def __init__(self, target: Any, label: str) -> None:
        super().__init__()
        self._target = target
        self._label = label
        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=_QUEUE_SIZE)
        self._counter_lock = threading.Lock()
        self._dropped = 0
        self._on_stall: Callable[[float, int], None] | None = None
        self._thread = threading.Thread(target=self._drain, name=f"console-{label}", daemon=True)

    # ---------------------------------------------------------- 生命周期

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """把队列里剩下的写完再收线程；写不动就超时放弃。"""
        if not self._thread.is_alive():
            return
        try:
            self._queue.put_nowait(_STOP)
        except queue.Full:
            # 队列满说明已经堆了一大堆，直接放弃剩下的，不值得为日志卡住退出流程
            return
        self._thread.join(timeout)

    def set_stall_callback(self, callback: Callable[[float, int], None]) -> None:
        """控制台真的被冻过时回调（秒数, 丢弃段数），用来记进日志文件。"""
        self._on_stall = callback

    # ---------------------------------------------------------- 写入口

    def write(self, text: str) -> int:
        if not text:
            return 0
        try:
            self._queue.put_nowait(text)
        except queue.Full:
            with self._counter_lock:
                self._dropped += 1
        return len(text)

    def flush(self) -> None:
        # 入队即“已提交”。真正落屏由 drain 线程尽快做，这里没有可等待的状态，
        # 也**不能**等 —— 调用方可能正是想让输出可见，但一等就又把自己卡住了。
        return None

    def close(self) -> None:
        # 故意不清 closed 标志：sys.stdout 被 close 掉会让后续 print 全部报错，
        # 而这个包装流在进程存活期间应该一直可用。
        return None

    def writable(self) -> bool:
        return True

    def readable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return False

    def isatty(self) -> bool:
        return bool(getattr(self._target, "isatty", lambda: False)())

    def fileno(self) -> int:
        return self._target.fileno()

    @property
    def encoding(self) -> str:
        return getattr(self._target, "encoding", None) or "utf-8"

    @property
    def errors(self) -> str:
        return getattr(self._target, "errors", None) or "strict"

    def __getattr__(self, name: str) -> Any:
        # 没定义的属性（name / buffer / newlines …）一律透传给真实流
        try:
            target = self.__dict__["_target"]
        except KeyError:
            raise AttributeError(name) from None
        return getattr(target, name)

    # ---------------------------------------------------------- 落屏线程

    def _drain(self) -> None:
        while True:
            item = self._queue.get()
            if item is _STOP:
                return
            batch = [item]
            while len(batch) < _BATCH_MAX:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is _STOP:
                    self._write(batch)
                    return
                batch.append(nxt)
            self._write(batch)

    def _write(self, batch: list[str]) -> None:
        started = time.monotonic()
        self._emit("".join(batch))
        spent = time.monotonic() - started
        dropped = self._take_dropped()
        if spent < _STALL_NOTICE_SECONDS and not dropped:
            return

        notes: list[str] = []
        if spent >= _STALL_NOTICE_SECONDS:
            notes.append(f"控制台被冻住 {spent:.1f} 秒（服务未中断，按一下键盘可取消窗口里的选择）")
        if dropped:
            notes.append(f"期间丢弃 {dropped} 段输出")
        self._emit("[" + "，".join(notes) + "]\n")

        callback = self._on_stall
        if callback is not None:
            try:
                callback(spent, dropped)
            except Exception:  # noqa: BLE001 - 回调是记日志用的，不能反过来炸掉这条线程
                pass

    def _emit(self, text: str) -> None:
        """真正写一次。这条线程是唯一可能被控制台卡住的地方，且绝不能死。"""
        if not text:
            return
        try:
            self._target.write(text)
            self._target.flush()
            return
        except UnicodeEncodeError:
            # 控制台码页带不动的字符（比如没切 65001 时的中文）
            encoding = getattr(self._target, "encoding", None) or "utf-8"
            try:
                self._target.write(text.encode(encoding, "replace").decode(encoding, "replace"))
                self._target.flush()
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            # 流已经坏了（管道对端退出之类）。放弃这一批，但线程继续活着，
            # 这样后面控制台恢复了还能继续输出。
            pass

    def _take_dropped(self) -> int:
        with self._counter_lock:
            dropped = self._dropped
            self._dropped = 0
        return dropped


_installed: list[_Channel] = []


def install_nonblocking_output() -> bool:
    """把 sys.stdout / sys.stderr 换成不阻塞业务的队列流。

    重复调用只生效一次。返回是否有改动。
    """
    changed = False
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None or isinstance(stream, _Channel):
            continue
        channel = _Channel(stream, name)
        channel.start()
        _installed.append(channel)
        setattr(sys, name, channel)
        changed = True
    if changed:
        # 兜底：正常退出路径会在 main() 里显式收尾，这里管的是异常退出
        atexit.register(flush_output, 1.0)
    return changed


def channels() -> list[_Channel]:
    return list(_installed)


def flush_output(timeout: float = 2.0) -> None:
    """等队列里剩余的输出去写完。退出前调用。"""
    for channel in channels():
        channel.stop(timeout)


__all__ = [
    "disable_quick_edit",
    "install_nonblocking_output",
    "flush_output",
    "channels",
]
