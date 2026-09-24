"""验证「控制台被冻住 → 服务卡死 → 敲键盘才恢复」这条链，以及修复是否真的成立。

做法是把控制台的冻结行为复刻出来（写操作阻塞到取消选择为止），然后做 A/B：

  A 对照组  直接用 StreamHandler 写这个流（= 现状）：
            一个线程卡在写入里、并且握着 logging 的 handler 锁，
            其它线程全部堆在锁上 —— 这就是手表看到的「服务卡死」。
  B 修复组  stdout 换成 bridge.console 的队列包装流：
            同样的冻结条件下，所有业务线程都跑完，一行都不阻塞。

同时验证：乱序/丢字、退出前收尾、以及 disable_quick_edit() 可安全调用。

用法：
    python scripts/console_freeze_check.py
"""

from __future__ import annotations

import io
import logging
import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bridge.console import _Channel, disable_quick_edit, install_nonblocking_output  # noqa: E402

# 冻结期间一个线程最多允许多少毫秒的写入耗时。队列包装之后应该是个位数，
# 留到 50ms 是给 GC 和线程调度留余量。
MAX_ALLOWED_WRITE_MS = 50.0
# 对照组要求至少卡这么久，才能说明这段场景真的把写入冻住了
MIN_CONTROL_BLOCK_MS = 300.0


class FrozenSink:
    """复刻 conhost 的选择态：冻结期间任何写入都会一直等下去。

    真实世界里等的是「取消选择」这个事件；这里用一个 Event 代替。
    """

    def __init__(self) -> None:
        self.frozen = threading.Event()
        self.entered = threading.Event()  # 有线程真的卡在写入里了
        self.encoding = "utf-8"
        self.errors = "strict"
        self.chunks: list[str] = []
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        if self.frozen.is_set():
            self.entered.set()
            while self.frozen.is_set():
                time.sleep(0.01)
        with self._lock:
            self.chunks.append(text)
        return len(text)

    def flush(self) -> None:
        return None

    def isatty(self) -> bool:
        return True

    def fileno(self) -> int:
        raise io.UnsupportedOperation("fileno")

    def text(self) -> str:
        with self._lock:
            return "".join(self.chunks)


def _probe_logger(tag: str, stream: object) -> logging.Logger:
    """一个独立的 logger，避免污染真实的 root 配置。"""
    logger = logging.getLogger(f"freeze-probe-{tag}")
    logger.handlers = [logging.StreamHandler(stream)]  # type: ignore[arg-type]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def _run_producers(logger: logging.Logger, threads: int, per_thread: int) -> tuple[list[threading.Thread], list[float]]:
    """多线程同时打日志，返回线程列表和每次调用的耗时（毫秒）。"""
    durations: list[float] = []
    durations_lock = threading.Lock()

    def worker() -> None:
        local: list[float] = []
        for index in range(per_thread):
            started = time.perf_counter()
            logger.info("探测日志 %d", index)
            local.append((time.perf_counter() - started) * 1000)
        with durations_lock:
            durations.extend(local)

    workers = [threading.Thread(target=worker, name=f"probe-{i}") for i in range(threads)]
    for worker_thread in workers:
        worker_thread.start()
    return workers, durations


def _alive(workers: list[threading.Thread]) -> int:
    return sum(1 for worker in workers if worker.is_alive())


def check_control_group(problems: list[str]) -> None:
    """A 组：不包装时，控制台冻结会让所有日志线程堵死。"""
    print("=" * 70)
    print("A 对照组：现状（StreamHandler 直接写控制台）")
    print("=" * 70)

    sink = FrozenSink()
    logger = _probe_logger("control", sink)

    sink.frozen.set()
    workers, _ = _run_producers(logger, threads=3, per_thread=200)
    time.sleep(MIN_CONTROL_BLOCK_MS / 1000 + 0.1)

    stuck = _alive(workers)
    print(f"   控制台冻结 0.4 秒后：{stuck}/3 个日志线程仍在阻塞", flush=True)
    print(f"   有线程卡在写入里：{'是' if sink.entered.is_set() else '否'}", flush=True)
    if stuck == 0:
        problems.append("对照组没有复现出阻塞 —— 这个测试没有验证能力，结论不可信")
    if not sink.entered.is_set():
        problems.append("对照组没有任何线程真正进入写入路径，冻结场景不成立")

    sink.frozen.clear()
    for worker in workers:
        worker.join(timeout=10)
    print(f"   解除冻结后全部结束：{'是' if _alive(workers) == 0 else '否'}", flush=True)
    print(flush=True)


