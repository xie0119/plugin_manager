from __future__ import annotations

import random
from typing import Any, Callable

from core.logging_config import get_plugin_logger

from bluetooth.backends.base import BluetoothBackend


class SimulatedBluetoothBackend(BluetoothBackend):
    """模拟手表 Peripheral：用于无硬件开发与集成测试。

    - connect 后短延迟回调 on_connected
    - write 会在启用 notify 后模拟回包
    """

    def __init__(self, connect_delay_ms: int = 120) -> None:
        self._logger = get_plugin_logger("SimulatedBLE")
        self._connect_delay_ms = connect_delay_ms
        self._callbacks: dict[str, dict[str, Any]] = {}
        self._timer_ids: dict[str, int] = {}
        self._notify_chars: dict[str, set[str]] = {}

    def open(
        self,
        mac: str,
        *,
        on_connected: Callable[[], None],
        on_disconnected: Callable[[str], None],
        on_ready: Callable[[str], None],
        on_data: Callable[[str, bytes], None],
        on_error: Callable[[str, str], None],
    ) -> None:
        from PySide6.QtCore import QTimer

        self._callbacks[mac] = {
            "on_connected": on_connected,
            "on_disconnected": on_disconnected,
            "on_ready": on_ready,
            "on_data": on_data,
            "on_error": on_error,
        }

        self._notify_chars.setdefault(mac, set())

        def _fire_connected() -> None:
            if mac in self._callbacks:
                self._logger.info("Simulated BLE connected: %s", mac)
                on_connected()

        QTimer.singleShot(self._connect_delay_ms, _fire_connected)
        self._timer_ids[mac] = 1

    def close(self, mac: str) -> None:
        self._callbacks.pop(mac, None)
        self._timer_ids.pop(mac, None)
        self._notify_chars.pop(mac, None)

    def enable_notify(self, mac: str, characteristic_uuid: str) -> None:
        self._notify_chars.setdefault(mac, set()).add(characteristic_uuid)
        # 模拟后端：把 enable_notify 视为该 characteristic 已可用（ready）
        cb = self._callbacks.get(mac)
        if cb:
            try:
                cb["on_ready"](characteristic_uuid)
            except Exception:
                pass

    def disable_notify(self, mac: str, characteristic_uuid: str) -> None:
        chars = self._notify_chars.get(mac)
        if not chars:
            return
        chars.discard(characteristic_uuid)

    def write(self, mac: str, characteristic_uuid: str, data: bytes) -> None:
        cb = self._callbacks.get(mac)
        if not cb:
            return

        # 模拟异步回包
        from PySide6.QtCore import QTimer

        def _echo() -> None:
            if mac not in self._callbacks:
                return
            # 若 notify 未启用，则模拟不会有回包
            if characteristic_uuid not in self._notify_chars.get(mac, set()):
                return
            # 简单协议：回显 + 随机尾字节，便于调试
            reply = b"ACK:" + data[:64] + bytes([random.randint(0, 255)])
            try:
                cb["on_data"](characteristic_uuid, reply)
            except Exception:
                pass

        QTimer.singleShot(30, _echo)


__all__ = ["SimulatedBluetoothBackend"]

