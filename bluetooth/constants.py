"""蓝牙相关事件名与载荷约定（经 EventBus 分发）。

插件应订阅这些事件名，而不是轮询底层连接对象。
"""

# 事件名（与需求文档一致）
EVT_CONNECTED = "bluetooth.connected"
EVT_DISCONNECTED = "bluetooth.disconnected"
EVT_READY = "bluetooth.ready"
EVT_DATA_RECEIVED = "bluetooth.data_received"
EVT_ERROR = "bluetooth.error"
EVT_STATE_CHANGED = "bluetooth.state_changed"

# 载荷中常用键
KEY_MAC = "mac"
KEY_OWNER = "owner"
KEY_DATA = "data"  # bytes
KEY_CHARACTERISTIC_UUID = "characteristic_uuid"
KEY_REASON = "reason"
KEY_CODE = "code"
KEY_MESSAGE = "message"
KEY_OLD_STATE = "old_state"
KEY_NEW_STATE = "new_state"
KEY_REF_COUNT = "ref_count"
KEY_SOURCE = "source"  # "bluetooth_service" | "device_manager"

__all__ = [
    "EVT_CONNECTED",
    "EVT_DISCONNECTED",
    "EVT_READY",
    "EVT_DATA_RECEIVED",
    "EVT_ERROR",
    "EVT_STATE_CHANGED",
    "KEY_MAC",
    "KEY_OWNER",
    "KEY_DATA",
    "KEY_CHARACTERISTIC_UUID",
    "KEY_REASON",
    "KEY_CODE",
    "KEY_MESSAGE",
    "KEY_OLD_STATE",
    "KEY_NEW_STATE",
    "KEY_REF_COUNT",
    "KEY_SOURCE",
]
