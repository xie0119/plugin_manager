# -*- coding: utf-8 -*-
"""
OTA 固件包头解析 —— 对齐 Java 的 OTAManager.readOTAHeader() 与 ActVersion.getBinMsg()。

三种包头：
  * "AOTA"  → 固件版本取文件 [64, 96) 的 32 字节
  * "ACTT"  → 固件版本取包内 XML 的版本信息，组装成 JSON 字符串
  * 其它    → 固件版本取文件 [12, 16)，VRAM 取文件 [56, 72)
"""

from __future__ import annotations

import json
import os
import re
import struct
from typing import Optional, Tuple

FILE_HEAD_308 = b"AOTA"
FILE_HEAD_238 = b"ACTT"

#: Java LabelUtils.ERROR_CODE
LABEL_ERROR = "ERROR_CODE"


def _byte_to_long(data: bytes) -> int:
    """小端 32 位（Java: Util.byteToLong，名不副实，只取低 4 字节）。"""
    return struct.unpack_from("<I", data, 0)[0]


def _label_data(original: str, target: str) -> str:
    """从 XML 里取 <target>...</target>（Java: LabelUtils.getLabelData）。"""
    clean = original.replace("\t", "").replace("\n", "")
    m = re.search(r"<%s>(.*?)</%s>" % (re.escape(target), re.escape(target)), clean, re.S)
    if not m:
        return LABEL_ERROR
    return m.group(1)


def _gson_escape(s: str) -> str:
    """Gson 默认会做 HTML 转义（< > & = '），这里保持一致。"""
    return (
        s.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("=", "\\u003d")
        .replace("'", "\\u0027")
    )


def _gson_json(head: str, bin_size: int, xml_size: int, version_code: str, board_name: str) -> str:
    """
    复刻 Gson 对 BinJson 的序列化结果（字段顺序 = 声明顺序）。

    BinJson 字段：head_name, bin_size, xml_size, version_code, board_name
    """
    return (
        '{"head_name":"%s","bin_size":%d,"xml_size":%d,"version_code":"%s","board_name":"%s"}'
        % (
            _gson_escape(head),
            bin_size,
            xml_size,
            _gson_escape(version_code),
            _gson_escape(board_name),
        )
    )


class OtaFile:
    """解析出来的 OTA 固件信息。"""

    def __init__(self, path: str):
        self.path = path
        self.size = os.path.getsize(path)
        self.magic: bytes = b""
        self.kind: str = "unknown"
        self.fw_version: str = ""          # 显示用
        self.fw_version_bytes: bytes = b""  # 握手时实际发送的字节
        self.vram: bytes = b"\x00" * 16
        self.board_name: str = ""
        self.version_code: str = ""
        self.version_name: str = ""
        self.version_code_value = None
        self._parse()

    def _parse(self) -> None:
        with open(self.path, "rb") as f:
            head = f.read(4)
            self.magic = head

            if head.upper() == FILE_HEAD_308:
                self.kind = "AOTA(308)"
                f.seek(64)
                raw = f.read(32)
                self.fw_version_bytes = raw
                self.fw_version = raw.split(b"\x00", 1)[0].decode("utf-8", "replace")
                f.seek(64)
                version = f.read(96)
                if len(version) == 96:
                    self.version_name = self.fw_version
                    # This SDK's build_ota_image.py uses <32s24s4xIxI>.
                    self.board_name = version[32:56].split(b"\0", 1)[0].decode("utf-8", "replace")
                    self.version_code_value = struct.unpack_from("<I", version, 60)[0]

            elif head.upper() == FILE_HEAD_238:
                self.kind = "ACTT(238)"
                f.seek(0)
                buf = f.read(24)
                head8 = buf[:8].decode("utf-8", "replace")
                bin_size = _byte_to_long(buf[20:24])

                f.seek(0x20)
                buf2 = f.read(24)
                bin_off = _byte_to_long(buf2[16:20])
                xml_size = _byte_to_long(buf2[20:24])

                f.seek(bin_off)
                xml = f.read(xml_size).decode("utf-8", "replace")
                version_code = _label_data(xml, "version_code")
                board_name = _label_data(xml, "board_name")
                self.version_code = version_code
                self.board_name = board_name
                self.version_name = _label_data(xml, "version_name")
                try:
                    self.version_code_value = int(version_code, 16 if version_code.lower().startswith("0x") else 10)
                except ValueError:
                    pass

                if version_code == LABEL_ERROR or board_name == LABEL_ERROR:
                    self.kind = "ACTT(238) 解析失败"
                    self.fw_version = LABEL_ERROR
                    self.fw_version_bytes = b""
                    return

                self.fw_version = _gson_json(head8, bin_size, xml_size, version_code, board_name)
                self.fw_version_bytes = self.fw_version.encode("utf-8")

            else:
                self.kind = "RAW"
                f.seek(12)
                raw = f.read(4)
                self.fw_version_bytes = raw
                self.fw_version = raw.decode("utf-8", "replace").rstrip("\x00")
                f.seek(56)
                self.vram = f.read(16)

    def summary(self) -> str:
        return "类型=%s  版本=%s  VRAM=%s  大小=%d" % (
            self.kind,
            self.fw_version[:64],
            self.vram.hex(),
            self.size,
        )


def parse_ota_file(path: str) -> OtaFile:
    return OtaFile(path)
