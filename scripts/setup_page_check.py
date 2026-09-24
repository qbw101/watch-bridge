"""服务端配置页（`bridge/setup_web.py` + `server_config.py` + `credentials.py`）的自检。

纯本地：起一个绑 127.0.0.1 的真服务、用真 HTTP 打它，然后全部收拾掉 ——
不碰抖音、不碰 8787、不碰真实的 `server_config.json`（用临时目录）。
扫码登录那几步用**假命令**驱动（`command_factory`），所以不会真的弹浏览器。

跑法：
    python scripts/setup_page_check.py

覆盖的是「这个页面能不能安全地干活」，不是界面好不好看：
  1. 页面能取到；
  2. `/api/state` 的字段齐全；
  3. 不带 CSRF 头 / Host 头不对 → 403（这两条是「令牌不被跨站偷改」的全部靠山）；
  4. 合法提交能落盘，重新读回来一致；
  5. 非法值回 400 且消息是人话；
  6. 换令牌真的换掉；
  7. 「改了但没重启」的字段能算出来；
  8. 配置文件坏掉时降级成默认值并留档 `.bad`；
  9. 手表端好友（`config.json` 的 `friends`）：能读、能写回、**那份文件里别的键
     一个都不动**、非法输入一个字都不写、原文件读不懂时拒绝覆盖、
     还没有这份文件时能新建；
 10. 「读取好友」的后台状态机（`bridge/friend_scan.py`）：成功 / 失败 / 取消 / 重复点击，
     以及走 HTTP 的两个接口。reader 是假的，不碰浏览器；
 11. 抖音账号（`storage-state.json`）：能读出昵称/登录时间、坏文件说得出话、
     扫码登录的状态机（等 / 取消 / 成功 / 失败）、退出登录真的把凭证删掉。
"""

from __future__ import annotations

import datetime
import json
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bridge import credentials, friend_scan, server_config, setup_web, watch_friends  # noqa: E402


PASSED = 0
FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED
    if ok:
        PASSED += 1
        print(f"  [v] {name}")
    else:
        FAILED.append(name)
        print(f"  [x] {name}" + (f" —— {detail}" if detail else ""))


