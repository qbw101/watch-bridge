"""配置页的浏览器级走查（Playwright）：抖音账号 + 手表端好友两块。

`setup_page_check.py` 打的是 HTTP 接口，验不出「点一下按钮页面会不会乱」。
这个脚本专门补那一层：真开一个 Chromium 去点、去勾、去读，然后回头看
落盘的文件对不对 —— 覆盖的都是改这两块 HTML 时最容易碰坏的地方：

  A. 初始渲染：脚本自带的示例名单能不能把 7 位好友正确铺成勾选列表；
  B. 取消勾一位再保存：落盘少一位，而**文件里不该凭空多出其它键**；
  C. 点「从抖音读取好友」：读到的会话能合进列表 —— 已勾的还勾着、没勾的补进来
     且不勾、已启用但这轮没读到的会被标「本次未读到」；
  D. 勾上「读到但没勾」的那位再保存：新名字进名单；
  E. 读取进行中的等待态：按钮禁用、冒出「取消」、提示条说清在滚列表；
  F. 点「取消」：合作式停下，名单与文件都不变；
  G. 原样保存一遍，文件逐字节不变（浮点化 / 键重排 / 丢键都在这一步现形）；
  H. 一位都不勾时保存被服务端拦下，页面给出错误且**文件分毫未动**；
  I. 抖音账号：昵称 / 徽章 / 按钮文案跟着凭证状态走；
  J. 退出登录必须点两下才生效（删凭证不可逆），删完徽章与按钮一起复位；
  K. 扫码登录的等待态：按钮变「等待扫码…」、多出一个「取消」、提示条说清在等什么；
  L. 取消之后回到可登录状态；真扫上（假命令写出凭证）后徽章回到「已登录」并提醒重启。

跑法：
    python scripts/setup_page_ui_check.py

不碰抖音、不碰 8787。名单和凭证都是脚本自己造的假数据 —— 项目根那份
`config.json` / `storage-state.json` **一概不读**，所有会写的东西都落在临时
目录里，凭证也是现场造的假 cookie，「读好友」用假 reader。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bridge import credentials, friend_scan, server_config, setup_web, watch_friends  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

FAILED: list[str] = []

# 1×1 透明 GIF，用 data: URI 当假头像 —— 不产生任何网络请求。
TINY_GIF = ("data:image/gif;base64,"
            "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("  [v] " if ok else "  [x] ") + name + (f" —— {detail}" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


# 「从抖音读取好友」的假结果：走查用，不碰浏览器。
# 刻意做成「不齐」的名单，好让三种状态一次看清：
#   · 已启用的多数人原样对得上（小明、阿华 3.12 …）；
#   · 比已启用的多一位「新来的人」—— 演示「读到但没勾」；
#   · 故意漏掉是启用的「小陈」—— 演示「已用但这次没读到」。
FAKE_ROSTER = [
    {"name": "小明", "preview": "晚安"},
    {"name": "阿华 3.12", "preview": "在吗"},
    {"name": "小林 6.09", "preview": "[图片]"},
    {"name": "小周 11.05", "preview": "明天见"},
    {"name": "小吴", "preview": "哈哈"},
    {"name": "小郑", "preview": "到了"},
    {"name": "新来的人", "preview": "你好呀"},
]

# 假 reader 的开关：默认立刻返回（快，好验「读到了什么」）；打开后慢慢跑，
# 好把页面停在「正在读取」那一小段，验按钮态和「取消」。
SLOW = {"on": False}


def scan_reader(should_stop):
    """假的「读抖音会话列表」：不碰浏览器。

    慢模式里每 0.1 秒看一次 `should_stop` —— 这是真 reader 的合作式取消约定，
    照抄一份才能验出「点取消会真的停下、且名单不变」。
    """
    if not SLOW["on"]:
        return FAKE_ROSTER
    for _ in range(80):          # 最多 8 秒，够走查观察
        if should_stop():
            return []
        time.sleep(0.1)
    return FAKE_ROSTER


def fake_login_argv(mode: dict, seed: Path, target: Path) -> list[str]:
    """假登录命令：不碰浏览器、不碰抖音，只按剧本睡觉 / 写文件 / 退出。

    `ok` 那条会把 seed 复制成凭证 —— 复制而不是「等测试那边写」是为了不引入
    时序竞争（谁先谁后都不影响判定）。
    """
    body = {
        "hang": "import time\ntime.sleep(30)\n",
        "ok": (
            "import shutil\n"
            f"shutil.copy(r'{seed}', r'{target}')\n"
            "print('登录状态已保存到 storage-state.json')\n"
        ),
        "fail": "import sys\nprint('等待 300 秒后仍未检测到登录成功')\nsys.exit(3)\n",
    }[mode["value"]]
    return [sys.executable, "-c", body]


def write_credential(path: Path, *, nickname: str = "示例账号") -> None:
    """造一份 storage-state.json（假 cookie，只为让页面有东西可显示）。

    头像用内联的 1×1 GIF：页面会把 `avatar` 塞进 `<img>`，用真域名的话
    headless Chromium 会当场报 `ERR_NAME_NOT_RESOLVED`，控制台就不干净了。
    """
    payload = {
        "cookies": [{"name": "login_time", "value": "1789477366888",
                     "domain": ".douyin.com", "path": "/"}],
        "origins": [{
            "origin": "https://www.douyin.com",
            "localStorage": [
                {"name": "user_info", "value": json.dumps({
                    "uid": "MS4wLjABAAAA9Xk2pQ7vR3mL5tZ8wY1cB4nD6eF0gH2jK4mN6pQ8rS0tU2vW0123",
                    "nickname": nickname,
                    "avatarUrl": TINY_GIF,
                }, ensure_ascii=False)},
            ],
        }],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


with tempfile.TemporaryDirectory(prefix="watch-ui-exercise-") as tmp:
    cfg_path = Path(tmp) / "server_config.json"
    config, _ = server_config.load(cfg_path)
    config.token = "a1b2c3d4"
    config.setup_done = True
    config.save()

    task_path = Path(tmp) / "config.json"
    # 名单用本脚本自带的示例，**不读项目根那份 config.json** ——
    # 那是运行数据、不进版本库，别人 clone 下来根本没有，测试会因此跑不起来
    # （而且会把本机的好友名字带进测试断言里）。
    # 这份示例刻意排成「比 FAKE_ROSTER 少一位『小陈』、多一位『新来的人』」，
    # 好把「已用但没读到」「读到但没勾」两种行状态一次验到。位数为 7，下面的
    # 断言按这个数写。
    base_text = json.dumps({
        "friends": ["小明", "阿华 3.12", "小林 6.09", "小陈", "小周 11.05", "小吴", "小郑"],
    }, ensure_ascii=False, indent=2)
    task_path.write_text(base_text, encoding="utf-8")
    base_doc = json.loads(base_text)   # 原文件的键序，B 步要拿它对

    # 凭证有两份：seed 是「扫出来的那份」永远留着，cred 会被退出登录删掉。
    cred_path = Path(tmp) / "storage-state.json"
    seed_path = Path(tmp) / "cred-seed.json"
    write_credential(seed_path)
    write_credential(cred_path)
    mode = {"value": "hang"}
    account = credentials.AccountStore(
        cred_path, project_root=ROOT,
        command_factory=lambda _t: fake_login_argv(mode, seed_path, cred_path),
    )

    app = setup_web.SetupWeb(config, friend_store=watch_friends.FriendStore(task_path),
                             account_store=account,
                             scanner=friend_scan.FriendScanner(scan_reader),
                             info_provider=lambda: {"lan": ["192.168.1.80"], "ipv6": [],
                                                    "service": {"backend_ready": True, "sessions": 1}})
    # 端口传 0：让系统分临时端口。绝不能从 8788 试起 —— 那上面可能正跑着用户自己的
    # 服务，撞上去会把对方的实例当成自己的用（真踩过，写坏了真实配置）。
    url = app.start(0)
    probe = app.state()
    if probe.get("config_path") != str(cfg_path):
        print(f"× 这个实例对不上（config_path={probe.get('config_path')}）—— 停手。")
        app.stop()
        raise SystemExit(1)   # 本脚本是平铺的（没包 main），这里不能用 return
    errors: list[str] = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1000, "height": 1400})
            page.on("console", lambda m: errors.append(f"{m.type}: {m.text}")
                    if m.type in ("error", "warning") else None)
            page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))
            # 「重新扫码登录」会弹一个 window.confirm 拦一下；headless 下默认是「取消」，
            # 不接这个钩子就会静默什么都不发生。
            page.on("dialog", lambda d: d.accept())
            page.goto(url, wait_until="load")
            page.wait_for_timeout(2000)

            print("A. 初始渲染：7 位好友、全勾、文件已接上")
            check("第 3 块露出来了", page.is_visible("#friendCard"))
            check("徽章说这份文件已在用", page.inner_text("#friendBadge") == "已在用",
                  page.inner_text("#friendBadge"))
            check("铺出 7 行", page.eval_on_selector_all("#friendList .friendrow", "e => e.length") == 7)
            check("7 行全勾着",
                  page.eval_on_selector_all("#friendList .friendrow:not(.off)", "e => e.length") == 7)
            check("已启用计数是 7", page.inner_text("#friendCount") == "7", page.inner_text("#friendCount"))
            names = page.eval_on_selector_all("#friendList .friendrow", "e => e.map(x => x.dataset.name)")
            check("名字按文件里的顺序铺", names[:2] == ["小明", "阿华 3.12"], str(names[:3]))
            check("还没读过时按钮是「从抖音读取好友」",
                  page.inner_text("#btnScan") == "从抖音读取好友", page.inner_text("#btnScan"))
            check("还没读过时不显示「取消」", not page.is_visible("#btnScanCancel"))
            check("提示条说清还没读过", "尚未读取" in page.inner_text("#scanNote"),
                  page.inner_text("#scanNote")[:140])

            print("B. 取消勾一位再保存：名单少一位，文件里不该多出别的键")
            # 一律用 click 而不是 check/uncheck：勾一下整块列表会重绘（innerHTML 换掉），
            # Playwright 的 check/uncheck 会拿被换掉的旧节点去复核状态，于是干等到超时。
            page.click('#friendList .friendrow[data-name="小吴"] input')
            page.wait_for_timeout(300)
            check("那一行退成未勾", page.eval_on_selector('#friendList .friendrow[data-name="小吴"]',
                                                      "e => e.classList.contains('off')"))
            check("计数跟着变成 6", page.inner_text("#friendCount") == "6", page.inner_text("#friendCount"))
            page.click("#btnSave")
            page.wait_for_timeout(1600)
            saved = json.loads(task_path.read_text(encoding="utf-8"))
            check("落盘 6 位", len(saved.get("friends", [])) == 6,
                  json.dumps(saved.get("friends"), ensure_ascii=False))
            check("「小吴」真没了", "小吴" not in saved.get("friends", []))
            check("键的集合与顺序没变（写回是在原文上改一个键，不是重排）",
                  list(saved) == list(base_doc), str(list(saved)))
            check("只写了 friends，没凭空多出别的键", [k for k in saved if k != "friends"] == [],
                  json.dumps([k for k in saved if k != "friends"], ensure_ascii=False))
            check("留了一份 .bak", task_path.with_name("config.json.bak").is_file())
            check("保存后按钮回到「保存全部」", page.inner_text("#btnSave") == "保存全部",
                  page.inner_text("#btnSave"))

            print("C. 点「从抖音读取好友」：读到的会话合进列表")
            page.click("#btnScan")
            page.wait_for_timeout(3000)   # 要等一轮 2 秒轮询把「读完了」取回来
            check("列表多出「读到但没勾」的两位",
                  page.eval_on_selector_all("#friendList .friendrow", "e => e.length") == 8,
                  str(page.eval_on_selector_all("#friendList .friendrow", "e => e.map(x => x.dataset.name)")))
            check("没勾的正好是「小吴」和「新来的人」",
                  sorted(page.eval_on_selector_all("#friendList .friendrow.off",
                                                   "e => e.map(x => x.dataset.name)"))
                  == sorted(["小吴", "新来的人"]),
                  str(page.eval_on_selector_all("#friendList .friendrow.off", "e => e.map(x => x.dataset.name)")))
            check("「新来的人」默认不勾",
                  not page.eval_on_selector('#friendList .friendrow[data-name="新来的人"] input',
                                            "e => e.checked"))
            check("这一轮没读到的「小陈」被标出来",
                  page.eval_on_selector_all("#friendList .friendrow.missing", "e => e.map(x => x.dataset.name)")
                  == ["小陈"],
                  str(page.eval_on_selector_all("#friendList .friendrow.missing", "e => e.map(x => x.dataset.name)")))
            check("那行带「这次没读到」的标签",
                  "本次未读到" in page.inner_text('#friendList .friendrow[data-name="小陈"]'),
                  page.inner_text('#friendList .friendrow[data-name="小陈"]'))
            check("已勾的还是 6 位（读取不改变勾选）", page.inner_text("#friendCount") == "6",
                  page.inner_text("#friendCount"))
            check("提示条报了这一轮读到几个会话",
                  "读取到 7 个会话" in page.inner_text("#scanNote"), page.inner_text("#scanNote")[:160])
            check("按钮改成「重新读取」", page.inner_text("#btnScan") == "重新读取", page.inner_text("#btnScan"))

            print("D. 勾上「新来的人」再保存：新名字进名单")
            page.click('#friendList .friendrow[data-name="新来的人"] input')
            page.wait_for_timeout(300)
            check("计数变成 7", page.inner_text("#friendCount") == "7", page.inner_text("#friendCount"))
            page.click("#btnSave")
            page.wait_for_timeout(1600)
            saved = json.loads(task_path.read_text(encoding="utf-8"))
            check("落盘 7 位", len(saved.get("friends", [])) == 7,
                  json.dumps(saved.get("friends"), ensure_ascii=False))
            check("新名字进去了", "新来的人" in saved.get("friends", []))
            check("取消掉的那位没被捎回来", "小吴" not in saved.get("friends", []))
            check("依旧只有 friends 一个键", list(saved) == ["friends"], str(list(saved)))

            print("E. 读取进行中的等待态")
            SLOW["on"] = True
            page.click("#btnScan")
            page.wait_for_timeout(900)
            check("按钮变禁用（不给重复点）", page.is_disabled("#btnScan"))
            check("冒出「取消」", page.is_visible("#btnScanCancel"))
            check("提示条说清在滚列表", "滚动" in page.inner_text("#scanNote"), page.inner_text("#scanNote")[:160])

            print("F. 点「取消」：合作式停下，名单与文件都不变")
            before = task_path.read_text(encoding="utf-8")
            page.click("#btnScanCancel")
            page.wait_for_timeout(2800)   # 取消是合作式的，要等当前那轮跑完 + 一轮轮询
            check("「取消」收起来了", not page.is_visible("#btnScanCancel"))
            check("提示条说已取消、名单没变",
                  "名单未发生变化" in page.inner_text("#scanNote"), page.inner_text("#scanNote")[:160])
            check("上一轮读到的名单还在（取消不清空）",
                  page.eval_on_selector_all("#friendList .friendrow", "e => e.length") == 8,
                  str(page.eval_on_selector_all("#friendList .friendrow", "e => e.map(x => x.dataset.name)")))
            check("按钮回到可点", not page.is_disabled("#btnScan"))
            check("文件没被动过", task_path.read_text(encoding="utf-8") == before)
            SLOW["on"] = False

            print("G. 原样保存一遍，文件不该变形")
            original = task_path.read_text(encoding="utf-8")
            # 取消勾一位又勾回来：模型里的值没变，但页面认为「动过」，于是会走一次保存
            page.click('#friendList .friendrow[data-name="新来的人"] input')
            page.wait_for_timeout(300)
            page.click('#friendList .friendrow[data-name="新来的人"] input')
            page.wait_for_timeout(300)
            page.click("#btnSave")
            page.wait_for_timeout(1600)
            check("落盘内容和上一版逐字节一致（不凭空多出小数点 / 不重排键）",
                  task_path.read_text(encoding="utf-8") == original,
                  task_path.read_text(encoding="utf-8")[:200])

            print("H. 一位都不勾 → 服务端拦下，文件分毫未动")
            for _ in range(20):
                if page.eval_on_selector_all("#friendList .friendrow:not(.off)", "e => e.length") == 0:
                    break
                page.click("#friendList .friendrow:not(.off) input")   # 点第一个还勾着的，取消它
                page.wait_for_timeout(150)
            check("全取消了", page.inner_text("#friendCount") == "0", page.inner_text("#friendCount"))
            before = task_path.read_text(encoding="utf-8")
            page.click("#btnSave")
            page.wait_for_timeout(1600)
            check("页面上给出了错误",
                  page.is_visible("#errStrip") and "至少保留一位" in page.inner_text("#errStrip"),
                  page.inner_text("#errStrip")[:180])
            check("文件没被写坏", task_path.read_text(encoding="utf-8") == before)
            page.click("#btnReload")
            page.wait_for_timeout(1600)
            check("「重新读取」把页面拉回文件里那份名单",
                  page.inner_text("#friendCount") == "7", page.inner_text("#friendCount"))

            print("I. 抖音账号：卡片跟着凭证状态走")
            check("显示昵称", page.inner_text("#acctName") == "示例账号", page.inner_text("#acctName"))
            check("徽章是「已登录」", page.inner_text("#acctBadge") == "已登录", page.inner_text("#acctBadge"))
            check("头像显示出来了", page.is_visible("#acctAvatar"))
            check("昵称首字母也在（头像挂了时的兜底）", page.inner_text("#acctInitial") == "示",
                  page.inner_text("#acctInitial"))
            meta = page.inner_text("#acctMeta")
            check("一行里能看到登录时间与打码 uid",
                  "登录于" in meta and "uid" in meta and "…" in meta, meta)
            check("已有凭证时按钮写成「重新扫码登录」",
                  page.inner_text("#btnLogin") == "重新扫码登录", page.inner_text("#btnLogin"))
            check("当前状态那一行也认得出是谁",
                  "示例账号" in page.inner_text("#statusList"), page.inner_text("#statusList")[:200])

            print("J. 退出登录：两下确认，删完卡片与按钮一起复位")
            page.click("#btnLogout")
            page.wait_for_timeout(300)
            check("第一下不算数，按钮变成确认",
                  "再次点击" in page.inner_text("#btnLogout"), page.inner_text("#btnLogout"))
            check("凭证还在（第一下没删）", cred_path.is_file())
            page.click("#btnLogout")
            page.wait_for_timeout(900)
            check("凭证文件真删了", not cred_path.is_file())
            check("徽章跟着变成「没登录」", page.inner_text("#acctBadge") == "未登录",
                  page.inner_text("#acctBadge"))
            check("昵称位改说「还没登录」", page.inner_text("#acctName") == "尚未登录",
                  page.inner_text("#acctName"))
            check("按钮立刻回到「扫码登录」（不等下一轮轮询）",
                  page.inner_text("#btnLogin") == "扫码登录", page.inner_text("#btnLogin"))
            check("退出登录按钮自己禁用了", page.is_disabled("#btnLogout"))
            check("提示条提醒要重启才真登出",
                  "重启" in page.inner_text("#loginStrip"), page.inner_text("#loginStrip")[:120])

            print("K. 扫码登录：等待态与取消")
            mode["value"] = "hang"          # 假命令挂住 30 秒，好观察「等待扫码」
            page.click("#btnLogin")
            page.wait_for_timeout(900)
            check("按钮变成「等待扫码…」", page.inner_text("#btnLogin") == "等待扫码…",
                  page.inner_text("#btnLogin"))
            check("多出一个「取消本次扫码」", page.is_visible("#btnCancel"))
            check("等待期间不让重复点登录", page.is_disabled("#btnLogin"))
            check("提示条说清在等扫码",
                  "扫码" in page.inner_text("#loginStrip"), page.inner_text("#loginStrip")[:140])
            page.click("#btnCancel")
            page.wait_for_timeout(900)
            check("取消后「取消」按钮收起来", not page.is_visible("#btnCancel"))
            check("提示条说取消了", "取消" in page.inner_text("#loginStrip"),
                  page.inner_text("#loginStrip")[:140])
            check("取消之后按钮能再点，且还叫「扫码登录」",
                  not page.is_disabled("#btnLogin") and page.inner_text("#btnLogin") == "扫码登录",
                  page.inner_text("#btnLogin"))

            print("L. 扫码登录：真扫上（假命令写出凭证）")
            mode["value"] = "ok"
            page.click("#btnLogin")
            page.wait_for_timeout(2500)
            check("凭证回来了", cred_path.is_file())
            check("徽章回到「已登录」", page.inner_text("#acctBadge") == "已登录",
                  page.inner_text("#acctBadge"))
            check("昵称也回来了", page.inner_text("#acctName") == "示例账号", page.inner_text("#acctName"))
            check("提示条让重启服务",
                  "重启" in page.inner_text("#loginStrip"), page.inner_text("#loginStrip")[:140])
            browser.close()
    finally:
        app.stop()

if errors:
    # H 步故意提交了一次不合法内容（一位都不勾），浏览器必然会记一条 400；那是预期内的
    real = [line for line in errors if "400 (Bad Request)" not in line]
    print("浏览器控制台有动静：")
    for line in errors:
        print("   ", line)
    if real:
        FAILED.append("控制台干净")
print()
print("没通过：" + "、".join(FAILED) if FAILED else "全部通过")
raise SystemExit(1 if FAILED else 0)
