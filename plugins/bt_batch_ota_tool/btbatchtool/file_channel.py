"""File RPC on the existing control connection, with its own receive queue."""
from ..btotatool import transport as tp


class SharedFileTransport(tp.Transport):
    kind = 'BLE'

    def __init__(self, owner):
        self.owner = owner
        self._client = owner._client
        self._channel = tp._BleChannel()
        self._closed = False
        if not self._client.is_connected:
            raise tp.TransportError('文件上传前蓝牙连接已断开')
        service = next((s for s in self._client.services
                        if s.uuid.lower() == tp.LOG_BLE_SERVICE.lower()), None)
        chars = {c.uuid.lower(): c for c in service.characteristics} if service else {}
        if tp.LOG_BLE_WRITE not in chars or tp.LOG_BLE_READ not in chars:
            raise tp.TransportError('手表没有文件调试所需的日志服务，请先升级配套固件')
        self._write_char = chars[tp.LOG_BLE_WRITE]
        self._notify_char = chars[tp.LOG_BLE_READ]
        owner._run(self._client.start_notify(self._notify_char, self._on_notify))

    def _on_notify(self, sender, data):
        if not self._closed:
            self._channel.feed(bytes(data))

    def write(self, data):
        if self._closed:
            raise tp.TransportError('文件通道已关闭')
        mtu = getattr(self._client, 'mtu_size', 23) or 23
        limit = min(max(20, mtu - 3), 240)
        for start in range(0, len(data), limit):
            self.owner._run(self._client.write_gatt_char(
                self._write_char, data[start:start + limit], response=False))

    def read(self, n, timeout=5):
        return self._channel.read(n, timeout)

    def flush(self):
        pass

    def _logln(self, message):
        self.owner._logln(message)

    def close(self):
        # Only end this borrowed byte stream. Keep both CCC subscriptions on
        # the owner until final disconnect, avoiding another WinRT transition.
        # The owner's lifecycle cleanup releases every registered callback.
        self._closed = True
