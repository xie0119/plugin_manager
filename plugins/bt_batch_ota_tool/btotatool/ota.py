# -*- coding: utf-8 -*-
"""
OTA 状态机 —— 1:1 对齐 Java 的 ``com.actions.aticonsota.OTAManager``。

时序（与 Android 端完全一致）：

    主机                                   设备
     |------- 0x09/0x01 握手(checkRemoteStatus) ------>|
     |<------ 0x09/0x01 状态(版本/板名/电量/feature) ---|
     |------- 0x09/0x02 请求参数(requestRemoteParameters) ->|
     |<------ 0x09/0x02 参数(otaUnit/interval/ackEnable) --|
     |------- 0x09/0x09 通知 APP 就绪 ------------------->|
     |<------ 0x09/0x03 请求数据(offset/length/bitmap) --|
     |------- 0x09/0x0B(或0x04) 数据帧 × N ------------->|
     |<------ 0x09/0x05 包大小 / 0x09/0x06 校验结果 ------|
     |------- 0x09/0x06 回应 --------------------------->|
"""

from __future__ import annotations

import os
import traceback
import threading
import time
from typing import Callable, List, Optional

from . import protocol
from .otafile import OtaFile, parse_ota_file
from .transport import Transport, TransportError


class OtaState:
    IDLE = "IDLE"
    PREPARING = "PREPARING"
    PREPARED = "PREPARED"
    TRANSFERRING = "TRANSFERRING"
    TRANSFERRED = "TRANSFERRED"
    SKIPPED = "SKIPPED"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class RemoteStatus:
    """设备在 0x0901 响应里回报的信息。"""

    def __init__(self):
        self.battery_threshold: int = 0
        self.version_name: str = ""
        self.board_name: str = ""
        self.hardware_rev: str = ""
        self.version_code: int = 0
        self.feature_support: int = 0
        self.crc_support: bool = False

    def summary(self) -> str:
        return "版本=%s  板名=%s  硬件=%s  版本号=%d  电量阈值=%d  CRC32=%s" % (
            self.version_name or "?",
            self.board_name or "?",
            self.hardware_rev or "?",
            self.version_code,
            self.battery_threshold,
            "支持" if self.crc_support else "不支持",
        )


class OtaParams:
    """设备在 0x0902 响应里回报的传输参数。"""

    def __init__(self):
        self.wait_timeout: int = 0
        self.restart_timeout: int = 0
        self.ota_unit: int = protocol.DEFAULT_OTA_UNIT
        self.interval: int = 0
        self.ack_enable: int = 0

    def summary(self) -> str:
        return "分片=%d  间隔=%d  应答=%d  等待超时=%d  重启超时=%d" % (
            self.ota_unit,
            self.interval,
            self.ack_enable,
            self.wait_timeout,
            self.restart_timeout,
        )


