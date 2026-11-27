"""Read-only Cyfit 0x7B queries; preserve each BLE notification boundary."""
from __future__ import annotations

import queue
import struct
import threading
import time
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass

from ..btotatool import transport as tp
from ..btotatool.device_services import DIS_SERVICE, bluetooth_uuid, alert_packet, validate_alert_reply

INFO_TYPES = {
    0: "全部",
    1: "设备名称",
    2: "固件版本",
    3: "构建时间",
    4: "Git ID",
    5: "模组版本",
}
MAX_INFO_BYTES = 4096


class DeviceInfoError(tp.TransportError):
    pass


@dataclass(frozen=True)
class InfoValue:
    type: int
    raw: bytes

    @property
    def label(self):
        return INFO_TYPES.get(self.type, "未知类型")

    @property
    def text(self):
        return self.raw.decode("utf-8") if self.raw else "（当前不可用）"


@dataclass(frozen=True)
class DeviceInfoResult:
    query_type: int
    payload: bytes
    values: tuple


def decode_info(payload, query_type):
    values, seen, offset = [], set(), 0
    while offset < len(payload):
        if len(payload) - offset < 2:
            raise DeviceInfoError("0x7B TLV 头不完整")
        kind, length = payload[offset:offset + 2]
        offset += 2
        if kind == 0 or kind in seen or (query_type and kind != query_type):
            raise DeviceInfoError("0x7B 返回类型重复或与请求不符")
        if length > 63 or length > len(payload) - offset:
            raise DeviceInfoError("0x7B TLV 长度无效或内容不完整")
        raw = payload[offset:offset + length]
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise DeviceInfoError("0x7B 返回值不是有效 UTF-8") from error
        if b"\0" in raw:
            raise DeviceInfoError("0x7B 返回值含有协议外的 NUL 字节")
        values.append(InfoValue(kind, raw))
        seen.add(kind)
        offset += length
    return DeviceInfoResult(query_type, bytes(payload), tuple(values))


