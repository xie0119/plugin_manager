"""Cyfit 0x7A client. Execution is sent once; recovery only reads its session."""
from __future__ import annotations

import secrets
import struct
import threading
import time
from dataclasses import dataclass

from ..btotatool import transport as tp
from .device_info import InfoBleTransport
from .push_output import PushOutput

UUIDS = (
    "6e40fff0-b5a3-f393-e0a9-e50e24dcca9e",
    "6e400002-b5a3-f393-e0a9-e50e24dcca9e",
    "6e400003-b5a3-f393-e0a9-e50e24dcca9e",
)
SHINE_UUIDS = tuple("0000e85%d-0000-1000-8000-00805f9b34fb" % n for n in (2, 3, 4))


class ShellError(tp.TransportError):
    pass


class ResultPending(ShellError):
    """The command might be running. The caller must never replay it."""


@dataclass(frozen=True)
class ShellResult:
    command: str
    session: int
    output: str
    code: int
    truncated: bool


def validate_command(command):
    value = command.encode("utf-8")
    if not command.strip() or len(value) > 255 or any(c < 32 or c == 127 for c in value):
        raise ValueError("请输入单行 Shell 命令，最多 255 个 UTF-8 字节，不含换行或控制字符")
    return value


def packet(action, session, offset=0, data=b""):
    if action not in (0, 1, 2, 3) or not 1 <= session <= 65535 or not 0 <= offset <= 65535:
        raise ValueError("invalid Shell packet")
    if len(data) > 12 or (action != 1 and data):
        raise ValueError("invalid Shell payload")
    frame = bytearray(20)
    struct.pack_into("<BBHHB", frame, 0, 0x7A, action, session, offset, len(data))
    frame[7:7 + len(data)] = data
    frame[19] = sum(frame[:19]) & 255
    return bytes(frame)


class ShellClient:
    """Used by one worker only; transport and notification buffer persist."""

    def __init__(self, io, cancel=None, reply_timeout=8.0):
        self.io = io
        self.cancel = cancel or threading.Event()
        self.reply_timeout = reply_timeout
        self.pending = None
        self._session = secrets.randbelow(65535) + 1
        self._buffer = bytearray()
        self._push_output = None

    def _exchange(self, request):
        if self.cancel.is_set():
            raise ShellError("已停止等待；不会取消手表正在执行的指令")
        until = time.monotonic() + self.reply_timeout
        if until <= time.monotonic():
            raise ShellError("等待结果超时")
        self.io.write(request)
        while time.monotonic() < until:
            if self.cancel.is_set():
                raise ShellError("已停止等待；不会取消手表正在执行的指令")
            while len(self._buffer) >= 20:
                reply = bytes(self._buffer[:20])
                if reply[0] != 0x7A or sum(reply[:19]) & 255 != reply[19]:
                    del self._buffer[0]
                    continue
                del self._buffer[:20]
                if reply[:4] == request[:4]:
                    return struct.unpack_from("<i", reply, 4)[0], reply
            self._buffer.extend(self.io.read(256, timeout=min(.2, max(.001, until - time.monotonic()))))
        raise ShellError("蓝牙应答超时")

    def _require(self, action, session, offset=0, data=b""):
        code, response = self._exchange(packet(action, session, offset, data))
        if code:
            raise ShellError("手表拒绝操作 %d，错误码=%d" % (action, code))
        return response

    def execute(self, command, timeout=30):
        value = validate_command(command)
        if self.pending:
            raise ShellError("上一条指令结果尚未确认，请先继续查询或断开连接")
        self._session = self._session % 65535 + 1
        session = self._session
        self._require(0, session, len(value))
        for offset in range(0, len(value), 12):
            self._require(1, session, offset, value[offset:offset + 12])
        self.io.start_shell_push(session)
        self._push_output = PushOutput(session)
        # A transport failure during execute cannot tell us whether it ran.
        self.pending = (session, command)
        try:
            code, _ = self._exchange(packet(2, session, len(value)))
        except Exception as error:
            raise ResultPending("提交应答未知，请点“继续查询”；不要重复发送。%s" % error) from error
        if code:
            self.pending = None
            self.io.end_shell_push(session)
            raise ShellError("指令未被接收，错误码=%d" % code)
        return self._wait_push(timeout)

    def resume(self, timeout=30):
        if not self.pending:
            raise ShellError("没有待查询的指令")
        session, _ = self.pending
        if self.cancel.is_set():
            raise ResultPending('已停止等待，可继续查询，不会重发命令')
        self.io.start_shell_push(session)
        self._push_output = PushOutput(session)
        try:
            self._require(3, session)
        except Exception as error:
            raise ResultPending('重新获取缓存结果失败，不会重发命令。%s' % error) from error
        return self._wait_push(timeout)

    def _wait_push(self, timeout):
        session, command = self.pending
        deadline = time.monotonic() + timeout
        try:
            while not self._push_output.complete:
                if self.cancel.is_set():
                    raise ShellError('已停止等待')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ShellError('等待 Shell 主动回传超时，请确认手表已更新配套固件')
                frame = self.io.read_shell_push(min(.1, remaining))
                if frame is not None:
                    self._push_output.feed(frame)
            output = self._push_output
            value = ShellResult(command, session, output.output.decode('utf-8', errors='replace'),
                                output.code, output.truncated)
        except Exception as error:
            raise ResultPending('指令已提交，输出未收齐；可继续查询缓存，不会重发命令。%s' % error) from error
        self.io.end_shell_push(session)
        self.pending = None
        return value


class ShellSession:
    """Reuse BtOtaTool's BLE transport; connect once for multiple commands."""

    def __init__(self, log=None, factory=None, cancel=None):
        self.log = log or (lambda message: None)
        self.factory = factory or InfoBleTransport
        self.cancel = cancel or threading.Event()
        self.client = None

    @property
    def connected(self):
        if self.client is None:
            return False
        device = getattr(self.client.io, "_client", None)
        return bool(getattr(device, "is_connected", True))

    @property
    def pending(self):
        return bool(self.client and self.client.pending)

    def connect(self, address, shine=False):
        if self.client is not None:
            raise ShellError("请先断开当前连接")
        service, write, notify = SHINE_UUIDS if shine else UUIDS
        io = self.factory(address, service_uuid=service, write_uuid=write,
                          notify_uuid=notify, strict_service=True, auto_subscribe=True,
                          on_log=self.log)
        self.client = ShellClient(io, self.cancel)

    def close(self):
        client, self.client = self.client, None
        if client is not None:
            client.io.close()

    def execute(self, command, timeout):
        if not self.connected:
            raise ShellError("蓝牙连接已断开，请重新连接；不会自动重发命令")
        return self.client.execute(command, timeout)

    def resume(self, timeout):
        if not self.connected:
            raise ShellError("蓝牙连接已断开；不会自动重发命令")
        return self.client.resume(timeout)

    def query_info(self, query_type=0, timeout=8):
        if not self.connected:
            raise ShellError("请先连接手表")
        if self.pending:
            raise ShellError("上一条 Shell 指令结果尚未确认，请先继续查询")
        return self.client.io.query_info(query_type, timeout, self.cancel)
