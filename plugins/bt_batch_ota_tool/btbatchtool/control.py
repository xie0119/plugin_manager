"""Cyfit control channel. Notification boundaries are part of 7B/7C framing."""
import asyncio
import queue
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout

from ..btotatool import transport as tp
from .device_info import InfoBleTransport
from .shell import ShellClient, UUIDS, SHINE_UUIDS
from ..btotatool.device_services import DIS_SERVICE

_connection_lock = threading.Lock()
_refresh_next = {}


class VersionNotReady(tp.TransportError):
    """A complete 7B reply with an empty value; the link is still usable."""


class ConnectionSetupError(tp.TransportError):
    """The control link never became ready; no 7B query has been sent."""


class ControlServiceUnavailable(tp.TransportError):
    """GATT discovery completed, but legacy firmware has no Cyfit channel."""


class BoundedBleTransport(tp.BleTransport):
    def __init__(self, *args, **kwargs):
        # Legacy watches can enter OTA directly after a failed 7B query.
        # This path must also exclude discovery scans while opening GATT.
        if not _connection_lock.acquire(timeout=60):
            raise tp.TransportError('等待 OTA 蓝牙建连名额超时（60 秒）')
        try:
            super().__init__(*args, **kwargs)
        finally:
            _connection_lock.release()

    def _run(self, coro):
        timeout = getattr(self, '_operation_timeout', 20)
        deadline = getattr(self, '_operation_deadline', None)
        if deadline is not None:
            timeout = min(timeout, deadline - time.monotonic())
        if timeout <= 0:
            coro.close()
            raise TimeoutError('蓝牙操作超过本次连接期限')
        future = self._submit(coro)
        try:
            return future.result(timeout=timeout)
        except FutureTimeout as error:
            future.cancel()
            raise tp.TransportError('蓝牙操作超时（本次上限 %.1f 秒）' % timeout) from error
        except Exception:
            future.cancel()
            raise

    def close(self):
        timeout = getattr(self, '_operation_timeout', 20)
        deadline = getattr(self, '_operation_deadline', None)
        self._operation_timeout = 2
        # Disconnect must still run if unsubscribing times out first.
        self._operation_deadline = None
        try:
            tp.BleTransport.close(self)
        finally:
            self._operation_timeout, self._operation_deadline = timeout, deadline


class ControlTransport(InfoBleTransport):
    _run = BoundedBleTransport._run
    close = BoundedBleTransport.close

    def _resolve_chars(self, service_uuid, write_uuid, notify_uuid):
        if getattr(self, '_information_first', False):
            return super()._resolve_chars(service_uuid, write_uuid, notify_uuid)
        if service_uuid.lower() in (UUIDS[0].lower(), SHINE_UUIDS[0].lower()):
            service = next((s for s in self._client.services
                            if s.uuid.lower() == service_uuid.lower()), None)
            found = {c.uuid.lower() for c in service.characteristics} if service else set()
            if not {write_uuid.lower(), notify_uuid.lower()}.issubset(found):
                raise ControlServiceUnavailable('设备未提供版本查询所需的控制服务或特征')
        return super()._resolve_chars(service_uuid, write_uuid, notify_uuid)

    def open_file_channel(self):
        from .file_channel import SharedFileTransport
        return SharedFileTransport(self)

    def use_ota_service(self):
        """Change characteristics on the existing link after version queries."""
        if not self._client.is_connected:
            raise tp.TransportError('读取版本后的蓝牙连接已断开')
        if self._subscribed:
            self._run(self._client.stop_notify(self._notify_char))
            self._subscribed = False
        self._control_uuids = None
        self._resolve_chars(tp.OTA_BLE_SERVICE, tp.OTA_BLE_WRITE, tp.OTA_BLE_READ)
        self._write_gap = 0

    def __init__(self, *args, **kwargs):
        self._operation_timeout = kwargs.pop('operation_timeout', 20)
        self._operation_deadline = time.monotonic() + self._operation_timeout
        self._fota_packets = queue.Queue(maxsize=16)
        self._fota_active = threading.Event()
        self._fota_broken = False
        try:
            super().__init__(*args, **kwargs)
        except tp.TransportError as error:
            # BleTransport already closes failed sessions and wraps the cause.
            # Only a confirmed absent control channel permits legacy fallback.
            if isinstance(error.__cause__, ControlServiceUnavailable):
                raise ControlServiceUnavailable(str(error.__cause__)) from error
            raise ConnectionSetupError(str(error)) from error
        finally:
            self._operation_deadline = None

    def _on_notify(self, sender, data):
        if bytes(data[:1]) != b'\x7c':
            return super()._on_notify(sender, data)
        if self._fota_active.is_set():
            try:
                self._fota_packets.put_nowait(bytes(data))
            except queue.Full:
                self._fota_broken = True

    def query_fota(self, timeout=8):
        # With no request ID, a timed-out query must not join the next query.
        if self._fota_broken or self._fota_active.is_set():
            raise tp.TransportError('升级状态查询尚未完整结束，需要重新连接设备')
        while not self._fota_packets.empty():
            self._fota_packets.get_nowait()
        self._fota_active.set()
        try:
            self.write(b'\x7c')
            frame = self._fota_packets.get(timeout=timeout)
            if (self._fota_broken or len(frame) != 4 or frame[1] not in (0, 1) or
                    frame[2] > 100 or frame[3] > 100):
                raise tp.TransportError('设备返回的升级进度格式不正确，请确认手表使用配套固件')
            return frame[1], frame[2], frame[3]
        except Exception as error:
            self._fota_broken = True
            raise tp.TransportError('读取 modem 升级状态失败：%s' % error) from error
        finally:
            self._fota_active.clear()


