"""蓝牙底层后端抽象：插件不得直接使用，仅由 BluetoothService 调用。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable


class BluetoothBackend(ABC):
    """Central 侧底层连接抽象（真实实现可替换为 Qt BLE / bleak 等）。"""

    @abstractmethod
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
        """异步建立连接；成功/失败通过回调通知。"""

    @abstractmethod
    def close(self, mac: str) -> None:
        """关闭该 MAC 对应底层连接。"""

    @abstractmethod
    def enable_notify(self, mac: str, characteristic_uuid: str) -> None:
        """启用指定 characteristic 的 notify（写入 CCCD）。"""

    @abstractmethod
    def disable_notify(self, mac: str, characteristic_uuid: str) -> None:
        """禁用指定 characteristic 的 notify（清除 CCCD）。"""

    @abstractmethod
    def write(
        self, mac: str, characteristic_uuid: str, data: bytes
    ) -> None:
        """向指定 characteristic 发送数据（write / write_no_resp）。"""


__all__ = ["BluetoothBackend"]

