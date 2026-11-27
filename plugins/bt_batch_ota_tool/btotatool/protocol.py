# -*- coding: utf-8 -*-
"""
TLV 帧编解码 —— 逐字节对齐 ActBluetoothUtils 的 Java 实现。

Java 参考实现：
    TransportManager.assembleTLV()   / assembleCommand()
    OTAManager.assembleTLV()         / assembleCommand()
    OTAManager.readSubTLVs()

帧结构（命令与响应同形）：

    +----------+--------+--------+---------------------+------------------+
    | service |  cmd   |  0x80  |  len (2B, 小端)    |  body (len 字节) |
    |  1 字节  | 1 字节 |  1 字节 |                    |                  |
    +----------+--------+--------+---------------------+------------------+

    body 内是若干子 TLV：type(1) + len(2, 小端) + value(len)

注意：OTAManager 解析 0x09/0x01 响应时，子 TLV 的长度字段也是 2 字节小端，
      但其中 versionCode(0x08) 固定占 4 字节、featureSupport(0x09) 固定 1 字节，
      这类"定长字段"在解析时按 Java 的硬编码偏移处理，见 ota.py。
"""

from __future__ import annotations

import struct
from typing import Iterable, List, Sequence, Tuple

# ---------------------------------------------------------------- 常量

#: service id
WS_SERVICE_ID = 0x01  # 手表通用服务（设置时间 / 发文件 / 发消息）
OTA_SERVICE_ID = 0x09  # OTA 服务

#: WS 服务下的 command id
WS_CMD_H2D_SET_TIME = 0x01
WS_CMD_H2D_SEND_FILE = 0x02
WS_CMD_H2D_SEND_MESSAGE = 0x03
WS_CMD_D2H_RESULT = 0x04

#: OTA 服务下的 command id
OTA_CMD_CHECK_STATUS = 0x01  # H2D 握手（checkRemoteStatus）
OTA_CMD_REQUEST_PARAMS = 0x02  # H2D 请求设备参数
OTA_CMD_D2H_TRANSFER_REQ = 0x03  # D2H 设备请求数据
OTA_CMD_H2D_DATA = 0x04  # H2D 数据帧（无 CRC32）
OTA_CMD_D2H_PKG_SIZE = 0x05  # D2H 包有效大小 / 已收大小
OTA_CMD_H2D_ACK_FINISH = 0x06  # H2D 回应 0x06
OTA_CMD_D2H_ERROR = 0x07  # D2H 错误
OTA_CMD_H2D_APP_READY = 0x09  # H2D 通知 APP 就绪
OTA_CMD_H2D_DATA_CRC = 0x0B  # H2D 数据帧（带 CRC32）
OTA_CMD_D2H_AUDIO = 0x7D  # D2H 音频数据

#: 超级 TLV 的 type
SUPER_TLV_TYPE = 0x80

#: OTA 握手成功时设备返回的 errCode
OTA_ERR_OK = 100000
#: 283F 约定：bin 文件错误
OTA_ERR_BIN_FILE = 110000

#: 设备默认 OTA 分片大小（Java: FRAME_SIZE）
DEFAULT_OTA_UNIT = 256


# ---------------------------------------------------------------- 基础读写


def u16le(buf: bytes, off: int = 0) -> int:
    """读 2 字节小端无符号数。"""
    return struct.unpack_from("<H", buf, off)[0]


def u32le(buf: bytes, off: int = 0) -> int:
    """读 4 字节小端无符号数（Java: Utils.byteArrayToInt）。"""
    return struct.unpack_from("<I", buf, off)[0]


def tlv(type_: int, value: bytes = b"") -> bytes:
    """
    构造一个 TLV：type(1) + len(2, 小端) + value。

    对应 Java: assembleTLV(int type, int length, byte[] value)
    """
    return struct.pack("<BH", type_ & 0xFF, len(value)) + value