class OtaClient:
    """OTA 客户端。

    线程模型：一个后台读线程负责收帧并分发；发送动作都在调用方线程执行。
    """

    def __init__(
        self,
        transport: Transport,
        on_status: Optional[Callable[[str], None]] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
        on_log: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[int, str], None]] = None,
        on_remote_status: Optional[Callable[[RemoteStatus], None]] = None,
        auto_start: bool = True,
        dry_run: bool = False,
        skip_same_version: bool = False,
    ):
        self.io = transport
        self._on_status = on_status or (lambda s: None)
        self._on_progress = on_progress or (lambda p, t: None)
        self._on_log = on_log or (lambda m: None)
        self._on_error = on_error or (lambda c, m: None)
        self._on_remote_status = on_remote_status or (lambda s: None)
        self._auto_start = auto_start
        #: dry_run = True 时走到"设备要数据"就停，只打印请求内容，不发数据
        self.dry_run = dry_run
        # Kept for caller compatibility; batch history checks run before connecting.
        self.skip_same_version = skip_same_version

        self.state = OtaState.IDLE
        self.remote_status = RemoteStatus()
        self.params = OtaParams()
        self.ota_file: Optional[OtaFile] = None

        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._write_bytes = 0
        #: 设备已校验通过（对应 Java 的 mIsTranFinish）。
        #: 之后链路断开是**正常现象**——设备校验完就准备重启，
        #: 所以此时的读超时不能再当"链路异常"报出去。
        self._finished = False

    # ------------------------------------------------------------ 日志

    def log(self, msg: str) -> None:
        self._on_log(msg)

    def _set_state(self, state: str) -> None:
        if self.state == state:
            return
        self.state = state
        self._on_status(state)

    # ------------------------------------------------------------ 准备

    def set_file(self, path: str) -> OtaFile:
        """设置 OTA 固件并解析包头（对应 Java: setOTAFile + readOTAHeader）。"""
        info = parse_ota_file(path)
        self.ota_file = info
        self.log("固件：%s" % info.summary())
        if not info.fw_version_bytes:
            raise TransportError("固件包头解析失败，无法取得版本信息")
        return info

    def start_reader(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(target=self._reader_loop, name="ota-reader", daemon=True)
        self._thread.start()

    def stop_reader(self) -> None:
        self._running = False

    # ------------------------------------------------------------ 对外动作

    def prepare(self) -> None:
        """第一步：握手（对应 Java: prepare() -> handshake() -> checkRemoteStatus）。

        发 0x0901 后**立刻**自动续传 0x0902（一步到底）。
        设备进 OTA 页面后 10 秒内收不到 OTA 包会自动重启，
        所以这里不设"等用户再点一次"的中间停顿。
        """
        if not self.ota_file:
            raise TransportError("尚未设置 OTA 文件")
        self.io.start_notifications()
        self.start_reader()
        self._set_state(OtaState.PREPARING)
        self._auto_start = True

        fw = self.ota_file.fw_version_bytes
        sub = [
            protocol.tlv(0x01, fw),  # package version
            protocol.tlv(0x02, b"\x00\x00"),  # component size
            protocol.tlv(0x03, self.ota_file.vram),  # VRAM
            protocol.tlv(0x04, b"\x00"),  # ota work mode
            protocol.tlv(0x09, b"\x01"),  # CRC32, same as the Android client
        ]
        self._send(protocol.assemble_command(protocol.OTA_SERVICE_ID, protocol.OTA_CMD_CHECK_STATUS, sub))
        self.log("已发送握手 0x0901（版本=%s）" % self.ota_file.fw_version[:32])

    def start_upgrade(self) -> None:
        """第二步：请求参数并进入传输（对应 Java: upgrade() -> requestRemoteParameters）。"""
        self._send(protocol.assemble_command(protocol.OTA_SERVICE_ID, protocol.OTA_CMD_REQUEST_PARAMS, None))
        self.log("已发送 0x0902 请求设备参数")
        self._write_bytes = 0

    def cancel(self) -> None:
        self._running = False
        self._set_state(OtaState.IDLE)
        self.log("已取消")

    def close(self) -> None:
        self._running = False

    # ------------------------------------------------------------ 收发

    def _send(self, data: bytes) -> None:
        with self._lock:
            try:
                self.io.flush()
                self.io.write(data)
                self.io.flush()
            except Exception as e:
                raise TransportError("发送失败：%s\n%s" % (e, traceback.format_exc())) from e

    def _reader_loop(self) -> None:
        while self._running:
            try:
                frame = self._read_frame()
                if frame is None:
                    continue
                self._dispatch(frame)
            except TransportError as e:
                if self._finished:
                    # 设备已校验通过并开始重启，链路随之断开是**正常现象**
                    # （对应 Java 的 mIsTranFinish：传输完成后不再上报 IO 错误）。
                    self.log("设备已完成升级并断开链路（设备即将重启，升级成功）")
                    self._running = False
                    break
                self.log("链路异常：%s" % str(e).splitlines()[0])
                if self._running:
                    time.sleep(0.05)
                continue
            except Exception as e:
                if self._finished:
                    self.log("设备已完成升级并断开链路（设备即将重启，升级成功）")
                    self._running = False
                    break
                self.log("读取异常：%s" % e)
                time.sleep(0.05)

    def _read_frame(self):
        """读一帧：service(1) + cmd(1) + 0x80 + len(2) + body(len)。"""
        head = self.io.read_exactly(2, timeout=30.0)
        service = head[0]
        cmd = head[1]
        super_tlv = self.io.read_exactly(3, timeout=30.0)
        length = protocol.u16le(super_tlv, 1)
        body = self.io.read_exactly(length, timeout=30.0) if length else b""
        return protocol.Frame(service, cmd, body)

    # ------------------------------------------------------------ 分发

    def _dispatch(self, frame: protocol.Frame) -> None:
        if frame.service != protocol.OTA_SERVICE_ID:
            self.log("忽略非 OTA 帧：service=0x%02X cmd=0x%02X" % (frame.service, frame.cmd))
            return
        handler = {
            protocol.OTA_CMD_CHECK_STATUS: self._on_cmd_status,
            protocol.OTA_CMD_REQUEST_PARAMS: self._on_cmd_params,
            protocol.OTA_CMD_D2H_TRANSFER_REQ: self._on_cmd_transfer_req,
            protocol.OTA_CMD_D2H_PKG_SIZE: self._on_cmd_pkg_size,
            protocol.OTA_CMD_H2D_ACK_FINISH: self._on_cmd_finish,
            protocol.OTA_CMD_D2H_ERROR: self._on_cmd_error,
        }.get(frame.cmd)
        if handler is None:
            self.log("未处理的 OTA 命令 0x%02X" % frame.cmd)
            return
        handler(frame.body)

    # -- 0x0901 状态 ------------------------------------------------

    def _on_cmd_status(self, body: bytes) -> None:
        if len(body) < 7:
            self.log("0x0901 响应过短")
            return
        if body[0] != 0x7F:
            self.log("0x0901 响应头异常：0x%02X" % body[0])
            return
        err = protocol.u32le(body, 3)
        self.log("0x0901 errCode=%d（100000=成功）" % err)
        self.log("0x0901 原始响应：%s" % body.hex(' '))
        if err != protocol.OTA_ERR_OK:
            msg = "bin 文件错误！" if err == protocol.OTA_ERR_BIN_FILE else "未知错误"
            self._on_error(err, msg)
            self._set_state(OtaState.ERROR)
            return

        st = RemoteStatus()
        # 严格按安卓 OTAManager 的 0x0901 解析语义逐字节走：
        #   type(1) + len(2, 小端) + value
        # 注意安卓对定长字段是**硬编码**消费长度，不是按 len 走：
        #   0x04 batteryThreshold → 消费 1 字节（len 字段只被读掉，不参与推进）
        #   0x05/0x06/0x07        → 消费 len 字节
        #   0x08 versionCode      → 消费 4 字节
        #   0x09 featureSupport   → 消费 1 字节
        #   其它                  → 消费 len 字节
        # 之前用统一的"按 length 跳过"逻辑，设备回报定长字段时就整段错位，
        # 导致版本名/板名/硬件全解析成空、版本号恒为 0。
        index = 7
        while index + 3 <= len(body):
            t = body[index]
            length = protocol.u16le(body, index + 1)
            index += 3
            if t == 0x04:  # battery threshold：定长 1 字节
                if index >= len(body):
                    break
                st.battery_threshold = body[index]
                index += 1
            elif t in (0x05, 0x06, 0x07):  # version name / board name / hardware rev
                if index + length > len(body):
                    break
                value = body[index:index + length]
                index += length
                text = value.decode("utf-8", "replace").rstrip("\x00")
                if t == 0x05:
                    st.version_name = text
                elif t == 0x06:
                    st.board_name = text
                else:
                    st.hardware_rev = text
            elif t == 0x08:  # version code：定长 4 字节
                if index + 4 > len(body):
                    break
                st.version_code = protocol.u32le(body, index)
                index += 4
            elif t == 0x09:  # feature support：定长 1 字节
                if index >= len(body):
                    break
                st.feature_support = body[index]
                st.crc_support = (st.feature_support & 0x01) == 0x01
                index += 1
            else:
                if index + length > len(body):
                    break
                index += length
        self.remote_status = st
        self.log("OTA握手参数（版本已由7B比较）：%s" % st.summary())
        self._on_remote_status(st)
        # Version names do not identify resource/font/firmware package contents.
        # Batch jobs check their local per-device package history before connecting.
        self._set_state(OtaState.PREPARED)
        # 设备进入 OTA 页面后 10 秒内收不到 OTA 包就会超时重启，
        # 所以握手成功后必须立即续上 0x0902，不能等用户点按钮。
        if self._auto_start:
            self.start_upgrade()

    # -- 0x0902 传输参数 --------------------------------------------

    def _on_cmd_params(self, body: bytes) -> None:
        if len(body) < 24:
            self.log("0x0902 响应过短")
            return
        p = OtaParams()
        # Java 里每个字段前面固定有 3 字节头，字段偏移 3 / 8 / 13 / 18 / 23
        p.wait_timeout = protocol.u16le(body, 3)
        p.restart_timeout = protocol.u16le(body, 8)
        p.ota_unit = protocol.u16le(body, 13)
        p.interval = protocol.u16le(body, 18)
        p.ack_enable = body[23] if len(body) > 23 else 0
        if p.ota_unit == 0:
            p.ota_unit = protocol.DEFAULT_OTA_UNIT
        self.params = p
        self.log("设备参数：%s" % p.summary())
        # 通知 APP 就绪
        self._send(
            protocol.assemble_command(
                protocol.OTA_SERVICE_ID, protocol.OTA_CMD_H2D_APP_READY, [protocol.tlv(0x01, b"\x01")]
            )
        )
        self.log("已发送 0x0909 通知 APP 就绪")

    # -- 0x0903 设备请求数据 ----------------------------------------

    def _on_cmd_transfer_req(self, body: bytes) -> None:
        if len(body) < 18:
            self.log("0x0903 响应过短")
            return
        self._set_state(OtaState.TRANSFERRING)

        offset = protocol.u32le(body, 3)
        length = protocol.u32le(body, 10)

        bitmap = None
        if len(body) > 20:
            blen = protocol.u16le(body, 19)
            bitmap = body[21:21 + blen] if blen else b""

        self.log("设备请求数据：offset=%d length=%d bitmap=%dB 分片=%d" % (
            offset, length, len(bitmap) if bitmap is not None else -1, self.params.ota_unit))

        if self.dry_run:
            if bitmap:
                self.log("bitmap hex = %s" % bitmap.hex())
                need = _zero_bit_indices(bitmap, length // (self.params.ota_unit or protocol.DEFAULT_OTA_UNIT) + 1)
                self.log("按 1=已收/0=需发 解析 -> 需下发 %d 包，前 16 个包号：%s" % (
                    len(need), need[:16]))
                need2 = _one_bit_indices(bitmap, length // (self.params.ota_unit or protocol.DEFAULT_OTA_UNIT) + 1)
                self.log("按 1=需发 解析      -> 需下发 %d 包，前 16 个包号：%s" % (
                    len(need2), need2[:16]))
            self.log("dry-run：到此为止，不发送数据")
            return

        # 应答 0x0903（仅当设备要求 ack）
        if self.params.ack_enable == 1:
            self._send(
                struct_pack_frame(protocol.OTA_SERVICE_ID, protocol.OTA_CMD_D2H_TRANSFER_REQ, body)
            )

        if not self.ota_file:
            self.log("没有 OTA 文件，无法应答数据请求")
            return
        try:
            sent = self._send_data_batch(offset, length, bitmap)
        except Exception as e:
            self.log("发送数据失败：%s" % e)
            self._on_error(-1, str(e))
            self._set_state(OtaState.ERROR)
            return
        self._on_progress(min(offset + length, self.ota_file.size), self.ota_file.size)
        self.log("本批发送完成：%d 包 / %d 字节" % (sent, self._write_bytes))

    def _send_data_batch(self, offset: int, length: int, bitmap: Optional[bytes]) -> int:
        """按 bitmap 挑出缺失的包并逐包发出，返回本批包数。"""
        unit = self.params.ota_unit or protocol.DEFAULT_OTA_UNIT
        group_num = length // unit + 1

        need: List[int] = []
        for i in range(group_num):
            if bitmap is None:
                need.append(i)
                continue
            bit_index = i
            byte_index = bit_index // 8
            if byte_index >= len(bitmap):
                need.append(i)  # bitmap 覆盖不到 → 视为缺失
                continue
            bit = (bitmap[byte_index] >> (bit_index % 8)) & 0x01
            if bit == 0:
                need.append(i)

        use_crc = self.remote_status.crc_support
        frames = 0
        with open(self.ota_file.path, "rb") as f:
            for seq, pkt_index in enumerate(need):
                odd = length - pkt_index * unit
                if odd <= 0:
                    break
                o = offset + pkt_index * unit
                size = unit if odd > unit else odd
                f.seek(o)
                payload = f.read(size)
                if not payload:
                    break
                if use_crc:
                    frame = protocol.data_frame_crc(seq % 256, payload)
                else:
                    frame = protocol.data_frame_no_crc(seq % 256, payload)
                self._send(frame)
                self._write_bytes += len(frame)
                frames += 1
                if self.params.interval:
                    time.sleep(self.params.interval / 1000.0)
        return frames

    # -- 0x0905 / 0x0906 / 0x0907 -----------------------------------

    def _on_cmd_pkg_size(self, body: bytes) -> None:
        if len(body) < 14:
            return
        pkg_valid = protocol.u32le(body, 3)
        received = protocol.u32le(body, 10)
        self.log("设备已收：有效=%d 已接收=%d" % (pkg_valid, received))
        if self.ota_file:
            self._on_progress(min(received, self.ota_file.size), self.ota_file.size)

    def _on_cmd_finish(self, body: bytes) -> None:
        if len(body) < 4:
            return
        valid = body[3]
        self.log("设备校验结果：%s" % ("通过" if valid == 1 else "失败"))
        self._send(protocol.assemble_command(protocol.OTA_SERVICE_ID, protocol.OTA_CMD_H2D_ACK_FINISH, None))
        if valid == 1:
            # 对应 Java 的 mIsTranFinish = true：
            # 传输已完成，之后的链路断开（设备重启）不再当错误报。
            self._finished = True
            self._set_state(OtaState.TRANSFERRED)
        else:
            self._on_error(-2, "固件包校验失败")
            self._set_state(OtaState.ERROR)

    def _on_cmd_error(self, body: bytes) -> None:
        if len(body) < 7 or body[0] != 0x7F:
            self.log("0x0907 错误帧")
            return
        err = protocol.u32le(body, 3)
        self.log("设备报错：%d" % err)
        self._on_error(err, "设备返回错误")
        self._set_state(OtaState.ERROR)


def struct_pack_frame(service: int, cmd: int, body: bytes) -> bytes:
    """用给定 body 重新组一帧（用于应答 0x0903 的回显）。"""
    return protocol.assemble_command(service, cmd, [body])


def _zero_bit_indices(bitmap: bytes, group_num: int) -> List[int]:
    """位=0 的包号（语义：1=已收到，0=需要下发）。"""
    out = []
    for i in range(group_num):
        bi = i // 8
        if bi >= len(bitmap):
            out.append(i)
            continue
        if ((bitmap[bi] >> (i % 8)) & 1) == 0:
            out.append(i)
    return out


def _one_bit_indices(bitmap: bytes, group_num: int) -> List[int]:
    """位=1 的包号（另一种可能的语义：1=需要下发）。"""
    out = []
    for i in range(group_num):
        bi = i // 8
        if bi >= len(bitmap):
            out.append(i)
            continue
        if (bitmap[bi] >> (i % 8)) & 1:
            out.append(i)
    return out
