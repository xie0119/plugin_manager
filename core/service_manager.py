"""应用级服务注册与注入：插件通过 ServiceManager 获取蓝牙、设备管理等公共服务。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional
import os

from PySide6.QtCore import QObject

from core.event_bus import EventBus, get_event_bus
from core.logging_config import get_plugin_logger

if TYPE_CHECKING:
    from bluetooth.service import BluetoothService
    from bluetooth.device_manager import DeviceManager


class ServiceManager:
    """集中持有 BluetoothService、DeviceManager 等，避免插件直接 import 单例实现。"""

    def __init__(self, event_bus: Optional[EventBus] = None) -> None:
        self._bus = event_bus or get_event_bus()
        self._bluetooth: Optional[Any] = None
        self._device_manager: Optional[Any] = None
        self._registry: dict[str, Any] = {}
        self._logger = get_plugin_logger("ServiceManager")

    def init_core(self, qt_parent: QObject, *, max_ble_connections: int = 8) -> "ServiceManager":
        """在 QApplication 创建之后调用，以便 DeviceManager 内 QTimer 可用。"""

        from bluetooth.service import BluetoothService
        from bluetooth.device_manager import DeviceManager
        from bluetooth.backends.qt_backend import QtBluetoothBackend

        if self._bluetooth is None:
            backend_name = os.environ.get("BLE_BACKEND", "qt").strip().lower()
            backend = None
            if backend_name in ("qt", "real", "ble"):
                try:
                    backend = QtBluetoothBackend()
                except Exception as exc:
                    self._logger.error("QtBluetoothBackend init failed: %s", exc)
                    backend = None
            else:
                backend = None  # 默认由 BluetoothService 使用 SimulatedBluetoothBackend

            self._bluetooth = BluetoothService(
                self._bus,
                max_connections=max_ble_connections,
                backend=backend,
            )
            self._logger.info("BluetoothService initialized (max=%s)", max_ble_connections)

        if self._device_manager is None:
            self._device_manager = DeviceManager(
                self._bus,
                self._bluetooth,
                parent=qt_parent,
            )
            self._logger.info("DeviceManager initialized")

        return self

    @property
    def event_bus(self) -> EventBus:
        return self._bus

    @property
    def bluetooth(self) -> "BluetoothService":
        if self._bluetooth is None:
            raise RuntimeError("ServiceManager.init_core() 尚未调用")
        return self._bluetooth

    @property
    def device_manager(self) -> "DeviceManager":
        if self._device_manager is None:
            raise RuntimeError("ServiceManager.init_core() 尚未调用")
        return self._device_manager

    def register(self, name: str, service: Any) -> None:
        """注册扩展服务（如未来打包器、OTA 通道等）。"""

        self._registry[name] = service

    def get(self, name: str) -> Any:
        if name == "bluetooth":
            return self._bluetooth
        if name == "device_manager":
            return self._device_manager
        if name == "event_bus":
            return self._bus
        return self._registry.get(name)


_service_manager: Optional[ServiceManager] = None


def get_service_manager() -> ServiceManager:
    global _service_manager
    if _service_manager is None:
        _service_manager = ServiceManager()
    return _service_manager


def reset_service_manager_for_tests() -> None:
    global _service_manager
    _service_manager = None


__all__ = ["ServiceManager", "get_service_manager", "reset_service_manager_for_tests"]