def connect_control(address, shine, log, timeout=20, fresh_services=False, control_only=False,
                    include_file=False, include_ota=True):
    service, write, notify = SHINE_UUIDS if shine else UUIDS
    options = {'use_cached_services': False} if fresh_services else {}
    if control_only:
        options['use_cached_services'] = not fresh_services
    # Avoid enumerating unrelated services, characteristics and descriptors.
    options['services'] = [service, DIS_SERVICE]
    if not control_only:
        if include_ota:
            options['services'].append(tp.OTA_BLE_SERVICE)
        if include_file:
            options['services'].append(tp.LOG_BLE_SERVICE)
    # Serialize connection/session/service discovery only, never file writes
    # or OTA transfers. Short verification probes share their existing budget;
    # initial jobs may queue separately rather than consuming their connect time.
    deadline = time.monotonic() + timeout
    queue_timeout = timeout if control_only else 60
    if not _connection_lock.acquire(blocking=False):
        log('[BLE 建连 %s] 等待扫描或其他设备完成连接准备' % address)
        if not _connection_lock.acquire(timeout=max(0, queue_timeout)):
            raise tp.TransportError('等待蓝牙建连名额超时（%.1f 秒）' % queue_timeout)
    try:
        remaining = deadline - time.monotonic() if control_only else timeout
        if remaining <= 0:
            raise tp.TransportError('等待蓝牙建连名额超过本次连接期限')
        key = address.upper()
        if key in _refresh_next:
            options['use_cached_services'] = False
            log('[BLE 建连 %s] 上次连接失败，本次刷新 GATT 服务' % address)
        try:
            io = ControlTransport(address, service_uuid=service, write_uuid=write,
                                  notify_uuid=notify, strict_service=True,
                                  information_first=True, auto_subscribe=False, on_log=log,
                                  operation_timeout=remaining, **options)
        except Exception:
            # Carry this across Device instances created by bounded job retries.
            # Do not replay commands or add an extra hidden connect attempt.
            _refresh_next[key] = True
            while len(_refresh_next) > 256:
                _refresh_next.pop(next(iter(_refresh_next)))
            raise
        _refresh_next.pop(key, None)
        return io
    finally:
        _connection_lock.release()


def read_version(io, kind, timeout=8):
    label = '手表固件版本' if kind == 2 else 'modem 版本'
    result = io.query_info(kind, timeout=timeout)
    if len(result.values) != 1 or result.values[0].type != kind:
        raise tp.TransportError('设备未返回所查询的%s，请确认手表使用配套固件' % label)
    value = result.values[0].raw.decode('utf-8').strip()
    if not value:
        raise VersionNotReady('设备暂未提供%s，请稍后重试' % label)
    return value


def execute_shell(io, command):
    return ShellClient(io).execute(command, timeout=12)
