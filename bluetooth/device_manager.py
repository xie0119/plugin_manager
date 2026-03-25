"""设备管理：聚合蓝牙事件、维护每 MAC 状态快照，供 GUI 或其它服务读取。

自动重连：对标记为 auto_reconnect 的设备，在非「owner 全部释放」断开时，
使用最近一次成功连接的 owner 标识重新调用 BluetoothService.connect。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from PySide6.QtCore import QObject, QTimer

from bluetooth.constants import (
    EVT_CONNECTED,
    EVT_DATA_RECEIVED,
    EVT_DISCONNECTED,
    EVT_ERROR,
    EVT_STATE_CHANGED,
    KEY_MAC,
    KEY_MESSAGE,
    KEY_NEW_STATE,
    KEY_OWNER,
    KEY_REASON,
)
from core.event_bus import EventBus, EventHandler    
from core.logging_config import get_plugin_logger


@dataclass
class DeviceSnapshot:
    mac: str
    state: str = "disconnected"
    last_error: str = ""
    last_rx_preview: str = ""
    display_name: str = ""
    auto_reconnect: bool = False
    reconnect_delay_ms: int = 2000
    meta: Dict[str, Any] = field(default_factory=dict)


class DeviceManager(QObject):
    """设备状态机 + GUI 数据视图 + 可选自动重连。"""

    def __init__(
        self,
        event_bus: EventBus,
        bluetooth_service: Any,
        parent: Optional[QObject] = None,
    ) -> None:
        super().__init__(parent)
        self._bus = event_bus
        self._bt = bluetooth_service
        self._lock = threading.RLock()
        self._devices: Dict[str, DeviceSnapshot] = {}
        self._last_owner: Dict[str, str] = {}
        self._logger = get_plugin_logger("DeviceManager")

        self._handler: EventHandler = self._on_bluetooth_event
        for ev in (
            EVT_CONNECTED,
            EVT_DISCONNECTED,
            EVT_DATA_RECEIVED,
            EVT_ERROR,
            EVT_STATE_CHANGED,
        ):
            self._bus.subscribe(ev, "DeviceManager", self._handler)

    def shutdown(self) -> None:
        for ev in (
            EVT_CONNECTED,
            EVT_DISCONNECTED,
            EVT_DATA_RECEIVED,
            EVT_ERROR,
            EVT_STATE_CHANGED,
        ):
            self._bus.unsubscribe(ev, self._handler)

    def _ensure(self, mac: str) -> DeviceSnapshot:
        if mac not in self._devices:
            self._devices[mac] = DeviceSnapshot(mac=mac)
        return self._devices[mac]

    def _on_bluetooth_event(self, event: str, data: Dict[str, Any]) -> None:
        mac = data.get(KEY_MAC)
        if not mac:
            return
        with self._lock:
            snap = self._ensure(mac)
            if event == EVT_CONNECTED:
                owner = data.get(KEY_OWNER, "")
                if owner:
                    self._last_owner[mac] = str(owner)
                reason = str(data.get(KEY_REASON, ""))
                if reason in ("established", "reused"):
                    snap.state = "connected"
                elif reason in ("connecting", "pending_share"):
                    snap.state = "connecting"
                else:
                    snap.state = "connected"
            elif event == EVT_DISCONNECTED:
                reason = str(data.get(KEY_REASON, ""))
                snap.state = "disconnected"
                if reason and reason != "all_owners_released":
                    snap.last_error = reason
                # 自动重连（主线程定时器）
                if snap.auto_reconnect and reason != "all_owners_released":
                    owner = self._last_owner.get(mac, "__device_manager__")
                    delay = max(500, snap.reconnect_delay_ms)

                    def _do_reconnect() -> None:
                        try:
                            self._bt.connect(mac, owner)
                        except Exception as exc:
                            self._logger.error("auto reconnect %s: %s", mac, exc)

                    QTimer.singleShot(delay, _do_reconnect)
            elif event == EVT_DATA_RECEIVED:
                raw = data.get("data")
                if isinstance(raw, (bytes, bytearray)):
                    snap.last_rx_preview = raw[:128].hex()
                else:
                    snap.last_rx_preview = str(raw)[:256]
            elif event == EVT_ERROR:
                snap.last_error = str(data.get(KEY_MESSAGE, "error"))
                snap.state = "error"
            elif event == EVT_STATE_CHANGED:
                snap.state = str(data.get(KEY_NEW_STATE, snap.state))

    def set_display_name(self, mac: str, name: str) -> None:
        with self._lock:
            self._ensure(mac).display_name = name

    def set_auto_reconnect(self, mac: str, enabled: bool, delay_ms: int = 2000) -> None:
        with self._lock:
            s = self._ensure(mac)
            s.auto_reconnect = enabled
            s.reconnect_delay_ms = delay_ms

    def get_snapshot(self, mac: str) -> Optional[DeviceSnapshot]:
        with self._lock:
            return self._devices.get(mac)

    def list_devices(self) -> list[DeviceSnapshot]:
        with self._lock:
            return list(self._devices.values())


__all__ = ["DeviceManager", "DeviceSnapshot"]
