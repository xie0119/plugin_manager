"""
插件 ``.bin`` 二进制包：格式定义、序列化、校验与安全解析。

设计目标：稳定可扩展的容器格式（magic/version/manifest/payload/CRC），
与具体宿主（GUI）解耦，仅依赖标准库。
"""

from __future__ import annotations

import json
import struct
import tempfile
import zlib
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import IntFlag
from pathlib import Path, PurePosixPath
from typing import Any, Final, Literal, Mapping

from core.logging_config import get_plugin_logger

_logger = get_plugin_logger("plugin_bin")

# ---------------------------------------------------------------------------
# 常量与限制（生产环境可按需在单处调参）
# ---------------------------------------------------------------------------

MAGIC: Final[bytes] = b"PLUGBIN\x00"
HEADER_SIZE: Final[int] = 64
BIN_FORMAT_VERSION: Final[int] = 1
MANIFEST_API_VERSION: Final[int] = 1

MAX_BIN_FILE_BYTES: Final[int] = 64 * 1024 * 1024
MAX_MANIFEST_BYTES: Final[int] = 256 * 1024
MAX_PAYLOAD_BYTES: Final[int] = MAX_BIN_FILE_BYTES - HEADER_SIZE - MAX_MANIFEST_BYTES
MAX_FILES_IN_PAYLOAD: Final[int] = 8192
MAX_SINGLE_FILE_BYTES: Final[int] = 32 * 1024 * 1024
MAX_PATH_BYTES: Final[int] = 4096


class PluginBinFlags(IntFlag):
    """载荷标志位（预留压缩/签名等）。"""

    NONE = 0
    # PAYLOAD_ZSTD = 1 << 0  # 预留
    # SIGNED = 1 << 1


class PluginBinError(Exception):
    """与插件 bin 相关的可恢复错误基类。"""


class PluginBinCorruptError(PluginBinError):
    """文件损坏、长度不符、CRC 失败等。"""


class PluginBinSecurityError(PluginBinError):
    """路径非法、疑似目录穿越等。"""


class PluginBinValidationError(PluginBinError):
    """manifest 与载荷不一致或缺字段。"""


@dataclass(frozen=True, slots=True)
class PluginBinHeader:
    """固定 64 字节头（小端）。"""

    magic: bytes
    format_version: int
    flags: int
    manifest_len: int
    payload_len: int
    crc32: int

    def pack(self) -> bytes:
        if len(self.magic) != 8:
            raise ValueError("magic must be 8 bytes")
        reserved = b"\x00" * (HEADER_SIZE - 28)
        return struct.pack(
            "<8sIIIII",
            self.magic,
            self.format_version,
            self.flags,
            self.manifest_len,
            self.payload_len,
            self.crc32,
        ) + reserved

    @classmethod
    def unpack(cls, blob: bytes) -> PluginBinHeader:
        if len(blob) < HEADER_SIZE:
            raise PluginBinCorruptError(
                f"truncated header: need {HEADER_SIZE} bytes, got {len(blob)}"
            )
        head = blob[:28]
        magic, fmt, flg, mlen, plen, crc = struct.unpack("<8sIIIII", head)
        return cls(
            magic=magic,
            format_version=fmt,
            flags=flg,
            manifest_len=mlen,
            payload_len=plen,
            crc32=crc,
        )


