"""手表端桥接服务（裸 TCP，非 HTTP —— 见 bridge/protocol.py）。"""

from bridge.session import BridgeError, DouyinBridge
from bridge.tcp_server import TcpBridgeServer

# 兼容旧名字：外部脚本/文档里还写着 BridgeServer。
BridgeServer = TcpBridgeServer

__all__ = ["BridgeError", "BridgeServer", "DouyinBridge", "TcpBridgeServer"]
