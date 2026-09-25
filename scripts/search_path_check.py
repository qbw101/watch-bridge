"""自检：搜不到好友时，不该再去扫左侧会话列表。

背景（2026-09-25 实测）：搜不到一个名字时，`DouyinChat._search_result` 原先不管
搜索有没有生效，都会「先在结果面板找一遍，再回退把左侧会话列表整个扫两遍」。
而搜索一旦生效，左侧列表就被面板盖住（实测首行可见性 False），那两遍扫描注定
一无所获 —— 整段 14.5 秒里有 13.2 秒耗在这里，其中 `[class*="ConversationItem"]`
一个 selector 就命中 122 行、每行再乘 5 个标题 selector、exact 与 group 各跑一遍。
而面板里当时早就写着「未搜索到相关内容」。

更坏的是搜索态下那些残留的会话行**不可见**，一旦被回退路径以 `force=True` 点下去，
就进入「点了没反应，再等满 15 秒 `_confirm_opened`」的路径。

修法是：用「搜索面板在不在」决定去哪一套 DOM 里找（见 `app/douyin.py`）。
本自检用假 page 把这条分派钉住 —— 不碰浏览器、不需要登录态、不需要抖音账号，
所以谁 clone 下来都能跑。目的是防止以后又被改回「总是回退扫会话行」。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.douyin import PANEL_CLOSE_GRACE_MS, DouyinChat, PageOperationError  # noqa: E402


PASSED = 0
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  [v] {name}")
    else:
        FAILED.append(name)
        print(f"  [x] {name}   {detail}")


class StubPage:
    """只实现 `DouyinChat` 真正用到的两个入口。"""

    def __init__(self, *, panel_visible: bool = True, evaluate_error: bool = False) -> None:
        self.panel_visible = panel_visible
        self.evaluate_error = evaluate_error
        self.evaluate_calls = 0
        self.slept_ms = 0

    async def evaluate(self, js, arg=None):  # noqa: ANN001
        self.evaluate_calls += 1
        if self.evaluate_error:
            raise RuntimeError("页面已经关了")
        return self.panel_visible

    async def wait_for_timeout(self, ms: int) -> None:
        self.slept_ms += ms
        # 真实睡一小口，让 `_confirm_opened` 的 while 循环不至于把 CPU 打满；
        # 宽限期是按真实时钟算的，所以这里睡得短不影响判定。
        await asyncio.sleep(min(ms, 20) / 1000)


def _stub_dispatch(chat: DouyinChat) -> list[str]:
    """把两条查找路径换成记录调用的小钩子。"""
    calls: list[str] = []

    async def fake_panel(name: str):
        calls.append("panel")
        return None

    async def fake_rows(name: str):
        calls.append("rows")
        return None

    chat._match_in_search_panel = fake_panel  # type: ignore[method-assign]
    chat._match_in_conversation_rows = fake_rows  # type: ignore[method-assign]
    return calls


async def main() -> int:
    print("=== 搜不到好友：该不该回退扫会话列表 ===")

    # ① 搜索面板在 —— 结论只在面板里，左侧列表被盖住，别去扫
    page = StubPage(panel_visible=True)
    chat = DouyinChat(page)
    calls = _stub_dispatch(chat)
    result = await chat._search_result("某人")
    check("面板可见时只在结果面板里找", calls == ["panel"], str(calls))
    check("面板可见时不回退扫左侧会话行", "rows" not in calls, str(calls))
    check("两条路都没命中时返回 None（= 确定性结论）", result is None, repr(result))

    # ② 面板不在 —— 搜索没生效（页面还停在会话列表），这时才该回退
    page2 = StubPage(panel_visible=False)
    chat2 = DouyinChat(page2)
    calls2 = _stub_dispatch(chat2)
    await chat2._search_result("某人")
    check("面板不在时才回退扫会话行", calls2 == ["rows"], str(calls2))

    # ③ evaluate 抛错（页面正在导航/关闭）不该把异常甩给调用方
    page3 = StubPage(evaluate_error=True)
    chat3 = DouyinChat(page3)
    visible = await chat3.search_mode_active()
    check("evaluate 抛错时当作「面板不在」，不向外抛", visible is False, repr(visible))

    # ④ 点了没生效时（面板一直挂着）要提前报错，别耗满 15 秒预算
    page4 = StubPage(panel_visible=True)
    chat4 = DouyinChat(page4, confirm_timeout_ms=15_000)

    async def always_error(name: str) -> PageOperationError:
        return PageOperationError("点击搜索结果后无法确认聊天已打开")

    chat4._chat_open_error = always_error  # type: ignore[method-assign]
    started = time.perf_counter()
    raised = False
    try:
        await chat4._confirm_opened("某人")
    except PageOperationError:
        raised = True
    elapsed = time.perf_counter() - started
    budget = PANEL_CLOSE_GRACE_MS / 1000 + 3.0
    check(
        f"点了没生效时提前报错（{elapsed:.1f}s，未耗满 15s 预算）",
        raised and elapsed < budget,
        f"raised={raised} elapsed={elapsed:.2f}s budget={budget:.1f}s",
    )

    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项：")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    print(f"全部通过（{PASSED} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