@dataclass(slots=True)
class PluginBinManifest:
    """manifest（JSON）对应结构。未知字段写入 ``extra`` 以便前向兼容。"""

    plugin_id: str
    plugin_name: str
    version: str
    entry: str
    package_type: Literal["single_file", "directory"]
    source_layout: Literal["flat_py", "directory_tree"]
    created_at: str
    api_version: int
    optional_dependencies: list[str] = field(default_factory=list)
    description: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_json_bytes(self) -> bytes:
        payload: dict[str, Any] = {
            "plugin_id": self.plugin_id,
            "plugin_name": self.plugin_name,
            "version": self.version,
            "entry": self.entry,
            "package_type": self.package_type,
            "source_layout": self.source_layout,
            "created_at": self.created_at,
            "api_version": self.api_version,
            "optional_dependencies": list(self.optional_dependencies),
            "description": self.description,
        }
        payload.update(self.extra)
        text = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return text.encode("utf-8")

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> PluginBinManifest:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise PluginBinValidationError("manifest is not valid UTF-8") from e
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise PluginBinValidationError("manifest JSON parse failed") from e
        if not isinstance(data, dict):
            raise PluginBinValidationError("manifest root must be an object")

        required = (
            "plugin_id",
            "plugin_name",
            "version",
            "entry",
            "package_type",
            "source_layout",
            "created_at",
            "api_version",
        )
        missing = [k for k in required if k not in data]
        if missing:
            raise PluginBinValidationError(f"manifest missing keys: {missing}")

        known_keys = set(required) | {
            "optional_dependencies",
            "description",
        }
        extra = {k: v for k, v in data.items() if k not in known_keys}

        pt = data["package_type"]
        sl = data["source_layout"]
        if pt not in ("single_file", "directory"):
            raise PluginBinValidationError("invalid package_type")
        if sl not in ("flat_py", "directory_tree"):
            raise PluginBinValidationError("invalid source_layout")

        deps = data.get("optional_dependencies", [])
        if not isinstance(deps, list) or not all(isinstance(x, str) for x in deps):
            raise PluginBinValidationError("optional_dependencies must be list[str]")

        return cls(
            plugin_id=str(data["plugin_id"]),
            plugin_name=str(data["plugin_name"]),
            version=str(data["version"]),
            entry=str(data["entry"]),
            package_type=pt,
            source_layout=sl,
            created_at=str(data["created_at"]),
            api_version=int(data["api_version"]),
            optional_dependencies=list(deps),
            description=str(data.get("description", "")),
            extra=extra,
        )


def _crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def build_payload_file_table(files: Mapping[str, bytes]) -> bytes:
    """将 ``{相对 posix 路径: 内容}`` 序列化为载荷字节。

    结构：``file_count`` + 重复 ``path_len, path_utf8, content_len, content``。
    """

    if len(files) > MAX_FILES_IN_PAYLOAD:
        raise PluginBinError(f"too many files: {len(files)} > {MAX_FILES_IN_PAYLOAD}")
    parts: list[bytes] = [struct.pack("<I", len(files))]
    for rel, content in sorted(files.items(), key=lambda x: x[0]):
        if len(rel) > MAX_PATH_BYTES:
            raise PluginBinError(f"path too long: {rel!r}")
        pbytes = rel.encode("utf-8")
        if len(content) > MAX_SINGLE_FILE_BYTES:
            raise PluginBinError(
                f"file too large: {rel!r} ({len(content)} > {MAX_SINGLE_FILE_BYTES})"
            )
        parts.append(struct.pack("<I", len(pbytes)))
        parts.append(pbytes)
        parts.append(struct.pack("<I", len(content)))
        parts.append(content)
    blob = b"".join(parts)
    if len(blob) > MAX_PAYLOAD_BYTES:
        raise PluginBinError(
            f"payload too large: {len(blob)} > {MAX_PAYLOAD_BYTES}"
        )
    return blob


def parse_payload_file_table(payload: bytes) -> dict[str, bytes]:
    """解析 ``build_payload_file_table`` 生成的载荷。"""

    if len(payload) > MAX_PAYLOAD_BYTES:
        raise PluginBinCorruptError("payload exceeds configured maximum")

    pos = 0
    if len(payload) < 4:
        raise PluginBinCorruptError("payload too short for file_count")
    (count,) = struct.unpack_from("<I", payload, pos)
    pos += 4
    if count > MAX_FILES_IN_PAYLOAD:
        raise PluginBinCorruptError("file_count absurdly large")

    out: dict[str, bytes] = {}
    for _ in range(count):
        if pos + 4 > len(payload):
            raise PluginBinCorruptError("truncated path_len")
        (plen,) = struct.unpack_from("<I", payload, pos)
        pos += 4
        if plen > MAX_PATH_BYTES:
            raise PluginBinCorruptError("path_len too large")
        if pos + plen > len(payload):
            raise PluginBinCorruptError("truncated path bytes")
        path = payload[pos : pos + plen].decode("utf-8")
        pos += plen
        if pos + 4 > len(payload):
            raise PluginBinCorruptError("truncated content_len")
        (clen,) = struct.unpack_from("<I", payload, pos)
        pos += 4
        if clen > MAX_SINGLE_FILE_BYTES:
            raise PluginBinCorruptError("content_len too large")
        if pos + clen > len(payload):
            raise PluginBinCorruptError("truncated content")
        body = payload[pos : pos + clen]
        pos += clen
        out[path] = body
    if pos != len(payload):
        raise PluginBinCorruptError("trailing garbage in payload")
    return out


