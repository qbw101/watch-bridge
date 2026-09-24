"""对比两台机器上 bridge 服务的身份：协议版本与应答字段。

背景：手表连的是 192.168.1.15，而那台机器上跑着一个能正常响应 Python 客户端的
服务，但手表端解不开它发的每一帧（doFinal failed）。要区分两种可能：

  A. 那台机器跑的是**旧版本**（比如发送时加密没包进锁，并发发送会撞 nonce）；
  B. 两台机器代码一致，问题在手表端的加密实现。

做法是不猜版本号，而是直接看**应答的字段集合**：新版 status 里带了
thumbBytes / sticker_bytes（表情缩略图体积统计），旧版没有。HELLO 应答也一并
打出来，协议版本对不上会直接暴露。

用法：
    python scripts/probe_identity.py
    python scripts/probe_identity.py --hosts 192.168.1.15,192.168.1.80 --token a1b2c3d4
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tcp_smoke import BridgeTcpClient  # noqa: E402

# 新版 status 独有 / 旧版没有的字段。加字段时同步更新这里。
NEW_STATUS_FIELDS = ("thumbBytes", "sticker_bytes")


def probe(host: str, port: int, token: str) -> dict:
    """连一台，返回它的自述信息；失败时把异常放进结果里。"""
    result: dict = {"host": f"{host}:{port}"}
    client = BridgeTcpClient(host=host, port=port, token=token, crypto="gcm", timeout=15.0)
    try:
        client.connect()
        result["hello"] = dict(client.server_info)
        status = client.request("status")
        result["status_fields"] = sorted(status.keys())
        result["status_subset"] = {
            k: status[k] for k in ("ready", "friend_count", "sticker_count", "current_chat") if k in status
        }
        # 新版会把缩略图体积报出来，旧版没有 —— 用它当版本探针
        result["looks_new"] = all(field in status for field in NEW_STATUS_FIELDS)
        result["missing_new_fields"] = [f for f in NEW_STATUS_FIELDS if f not in status]
    except Exception as exc:  # noqa: BLE001 - 探测脚本要能把失败也报出来
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            client.close()
        except Exception:  # noqa: BLE001
            pass
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="对比各主机上 bridge 服务的版本特征")
    parser.add_argument("--hosts", default="192.168.1.15,192.168.1.80", help="逗号分隔的主机列表")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--token", default="a1b2c3d4")
    args = parser.parse_args()

    reports = []
    for raw in args.hosts.split(","):
        host = raw.strip()
        if not host:
            continue
        print("=" * 68, flush=True)
        print(f"探测 {host}:{args.port}", flush=True)
        print("=" * 68, flush=True)
        report = probe(host, args.port, args.token)
        reports.append(report)
        if "error" in report:
            print(f"  连接失败：{report['error']}", flush=True)
            continue
        hello = report["hello"]
        print(f"  HELLO 应答：{json.dumps(hello, ensure_ascii=False, sort_keys=True)}", flush=True)
        print(f"  协议版本 v={hello.get('v')}  加密={hello.get('crypto')}", flush=True)
        print(f"  status 字段：{', '.join(report['status_fields'])}", flush=True)
        print(f"  status 摘要：{json.dumps(report['status_subset'], ensure_ascii=False)}", flush=True)
        verdict = "新版（含缩略图体积统计）" if report["looks_new"] else f"旧版（缺 {report['missing_new_fields']}）"
        print(f"  版本判定：{verdict}", flush=True)
        print(flush=True)

    if len(reports) == 2 and all("error" not in r for r in reports):
        same_version = reports[0]["looks_new"] == reports[1]["looks_new"]
        same_proto = reports[0]["hello"].get("v") == reports[1]["hello"].get("v")
        print("=" * 68, flush=True)
        print(f"两台版本一致：{'是' if same_version else '否'}   协议版本一致：{'是' if same_proto else '否'}", flush=True)
        if not same_version:
            print("→ 版本不同的那台就是问题所在：手表连的服务比本地旧。", flush=True)
        else:
            print("→ 两台代码一致，问题出在手表端的加密实现（不是服务端版本）。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