def assemble_command(service_id: int, cmd_id: int, sub_tlvs: Sequence[bytes] | None = None) -> bytes:
    """
    构造一条命令帧。

    对应 Java: assembleCommand(int serverID, int commandID, ArrayList<byte[]> subTLVs)
    """
    body = b"".join(sub_tlvs) if sub_tlvs else b""
    return struct.pack("<BB", service_id & 0xFF, cmd_id & 0xFF) + tlv(SUPER_TLV_TYPE, body)


def parse_tlvs(buf: bytes) -> List[Tuple[int, bytes]]:
    """
    解析 body 里的子 TLV 列表，返回 [(type, value), ...]。

    长度字段固定 2 字节小端 —— 与 Java readSubTLVs / 0x0901 解析一致。
    """
    out: List[Tuple[int, bytes]] = []
    i = 0
    n = len(buf)
    while i + 3 <= n:
        type_ = buf[i]
        length = u16le(buf, i + 1)
        i += 3
        if i + length > n:
            # 尾巴被截断，剩余整体当作最后一个 value，避免静默丢数据
            out.append((type_, buf[i:]))
            break
        out.append((type_, buf[i:i + length]))
        i += length
    return out


# ---------------------------------------------------------------- 数据帧


def data_frame_no_crc(seq: int, payload: bytes) -> bytes:
    """
    无 CRC32 的 OTA 数据帧。

    对应 Java: buffer = new byte[6 + contentLen]
        [0]=0x09 [1]=0x04 [2]=0x80 [3..4]=len [5]=seq [6..]=data
    其中 len = contentLen + 1（1 字节 seq）
    """
    value = struct.pack("<B", seq & 0xFF) + payload
    return struct.pack("<BBBH", OTA_SERVICE_ID, OTA_CMD_H2D_DATA, SUPER_TLV_TYPE, len(value)) + value


def data_frame_crc(seq: int, payload: bytes) -> bytes:
    """
    带 CRC32 的 OTA 数据帧。

    对应 Java: buffer = new byte[6 + 4 + contentLen]
        [0]=0x09 [1]=0x0B [2]=0x80 [3..4]=len [5]=seq [6..9]=CRC32(小端) [10..]=data
    其中 len = contentLen + 1 + 4，且 CRC32 只对 payload 计算（不含 seq、不含 crc 本身）。
    """
    import zlib

    crc = zlib.crc32(payload) & 0xFFFFFFFF
    value = struct.pack("<BI", seq & 0xFF, crc) + payload
    return struct.pack("<BBBH", OTA_SERVICE_ID, OTA_CMD_H2D_DATA_CRC, SUPER_TLV_TYPE, len(value)) + value


# ---------------------------------------------------------------- 帧解析


class Frame:
    """一帧解析结果。"""

    __slots__ = ("service", "cmd", "body")

    def __init__(self, service: int, cmd: int, body: bytes):
        self.service = service
        self.cmd = cmd
        self.body = body

    def tlvs(self) -> List[Tuple[int, bytes]]:
        return parse_tlvs(self.body)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return "Frame(service=0x%02X, cmd=0x%02X, body=%dB)" % (self.service, self.cmd, len(self.body))


def parse_frame(buf: bytes) -> Tuple[Frame | None, int]:
    """
    从缓冲区头部解析一帧，返回 (帧, 消耗字节数)。数据不足时返回 (None, 0)。

    帧结构见模块文档。
    """
    if len(buf) < 5:
        return None, 0
    service = buf[0]
    cmd = buf[1]
    if buf[2] != SUPER_TLV_TYPE:
        # 不是合法帧头：跳过 1 字节重新同步
        return None, 1
    length = u16le(buf, 3)
    total = 5 + length
    if len(buf) < total:
        return None, 0
    return Frame(service, cmd, buf[5:total]), total


def hexs(data: bytes) -> str:
    """十六进制可读串（空格分隔）。"""
    return " ".join("%02X" % b for b in data)