def _is_safe_relative_posix(path: str) -> bool:
    """禁止 ``..``、绝对路径、空段、Windows 盘符路径及非规范 ``//`` 等。"""

    if not path or path.startswith(("/", "\\")):
        return False
    if len(path.encode("utf-8")) > MAX_PATH_BYTES:
        return False
    try:
        p = PurePosixPath(path)
    except ValueError:
        return False
    if str(p) != path:
        return False
    parts = p.parts
    if not parts:
        return False
    for seg in parts:
        if seg in ("", ".", ".."):
            return False
        if seg.endswith(":"):  # 'C:' 等
            return False
    return True


def validate_file_table_paths(files: Mapping[str, bytes]) -> None:
    """校验相对路径键；发现非法项抛出 ``PluginBinSecurityError``。"""

    for rel in files:
        rel_slash = rel.replace("\\", "/")
        if rel_slash != rel:
            raise PluginBinSecurityError(f"backslash not allowed in path key: {rel!r}")
        if not _is_safe_relative_posix(rel_slash):
            raise PluginBinSecurityError(f"unsafe path: {rel!r}")


def validate_manifest_against_files(
    manifest: PluginBinManifest, files: Mapping[str, bytes]
) -> None:
    """manifest.entry 必须存在；单文件/目录与文件表一致性检查。"""

    if not manifest.entry:
        raise PluginBinValidationError("empty entry")
    entry = manifest.entry.replace("\\", "/")
    if entry not in files:
        raise PluginBinValidationError(f"entry file not in payload: {entry!r}")

    if manifest.package_type == "single_file":
        if len(files) != 1:
            raise PluginBinValidationError(
                "single_file package must contain exactly one file"
            )
        if manifest.source_layout != "flat_py":
            raise PluginBinValidationError("single_file expects source_layout flat_py")
    else:
        if manifest.source_layout != "directory_tree":
            raise PluginBinValidationError(
                "directory package expects source_layout directory_tree"
            )


def pack_plugin_bin(
    manifest: PluginBinManifest,
    files: Mapping[str, bytes],
    *,
    flags: int = int(PluginBinFlags.NONE),
) -> bytes:
    """打包为完整 ``.bin`` 字节（含头、manifest、payload、CRC）。"""

    validate_file_table_paths(files)
    validate_manifest_against_files(manifest, files)

    manifest_bytes = manifest.to_json_bytes()
    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
        raise PluginBinError("manifest too large")

    payload_bytes = build_payload_file_table(files)
    total_body = manifest_bytes + payload_bytes
    if HEADER_SIZE + len(total_body) > MAX_BIN_FILE_BYTES:
        raise PluginBinError("total bin size exceeds limit")

    crc = _crc32(total_body)
    header = PluginBinHeader(
        magic=MAGIC,
        format_version=BIN_FORMAT_VERSION,
        flags=flags,
        manifest_len=len(manifest_bytes),
        payload_len=len(payload_bytes),
        crc32=crc,
    )
    # 头内 CRC 占位：规范为「manifest+payload 的 CRC」
    return header.pack() + manifest_bytes + payload_bytes