def check_fixed_group(problems: list[str]) -> None:
    """B 组：换成队列包装流后，同样的冻结条件下业务线程一次都不阻塞。"""
    print("=" * 70)
    print("B 修复组：stdout 换成不阻塞的队列包装流")
    print("=" * 70)

    sink = FrozenSink()
    channel = _Channel(sink, "probe")
    channel.start()
    logger = _probe_logger("fixed", channel)

    sink.frozen.set()
    # 条数要压过队列容量（4096），否则「溢出丢弃」这条路径根本走不到
    workers, durations = _run_producers(logger, threads=4, per_thread=2000)
    for worker in workers:
        worker.join(timeout=30)
    # 冻结时长要超过 _STALL_NOTICE_SECONDS（1.0），否则不该提示、也测不到提示
    time.sleep(1.5)

    stuck = _alive(workers)
    worst = max(durations) if durations else 0.0
    total = len(durations)
    print(f"   冻结期间 {total} 次日志调用，最大单次耗时 {worst:.2f} ms", flush=True)
    print(f"   未结束的线程：{stuck}/4", flush=True)
    print(f"   有线程卡在写入里：{'是（落屏线程，符合设计）' if sink.entered.is_set() else '否'}", flush=True)

    if stuck != 0:
        problems.append(f"修复组仍有 {stuck} 个业务线程被阻塞")
    if worst > MAX_ALLOWED_WRITE_MS:
        problems.append(f"修复组出现 {worst:.1f}ms 的写入阻塞，超过 {MAX_ALLOWED_WRITE_MS}ms 上限")
    if not sink.entered.is_set():
        problems.append("修复组没有真的把控制台冻住，这轮等于没测")

    # 解冻：输出要继续流动，并且要打出一行「曾经被冻住」的提示
    sink.frozen.clear()
    time.sleep(0.8)
    channel.stop(timeout=5)
    text = sink.text()
    print(f"   解冻后落屏字符数：{len(text)}", flush=True)
    if "控制台被冻住" not in text:
        problems.append("解冻后没有打出「控制台被冻住」的提示，用户无从知道刚才发生了什么")
    if "丢弃" not in text:
        problems.append("队列溢出丢弃了输出，但没有向用户说明")
    print(f"   冻结提示：{'有' if '控制台被冻住' in text else '无'}", flush=True)
    print(f"   丢弃提示：{'有' if '丢弃' in text else '无'}", flush=True)
    print(f"   解冻后仍能输出：{'是' if len(text) > 0 else '否'}", flush=True)
    print(flush=True)


def check_order_and_flush(problems: list[str]) -> None:
    """安装到 sys.stdout 之后，内容不能错序、不能丢，退出前要能收尾。"""
    print("=" * 70)
    print("C 顺序与收尾：装到 sys.stdout 上之后")
    print("=" * 70)

    buffer = io.StringIO()
    saved = (sys.stdout, sys.stderr)
    sys.stdout = buffer
    sys.stderr = buffer
    try:
        assert install_nonblocking_output(), "install_nonblocking_output 没有生效"
        for index in range(500):
            print(f"line-{index:04d}", flush=True)
        sys.stdout.write("尾行\n")
        from bridge.console import flush_output

        flush_output(5)
        body = buffer.getvalue()
    finally:
        sys.stdout, sys.stderr = saved

    lines = [line for line in body.splitlines() if line.startswith("line-")]
    expected = [f"line-{index:04d}" for index in range(500)]
    print(f"   写入 500 行，落盘 {len(lines)} 行", flush=True)
    if lines != expected:
        problems.append("包装后输出错序或丢行")
    if "尾行" not in body:
        problems.append("退出前 flush_output 没有把最后一行写出去")
    print(f"   顺序完整：{'是' if lines == expected else '否'}", flush=True)
    print(f"   尾行已写出：{'是' if '尾行' in body else '否'}", flush=True)
    print(flush=True)


def check_quick_edit(problems: list[str]) -> None:
    print("=" * 70)
    print("D 快速编辑模式开关")
    print("=" * 70)
    try:
        status = disable_quick_edit()
        print(f"   disable_quick_edit() → {status}", flush=True)
        if not isinstance(status, str) or not status:
            problems.append("disable_quick_edit 没有返回可读状态")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"disable_quick_edit 抛异常：{exc!r}")
    print(flush=True)


def main() -> int:
    problems: list[str] = []
    check_control_group(problems)
    check_fixed_group(problems)
    check_order_and_flush(problems)
    check_quick_edit(problems)

    print("=" * 70)
    if problems:
        print("发现问题：")
        for item in problems:
            print(f"  ✗ {item}")
        return 1
    print("全部通过：")
    print("  ✓ 对照组复现了「一个线程卡在写控制台，其它线程全被 logging 锁挡住」")
    print("  ✓ 修复组在同条件下，全部业务线程零阻塞，输出降级为「可能丢几行」")
    print("  ✓ 输出不乱序、不丢尾行，冻结恢复后给出明确提示")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