class InfoBleTransport(tp.BleTransport):
    """Route info/Shell pushes separately from fixed 20-byte control replies."""

    def __init__(self, *args, **kwargs):
        self._information_first = kwargs.pop('information_first', False)
        self._control_uuids = None
        self._dis_invalid = False
        self._alert_packets = queue.Queue(maxsize=32)
        self._alert_lock = threading.Lock()
        self._alert_active = False
        self._alert_uncertain = False
        self._alert_overflow = False
        self._info_active = threading.Event()
        self._info_overflow = threading.Event()
        self._info_packets = queue.Queue(maxsize=256)
        self._info_broken = False
        self._shell_session = 0
        self._shell_packets = queue.Queue(maxsize=128)
        self._shell_overflow = threading.Event()
        if self._information_first:
            kwargs['auto_subscribe'] = False
        super().__init__(*args, **kwargs)

    def _resolve_chars(self, service_uuid, write_uuid, notify_uuid):
        if self._information_first and service_uuid.lower() != tp.OTA_BLE_SERVICE.lower():
            self._control_uuids = (service_uuid, write_uuid, notify_uuid)
            self._write_char = self._notify_char = None
            return
        return super()._resolve_chars(service_uuid, write_uuid, notify_uuid)

    def ensure_control(self):
        if self._control_uuids:
            service, write, notify = self._control_uuids
            super()._resolve_chars(service, write, notify)
            self._control_uuids = None
        self.start_notifications()

    def write(self, data):
        if self._control_uuids:
            self.ensure_control()
        return super().write(data)

    def use_ota_service(self):
        if self._subscribed:
            self._run(self._client.stop_notify(self._notify_char))
            self._subscribed = False
        self._control_uuids = None
        super()._resolve_chars(tp.OTA_BLE_SERVICE, tp.OTA_BLE_WRITE, tp.OTA_BLE_READ)
        self._write_gap = 0

    def read_dis(self, kind, timeout=1.2):
        service = self._client.services.get_service(DIS_SERVICE)
        if service is None:
            return None
        characteristic = service.get_characteristic(bluetooth_uuid(kind))
        if characteristic is None:
            return None
        if 'read' not in characteristic.properties:
            raise tp.TransportError('DIS 特征不可读')
        previous = getattr(self, '_operation_timeout', None)
        self._operation_timeout = timeout
        try:
            return bytes(self._run(asyncio.wait_for(
                self._client.read_gatt_char(characteristic, use_cached=False), timeout=timeout)))
        except (TimeoutError, FutureTimeout, asyncio.TimeoutError):
            self._dis_invalid = True
            raise tp.TransportError('DIS 读取超时，连接需要重建')
        except Exception as error:
            if (isinstance(error.__cause__, (TimeoutError, FutureTimeout, asyncio.TimeoutError)) or
                    getattr(error, 'winerror', None) == -2147483629 or not self._client.is_connected):
                self._dis_invalid = True
            raise
        finally:
            self._operation_timeout = previous

    def completion_alert(self, action=1, timeout=3):
        request = alert_packet(action)
        with self._alert_lock:
            if self._alert_uncertain:
                raise tp.TransportError('上次 0x80 结果未知，请重新连接；不会自动重发')
            while not self._alert_packets.empty():
                self._alert_packets.get_nowait()
            deadline = time.monotonic() + timeout
            previous = getattr(self, '_operation_timeout', None)
            self._alert_active = True
            self._alert_uncertain = True
            self._alert_overflow = False
            try:
                self._operation_timeout = max(.001, deadline - time.monotonic())
                self.ensure_control()
                self._logln('0x80 TX: ' + request.hex(' '))
                self._operation_timeout = max(.001, deadline - time.monotonic())
                self.write(request)
                while time.monotonic() < deadline:
                    try:
                        frame = self._alert_packets.get(timeout=max(.001, deadline - time.monotonic()))
                    except queue.Empty:
                        break
                    if self._alert_overflow:
                        raise tp.TransportError('0x80 应答队列溢出，结果未确认')
                    # An old stop notification cannot confirm a start request.
                    if len(frame) == 16 and frame[1] != action:
                        validate_alert_reply(frame, frame[1])
                        continue
                    validate_alert_reply(frame, action)
                    self._alert_uncertain = False
                    return
                raise tp.TransportError('0x80 等待应答超时（%.1f 秒）' % timeout)
            except Exception as error:
                self._logln('0x80 异常：' + str(error))
                raise
            finally:
                self._alert_active = False
                self._operation_timeout = previous

    def _on_notify(self, sender, data):
        data = bytes(data)
        if data[:1] == b'\x80':
            self._logln('0x80 RX: ' + data.hex(' '))
            if self._alert_active:
                try:
                    self._alert_packets.put_nowait(data)
                except queue.Full:
                    self._alert_uncertain = True
                    self._alert_overflow = True
            return
        if data[:1] == b'\x7a' and len(data) >= 2 and data[1] in (0x80, 0x81):
            if self._shell_session and (len(data) < 4 or
                    struct.unpack_from('<H', data, 2)[0] == self._shell_session):
                try:
                    self._shell_packets.put_nowait(data)
                except queue.Full:
                    self._shell_overflow.set()
            return
        if data[:1] != b"\x7b":
            return super()._on_notify(sender, data)
        if self._info_active.is_set():
            self._logln("0x7B RX: " + data.hex(" "))
            try:
                self._info_packets.put_nowait(data)
            except queue.Full:
                self._info_overflow.set()

    def start_shell_push(self, session):
        while not self._shell_packets.empty():
            self._shell_packets.get_nowait()
        self._shell_overflow.clear()
        self._shell_session = session

    def end_shell_push(self, session):
        if self._shell_session == session:
            self._shell_session = 0

    def read_shell_push(self, timeout):
        if self._shell_overflow.is_set():
            raise tp.TransportError('Shell 推送接收队列溢出，请继续查询缓存结果')
        try:
            return self._shell_packets.get(timeout=timeout)
        except queue.Empty:
            return None

    def query_info(self, query_type=0, timeout=8, cancel=None):
        if not isinstance(query_type, int) or not 0 <= query_type <= 255:
            raise ValueError("设备信息类型必须为 0～255")
        if timeout <= 0:
            raise ValueError("查询超时必须大于 0")
        if self._info_broken:
            raise DeviceInfoError("上次 0x7B 查询未完整结束，请断开重连后再查询")
        if self._info_active.is_set():
            raise DeviceInfoError("已有设备信息查询正在进行")
        cancel = cancel or threading.Event()
        if cancel.is_set():
            raise DeviceInfoError("已停止设备信息查询")
        while not self._info_packets.empty():
            self._info_packets.get_nowait()
        self._info_overflow.clear()
        self._info_active.set()
        payload, total = bytearray(), None
        deadline = time.monotonic() + timeout
        try:
            # Exactly two bytes; no Shell framing, padding or checksum.
            self.write(bytes((0x7B, query_type)))
            while True:
                if cancel.is_set():
                    raise DeviceInfoError("已停止设备信息查询")
                if self._info_overflow.is_set():
                    raise DeviceInfoError("0x7B 接收队列溢出")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise DeviceInfoError("0x7B 应答超时；请确认手表已更新配套固件")
                try:
                    frame = self._info_packets.get(timeout=min(.1, remaining))
                except queue.Empty:
                    continue
                if len(frame) < 3:
                    raise DeviceInfoError("0x7B 响应头不完整")
                length = struct.unpack_from("<H", frame, 1)[0]
                if length > MAX_INFO_BYTES or (total is not None and length != total):
                    raise DeviceInfoError("0x7B 总长度异常或分包总长度不一致")
                total = length
                if len(frame) == 3 and total:
                    raise DeviceInfoError("0x7B 分包没有有效数据")
                payload.extend(frame[3:])
                if len(payload) > total:
                    raise DeviceInfoError("0x7B 收到的数据超过声明总长度")
                if len(payload) == total:
                    return decode_info(payload, query_type)
        except Exception as error:
            # No request ID/offset exists: late chunks cannot safely join a retry.
            self._info_broken = True
            raise DeviceInfoError("%s；请断开重连后再查询 0x7B" % error) from error
        finally:
            self._info_active.clear()