def parse_plugin_bin_blob(data: bytes) -> tuple[PluginBinHeader, PluginBinManifest, dict[str, bytes]]:
    """解析完整 ``.bin`` 字节，校验 magic/长度/CRC，返回头、manifest、文件表。"""

    if len(data) > MAX_BIN_FILE_BYTES:
        raise PluginBinCorruptError("bin file larger than allowed maximum")
    if len(data) < HEADER_SIZE:
        raise PluginBinCorruptError("file too small")

    header = PluginBinHeader.unpack(data)
    if header.magic != MAGIC:
        raise PluginBinCorruptError("bad magic — not a PLUGBIN container")
    if header.format_version != BIN_FORMAT_VERSION:
        raise PluginBinCorruptError(
            f"unsupported bin format_version {header.format_version}"
        )
    if header.manifest_len == 0 or header.manifest_len > MAX_MANIFEST_BYTES:
        raise PluginBinCorruptError("invalid manifest_len")
    if header.payload_len > MAX_PAYLOAD_BYTES:
        raise PluginBinCorruptError("invalid payload_len")

    need = HEADER_SIZE + header.manifest_len + header.payload_len
    if need != len(data):
        raise PluginBinCorruptError(
            f"size mismatch: expected {need} bytes, got {len(data)}"
        )

    mstart = HEADER_SIZE
    mend = mstart + header.manifest_len
    pstart = mend
    pend = pstart + header.payload_len

    manifest_bytes = data[mstart:mend]
    payload_bytes = data[pstart:pend]
    crc_expect = _crc32(manifest_bytes + payload_bytes)
    if crc_expect != header.crc32:
        raise PluginBinCorruptError(
            f"CRC mismatch: expected {header.crc32:08x}, got {crc_expect:08x}"
        )

    manifest = PluginBinManifest.from_json_bytes(manifest_bytes)
    if manifest.api_version != MANIFEST_API_VERSION:
        _logger.warning(
            "manifest api_version %s != %s (continuing)",
            manifest.api_version,
            MANIFEST_API_VERSION,
        )

    files = parse_payload_file_table(payload_bytes)
    validate_file_table_paths(files)
    validate_manifest_against_files(manifest, files)
    return header, manifest, files


def parse_plugin_bin_file(path: Path) -> tuple[PluginBinHeader, PluginBinManifest, dict[str, bytes]]:
    """从路径读取并解析 ``.bin`` 文件。"""

    p = path.resolve()
    try:
        data = p.read_bytes()
    except OSError as e:
        raise PluginBinError(f"cannot read {p}: {e}") from e
    return parse_plugin_bin_blob(data)


def extract_files_to_directory(
    files: Mapping[str, bytes],
    target_dir: Path,
    *,
    logger: Any | None = None,
) -> None:
    """将文件表解包到 ``target_dir``（创建子目录）。路径已预先校验为相对安全路径。"""

    log = logger or _logger
    target_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        rel = rel.replace("\\", "/")
        dest = (target_dir / rel).resolve()
        try:
            dest.relative_to(target_dir.resolve())
        except ValueError as e:
            raise PluginBinSecurityError(f"path escapes target_dir: {rel!r}") from e
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
        log.debug("extracted %s (%s bytes)", rel, len(content))


def make_temp_extract_root(plugin_id: str) -> Path:
    """为插件解包创建临时目录（调用方负责删除整棵树）。"""

    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in plugin_id)[:64]
    return Path(
        tempfile.mkdtemp(prefix=f"plg_{safe}_", suffix="_bin")
    ).resolve()


def remove_tree_quiet(path: Path, *, logger: Any | None = None) -> None:
    """删除解包目录；失败仅记日志。"""

    import shutil

    log = logger or _logger
    try:
        shutil.rmtree(path, ignore_errors=False)
    except OSError as e:
        log.warning("failed to remove extract dir %s: %s", path, e)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


__all__ = [
    "MAGIC",
    "HEADER_SIZE",
    "BIN_FORMAT_VERSION",
    "MANIFEST_API_VERSION",
    "MAX_BIN_FILE_BYTES",
    "PluginBinFlags",
    "PluginBinError",
    "PluginBinCorruptError",
    "PluginBinSecurityError",
    "PluginBinValidationError",
    "PluginBinHeader",
    "PluginBinManifest",
    "build_payload_file_table",
    "parse_payload_file_table",
    "validate_file_table_paths",
    "validate_manifest_against_files",
    "pack_plugin_bin",
    "parse_plugin_bin_blob",
    "parse_plugin_bin_file",
    "extract_files_to_directory",
    "make_temp_extract_root",
    "remove_tree_quiet",
    "utc_now_iso",
]