def request(url: str, *, method: str = "GET", payload: dict | None = None,
            headers: dict[str, str] | None = None, host: str | None = None):
    """返回 (状态码, 文本)。故意关掉代理：127.0.0.1 不该绕到系统代理上去。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        req.add_header(key, value)
    if host is not None:
        req.add_header("Host", host)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as res:
            return res.status, res.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def write_credential(path: Path, *, nickname: str = "qbw", login_ms: int = 1789477366888) -> None:
    """造一份 storage-state.json。结构照 Playwright 那份来，cookie 值是假的。

    只用来验「读得出昵称 / 登录时间」这条路径，不参与任何真实请求。
    """
    payload = {
        "cookies": [
            {"name": "sessionid", "value": "fake-session", "domain": ".douyin.com", "path": "/"},
            {"name": "login_time", "value": str(login_ms), "domain": ".douyin.com", "path": "/"},
        ],
        "origins": [
            {
                "origin": "https://www.douyin.com",
                "localStorage": [
                    {
                        "name": "user_info",
                        "value": json.dumps(
                            {
                                "uid": "MS4wLjABAAAAfake-uid-for-check-0123",
                                "nickname": nickname,
                                "avatarUrl": "https://example.invalid/avatar.jpg",
                            },
                            ensure_ascii=False,
                        ),
                    }
                ],
            }
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def fake_login_argv(mode: str) -> list[str]:
    """假登录命令：不碰浏览器、不碰抖音，只按剧本睡觉和打印。

    `mode`：ok=正常退出（等测试那边把凭证写出来）、fail=报错退出、hang=挂住等取消。
    """
    body = {
        "ok": "import time\ntime.sleep(0.8)\nprint('登录状态已保存到 storage-state.json')\n",
        "fail": "import sys\nprint('等待 300 秒后仍未检测到登录成功')\nsys.exit(3)\n",
        "hang": "import time\ntime.sleep(30)\n",
    }[mode]
    return [sys.executable, "-c", body]


def _sleep(seconds: float) -> None:
    """状态机是后台线程跑的，测的时候得给它一点时间。"""
    time.sleep(seconds)


def _wait_scan(scanner, timeout: float = 8.0):
    """等一次扫描跑完（或取消完），返回最终状态。"""
    deadline = time.time() + timeout
    state_now = scanner.state()
    while time.time() < deadline and state_now["running"]:
        time.sleep(0.05)
        state_now = scanner.state()
    return state_now


def _raise(exc: BaseException):
    raise exc


def main() -> int:
    mode = {"value": "ok"}

    with tempfile.TemporaryDirectory(prefix="watch-setup-check-") as tmp:
        cfg_path = Path(tmp) / "server_config.json"
        config, _ = server_config.load(cfg_path)
        config.token = "abcd1234"

        task_path = Path(tmp) / "config.json"
        friends_store = watch_friends.FriendStore(task_path)
        # 读取好友用假 reader：真实那个要开浏览器、要抖音登录态，这里只测状态机。
        scanner = friend_scan.FriendScanner(
            lambda should_stop: [{"name": "小明", "preview": "晚安"}, {"name": "小吴", "preview": "在吗"}]
        )

        cred_path = Path(tmp) / "storage-state.json"
        write_credential(cred_path)
        account = credentials.AccountStore(
            cred_path, project_root=PROJECT_ROOT, command_factory=lambda _t: fake_login_argv(mode["value"])
        )

        app = setup_web.SetupWeb(config,
                                 info_provider=lambda: {"lan": ["192.168.1.80"], "ipv6": [],
                                                        "service": {"backend_ready": False}},
                                 account_store=account,
                                 friend_store=friends_store,
                                 scanner=scanner)
        # 端口传 0：让系统分一个临时端口。绝不能默认从 8788 试起 —— 那上面可能
        # 正跑着用户自己的服务，SO_REUSEADDR 会让绑定「成功」，于是下面的请求全
        # 打到对方的实例上，把真实的 server_config.json 写掉（真踩过）。
        url = app.start(0)
        if not url:
            print("配置页起不来 —— 后面的检查没法跑")
            return 1
        base = url.rstrip("/")
        print(f"配置页: {base}")

        probe = app.state()
        if probe.get("config_path") != str(cfg_path) or "friends" not in probe:
            print(f"× 这个实例对不上（config_path={probe.get('config_path')}）—— 停手，不碰它。")
            app.stop()
            return 1

        try:
            print("1. 页面与状态")
            status, html = request(base + "/")
            check("GET / 返回 200", status == 200, f"实际 {status}")
            check("页面是配置页（含『手表端需要填写』）", "手表端需要填写" in html)
            check("页面有抖音账号块", "抖音账号" in html)
            check("页面引用了状态接口", "/api/state" in html)

            status, text = request(base + "/api/state")
            state = json.loads(text)
            check("GET /api/state 返回 200", status == 200, f"实际 {status}")
            check("state 字段齐全",
                  {"config", "runtime", "restart", "first_run", "url", "lan", "service", "account"} <= set(state),
                  f"实际 {sorted(state)}")
            check("首次启动标记为真", state["first_run"] is True)
            check("端口取到默认值", state["config"]["port"] == 8787, str(state["config"]["port"]))
            check("网卡地址透传", state["lan"] == ["192.168.1.80"], str(state["lan"]))

            print("2. 跨站防线")
            status, text = request(base + "/api/config", method="POST",
                                   payload={"port": "9001"}, headers={"Content-Type": "application/json"})
            check("POST 不带 CSRF 头 → 403", status == 403, f"实际 {status} {text[:80]}")
            status, _ = request(base + "/api/config", method="POST", payload={"port": "9001"},
                                headers={setup_web.CSRF_HEADER: "1"}, host="evil.example.com")
            check("Host 头不是本机 → 403", status == 403, f"实际 {status}")
            check("被拦下的请求没有写文件", not cfg_path.exists())

            print("3. 保存与回读")
            good = {"host": "0.0.0.0", "port": "9001", "token": "newtok12345",
                    "crypto": "gcm", "sticker_px": "80", "media_px": "144", "open_page": "first"}
            status, text = request(base + "/api/config", method="POST", payload=good,
                                   headers={setup_web.CSRF_HEADER: "1"})
            body = json.loads(text)
            check("合法提交返回 200", status == 200, f"实际 {status} {text[:120]}")
            check("返回里带重启提示字段", "restart" in body)
            check("文件已生成", cfg_path.is_file())
            reloaded, _ = server_config.load(cfg_path)
            check("端口落盘", reloaded.port == 9001, str(reloaded.port))
            check("图片尺寸落盘", (reloaded.sticker_px, reloaded.media_px) == (80, 144),
                  f"{reloaded.sticker_px}/{reloaded.media_px}")
            check("令牌落盘", reloaded.token == "newtok12345", reloaded.token)

            print("4. 非法值")
            status, text = request(base + "/api/config", method="POST",
                                   payload=dict(good, port="999999"),
                                   headers={setup_web.CSRF_HEADER: "1"})
            check("端口越界 → 400", status == 400, f"实际 {status}")
            check("错误消息说人话", "监听端口" in text or "端口" in text, text[:120])
            status, text = request(base + "/api/config", method="POST",
                                   payload=dict(good, crypto="rot13"),
                                   headers={setup_web.CSRF_HEADER: "1"})
            check("加密方式乱填 → 400", status == 400, f"实际 {status}")
            after, _ = server_config.load(cfg_path)
            check("非法提交没有污染文件", after.port == 9001 and after.crypto == "gcm")

            print("5. 换令牌")
            status, text = request(base + "/api/token", method="POST", payload=good,
                                   headers={setup_web.CSRF_HEADER: "1"})
            token = json.loads(text).get("token", "")
            check("换令牌返回 200", status == 200, f"实际 {status}")
            check("新令牌非空且与旧的不同的", bool(token) and token != "newtok12345", token)
            after, _ = server_config.load(cfg_path)
            check("新令牌落盘", after.token == token, after.token)

            print("6. 未生效的改动")
            app.runtime = server_config.ServerConfig(**{**after.to_dict(), "port": 8787})
            check("端口差异能算出来", app.restart_fields() == ["监听端口"], str(app.restart_fields()))
            app.runtime = None
            check("没有运行快照时不乱喊", app.restart_fields() == [])

            print("7. 配置文件坏掉")
            bad = Path(tmp) / "broken.json"
            bad.write_text("{ 这不是 JSON", encoding="utf-8")
            fallback, notes = server_config.load(bad)
            check("坏文件降级成默认值", fallback.port == 8787 and fallback.crypto == "gcm")
            check("坏文件有说明", bool(notes), str(notes))
            check("坏文件留档 .bad", bad.with_name(bad.name + ".bad").is_file())
            check("坏文件已让位", not bad.exists())

            print("8. 手加的键不丢")
            extra = Path(tmp) / "extra.json"
            extra.write_text(json.dumps({"port": 9100, "备注": "别删我"}, ensure_ascii=False),
                             encoding="utf-8")
            loaded, _ = server_config.load(extra)
            loaded.save()
            kept = json.loads(extra.read_text(encoding="utf-8"))
            check("保留不认识的键", kept.get("备注") == "别删我", str(kept))
            check("保留的键不影响已知字段", kept.get("port") == 9100)

            print("9. 手表端好友：名单从 config.json 里读出来")
            status, text = request(base + "/api/state")
            state = json.loads(text)
            check("state 带好友块", isinstance(state.get("friends"), dict), str(state.get("friends"))[:120])
            check("文件不存在也算「可以改」",
                  state["friends"]["exists"] is False and state["friends"]["editable"] is True,
                  str(state["friends"])[:160])
            check("文件路径透传", state["friends"]["path"] == str(task_path))
            check("还没有文件时名单是空的", state["friends"]["enabled"] == [], str(state["friends"]["enabled"]))

            print("10. 手表端好友：勾选写回 friends，别的键一个都不动")
            # 手写一份「用户可能自己加过东西」的 config.json：本页保存之后，
            # 除了 friends 之外的每个键都必须原封不动。
            foreign = {
                "备注": "别删我",
                "手写的第几个版本": 3,
                "别处留的结构": {"a": [1, 2], "b": None},
            }
            task_path.write_text(json.dumps({**foreign, "friends": ["小明"]}, ensure_ascii=False, indent=2),
                                 encoding="utf-8")
            status, text = request(base + "/api/friends", method="POST",
                                   payload={"enabled": ["小明", "小吴", "  小陈  "]},
                                   headers={setup_web.CSRF_HEADER: "1"})
            body = json.loads(text)
            check("保存返回 200", status == 200, f"实际 {status} {text[:160]}")
            check("回传整理后的名单（去空白）", body.get("enabled") == ["小明", "小吴", "小陈"],
                  str(body.get("enabled")))
            saved = json.loads(task_path.read_text(encoding="utf-8"))
            check("friends 落盘", saved.get("friends") == ["小明", "小吴", "小陈"], str(saved.get("friends")))
            rest = {k: v for k, v in saved.items() if k != "friends"}
            check("其余每个键都原封不动", rest == foreign, str(sorted(set(rest) ^ set(foreign) | {k for k in rest if rest[k] != foreign.get(k)}))[:200])
            check("手写的备注也还在", saved.get("备注") == "别删我")
            check("别处留的嵌套结构一字节没差", saved.get("别处留的结构") == foreign["别处留的结构"])

            print("11. 手表端好友：去重、不合法就不写")
            status, text = request(base + "/api/friends", method="POST",
                                   payload={"enabled": ["小明", "小明", " ", ""]},
                                   headers={setup_web.CSRF_HEADER: "1"})
            check("重复的名字只留一个", status == 200 and json.loads(text)["enabled"] == ["小明"], text[:140])
            check("落盘也只有一个",
                  json.loads(task_path.read_text(encoding="utf-8"))["friends"] == ["小明"])

            before = task_path.read_text(encoding="utf-8")
            for label, payload, keyword in [
                ("一个都不勾", {"enabled": []}, "至少保留一位"),
                ("名单不是数组", {"enabled": "小明"}, "列表"),
            ]:
                status, text = request(base + "/api/friends", method="POST", payload=payload,
                                       headers={setup_web.CSRF_HEADER: "1"})
                check(f"{label} → 400 且说清原因", status == 400 and keyword in text,
                      f"实际 {status} {text[:140]}")
            check("非法提交没碰过文件", task_path.read_text(encoding="utf-8") == before)

            print("12. 手表端好友：原文件读不懂时拒绝覆盖")
            bad_task = Path(tmp) / "broken_task.json"
            bad_task.write_text("{ 这不是 JSON", encoding="utf-8")
            broken_store = watch_friends.FriendStore(bad_task)
            info = broken_store.describe()
            check("坏文件在 describe 里带出错误", bool(info["error"]), str(info["error"])[:80])
            check("坏文件算「不能改」", info["editable"] is False)
            try:
                broken_store.save({"enabled": ["小明"]})
                check("坏文件时保存被拒", False, "居然写进去了")
            except watch_friends.WatchFriendsError as exc:
                check("坏文件时保存被拒", "无法读取" in str(exc), str(exc)[:120])
            check("坏文件原样没动", bad_task.read_text(encoding="utf-8") == "{ 这不是 JSON")

            print("13. 手表端好友：还没有这份文件时能新建")
            task_path.unlink()
            status, text = request(base + "/api/friends", method="POST", payload={"enabled": ["小明", "小吴"]},
                                   headers={setup_web.CSRF_HEADER: "1"})
            check("能新建", status == 200, f"实际 {status} {text[:140]}")
            fresh = json.loads(task_path.read_text(encoding="utf-8"))
            check("名单写进去了", fresh.get("friends") == ["小明", "小吴"], str(fresh))
            check("文件里就 friends 一个键", list(fresh) == ["friends"], str(list(fresh)))
            check("新文件也能被服务读回来",
                  watch_friends.load_friend_names(task_path) == ["小明", "小吴"])

            print("14. 读取好友：后台状态机（假 reader）")
            slow = friend_scan.FriendScanner(lambda should_stop: (_sleep(0.4), [{"name": "慢", "preview": ""}])[1])
            started = slow.start()
            check("start 之后就是 scanning（不等它跑完）", started["state"] == "scanning", started["state"])
            try:
                slow.start()
                check("正在读的时候再点被拒", False, "居然又起了一次")
            except friend_scan.FriendScanError:
                check("正在读的时候再点被拒", True)
            _wait_scan(slow)
            check("慢 reader 也能跑完", slow.state()["state"] == "done", slow.state()["state"])

            roster = [{"name": "小明", "preview": "晚安"}, {"name": " 小吴 ", "preview": "在吗"},
                      {"name": "小明", "preview": "重复的"}, {"name": "", "preview": "没名字的"}]
            ok_scan = friend_scan.FriendScanner(lambda should_stop: roster)
            ok_scan.start()
            done = _wait_scan(ok_scan)
            check("跑完变成 done", done["state"] == "done", done["state"])
            names = [item["name"] for item in done["items"]]
            check("名字去重、去空白、丢掉空名字", names == ["小明", "小吴"], str(names))
            check("带出读取时刻", bool(done["scanned_at"]), str(done["scanned_at"]))
            check("报出耗时", isinstance(done["elapsed"], float), str(done["elapsed"]))

            boom = friend_scan.FriendScanner(lambda should_stop: _raise(RuntimeError("抖音会话未就绪")))
            boom.start()
            failed = _wait_scan(boom)
            check("reader 抛错 → failed", failed["state"] == "failed", failed["state"])
            check("把原因带出来", "未就绪" in failed["message"], failed["message"])

            def _hang(should_stop):
                for _ in range(50):
                    if should_stop():
                        return [{"name": "半个", "preview": ""}]
                    _sleep(0.1)
                return []

            hang = friend_scan.FriendScanner(_hang)
            hang.start()
            _sleep(0.2)
            hang.cancel()
            stopped = _wait_scan(hang)
            check("取消 → cancelled", stopped["state"] == "cancelled", stopped["state"])
            check("取消后名单没有变", stopped["items"] == [], str(stopped["items"]))
            check("取消时说明白", "取消" in stopped["message"], stopped["message"])

            idle = friend_scan.FriendScanner(None)
            check("没接上抖音时会话也没得读", idle.state()["available"] is False)
            try:
                idle.start()
                check("没接上时 start 被拒", False, "居然起来了")
            except friend_scan.FriendScanError as exc:
                check("没接上时 start 被拒", "抖音会话" in str(exc), str(exc)[:120])

            print("15. 读取好友：走 HTTP 的两个接口")
            check("不带 CSRF 头 → 403",
                  request(base + "/api/friends/scan", method="POST", payload={})[0] == 403)
            status, text = request(base + "/api/friends/scan", method="POST", payload={},
                                   headers={setup_web.CSRF_HEADER: "1"})
            body = json.loads(text)
            check("发起读取返回 200", status == 200, f"实际 {status} {text[:140]}")
            check("回带了扫描状态", isinstance(body.get("friends_scan"), dict), str(body)[:140])
            deadline = time.time() + 6
            scan = json.loads(request(base + "/api/state")[1])["friends_scan"]
            while time.time() < deadline and scan["running"]:
                time.sleep(0.1)
                scan = json.loads(request(base + "/api/state")[1])["friends_scan"]
            check("state 里能看到结果", scan["state"] == "done" and scan["count"] == 2, str(scan)[:180])
            check("读到的人带最后一条消息预览", scan["items"][0]["preview"] == "晚安", str(scan["items"])[:160])
            status, text = request(base + "/api/friends/cancel", method="POST", payload={},
                                   headers={setup_web.CSRF_HEADER: "1"})
            check("没在读的时候点取消不报错", status == 200, f"实际 {status} {text[:120]}")

            print("16. 抖音账号：读出登录的是谁")
            status, text = request(base + "/api/state")
            acct = json.loads(text)["account"]
            check("state 带账号块", isinstance(acct, dict) and "login" in acct, str(acct)[:120])
            check("认出凭证文件", acct["exists"] is True and acct["path"] == str(cred_path), acct["path"])
            check("读出昵称", acct["nickname"] == "qbw", acct["nickname"])
            check("读出登录时间",
                  acct["login_at"] == datetime.datetime.fromtimestamp(1789477366).strftime("%Y-%m-%d %H:%M:%S"),
                  acct["login_at"])
            check("uid 打码不漏全", acct["uid_short"].endswith("0123") and "…" in acct["uid_short"],
                  acct["uid_short"])
            check("头像地址透传", acct["avatar"].startswith("https://"), acct["avatar"])
            check("没有登录动作时是 idle", acct["login"]["state"] == "idle", str(acct["login"]))

            print("17. 抖音账号：凭证文件坏了要说得出话")
            write_credential(cred_path)
            bad_cred = cred_path.read_text(encoding="utf-8")
            cred_path.write_text("{ 这不是 JSON", encoding="utf-8")
            acct = json.loads(request(base + "/api/state")[1])["account"]
            check("坏文件报错且不装成「没登录」", bool(acct["error"]) and acct["exists"] is True, str(acct)[:140])
            write_credential(cred_path)

            print("18. 抖音账号：扫码登录的状态机")
            check("不带 CSRF 头就登录 → 403",
                  request(base + "/api/account/login", method="POST", payload={})[0] == 403)
            cred_path.unlink()
            mode["value"] = "ok"
            status, text = request(base + "/api/account/login", method="POST", payload={"timeout": 1},
                                   headers={setup_web.CSRF_HEADER: "1"})
            body = json.loads(text)
            check("发起登录返回 200", status == 200, f"实际 {status} {text[:140]}")
            check("状态是「等你扫码」", body["account"]["login"]["state"] == "waiting",
                  str(body["account"]["login"])[:140])
            check("等待秒数被夹到下限 30", body["account"]["login"]["timeout"] == 30,
                  str(body["account"]["login"]["timeout"]))
            check("假命令真的在跑", body["account"]["login"]["running"] is True)
            status, text = request(base + "/api/account/login", method="POST", payload={},
                                   headers={setup_web.CSRF_HEADER: "1"})
            check("重复发起被拒 → 400", status == 400 and "仍在等待" in text, f"实际 {status} {text[:120]}")
            write_credential(cred_path)      # 模拟「用户扫上了」
            deadline = time.time() + 8
            state_now = ""
            while time.time() < deadline:
                acct = json.loads(request(base + "/api/state")[1])["account"]
                state_now = acct["login"]["state"]
                if state_now != "waiting":
                    break
                time.sleep(0.3)
            check("扫完变成 done", state_now == "done", state_now)
            check("done 时让人去重启", "重启" in acct["login"]["message"], acct["login"]["message"][:120])

            print("19. 抖音账号：取消与失败")
            mode["value"] = "hang"
            request(base + "/api/account/login", method="POST", payload={}, headers={setup_web.CSRF_HEADER: "1"})
            status, text = request(base + "/api/account/cancel", method="POST", payload={},
                                   headers={setup_web.CSRF_HEADER: "1"})
            check("取消返回 200", status == 200, f"实际 {status} {text[:120]}")
            check("状态变成 cancelled", json.loads(text)["account"]["login"]["state"] == "cancelled", text[:140])
            check("进程已经收回", json.loads(text)["account"]["login"]["running"] is False)
            check("没在等的时候取消 → 400",
                  request(base + "/api/account/cancel", method="POST", payload={},
                          headers={setup_web.CSRF_HEADER: "1"})[0] == 400)

            mode["value"] = "fail"
            request(base + "/api/account/login", method="POST", payload={}, headers={setup_web.CSRF_HEADER: "1"})
            deadline = time.time() + 8
            state_now = ""
            while time.time() < deadline:
                acct = json.loads(request(base + "/api/state")[1])["account"]
                state_now = acct["login"]["state"]
                if state_now != "waiting":
                    break
                time.sleep(0.3)
            check("脚本报错 → failed", state_now == "failed", state_now)
            check("把脚本最后一句带出来了", "等待 300 秒" in acct["login"]["message"],
                  acct["login"]["message"][:160])

            print("20. 抖音账号：退出登录")
            write_credential(cred_path)
            status, text = request(base + "/api/account/logout", method="POST", payload={},
                                   headers={setup_web.CSRF_HEADER: "1"})
            body = json.loads(text)
            check("退出返回 200", status == 200, f"实际 {status} {text[:120]}")
            check("凭证文件真的没了", not cred_path.exists())
            check("提醒要重启才真登出", "重启" in body.get("message", ""), body.get("message", "")[:120])
            check("状态跟着变成没登录", body["account"]["exists"] is False, str(body["account"])[:120])
            status, text = request(base + "/api/account/logout", method="POST", payload={},
                                   headers={setup_web.CSRF_HEADER: "1"})
            check("再点一次不报错，只说本来就是没登录",
                  status == 200 and json.loads(text).get("removed") is False, text[:120])
        finally:
            app.stop()

    print()
    if FAILED:
        print(f"没通过 {len(FAILED)} 项：{', '.join(FAILED)}")
        return 1
    print(f"全部通过（{PASSED} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
