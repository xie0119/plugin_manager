"""Validate the whole job before opening any device connection."""
import math
import re
from dataclasses import dataclass
from pathlib import Path

from ..btotatool.file_debug import FileDebugClient
from ..btotatool.otafile import parse_ota_file
from .shell import validate_command


def name_rule(value):
    value = value.strip()
    names = frozenset(n.strip() for n in value.split(',') if n.strip())
    if not names:
        raise ValueError('请输入蓝牙名前缀或英文逗号分隔的完整名称')
    return value, names if ',' in value else None


def watch_version(value):
    # 7B 02 is VERSION_BUILD_STR; AOTA appends a build timestamp to this string.
    value = value.strip()
    match = re.fullmatch(r'(\d+\.\d+\.\d+)(?:_\d{10,14})?', value)
    return match.group(1) if match else value


def version_from_filename(path):
    """Example: ..._V1_16_23_.bin -> 1.16.23; never guess from package headers."""
    match = re.search(r'_V(\d+)_(\d+)_(\d+)(?=_|\.|$)', Path(path).name, re.I)
    return '.'.join(match.groups()) if match else ''


def modem_versions_from_filename(path):
    """Read source/target revisions from an unambiguous delta package name."""
    match = re.fullmatch(r'v([0-9A-Za-z]{3})_v([0-9A-Za-z]{3})_delta\.par',
                        Path(path.strip()).name, re.I)
    return match.groups() if match else None


def modem_version(value, device_version):
    """Expand a three-character revision using this device's full version."""
    value = value.strip()
    if len(value) != 3:
        return value
    match = re.fullmatch(r'(.*?_V)[^.]+(\..+)', device_version.strip())
    if not match:
        raise ValueError('无法补全 modem 版本 %s：设备版本缺少 _V版本.后缀 格式（%s）' %
                         (value, device_version))
    return match.group(1) + value + match.group(2)


def modem_command(remote):
    FileDebugClient.path(None, remote)
    if not remote.startswith(('/NOR:/', '/NAND:/')) or remote.endswith('/'):
        raise ValueError('手表目标路径需为 /NOR:/文件名 或 /NAND:/文件名')
    if '"' in remote or any(ord(c) < 32 or ord(c) == 127 for c in remote):
        raise ValueError('目标路径不能含引号或控制字符')
    command = 'xfn modem fota "%s"' % remote
    validate_command(command)
    return command


def modem_remove_command(remote):
    # Apply exactly the same full-path validation as the upload/start command.
    modem_command(remote)
    command = 'fs rm "%s"' % remote
    validate_command(command)
    return command


@dataclass(frozen=True)
class Settings:
    prefix: str = 'XFN_'
    min_rssi: int = -80
    max_attempts: int = 3
    firmware: str = ''
    local_version: str = ''
    reconnect_timeout: float = 120
    skip_same: bool = True
    shine: bool = False
    modem: bool = False
    modem_file: str = ''
    remote: str = '/NOR:/modem_update.bin'
    overwrite: bool = True
    modem_source: str = ''
    modem_target: str = ''
    send_timeout: float = 20
    wait_timeout: float = 20
    concurrency: int = 3
    alert_on_success: bool = True

    def validate(self):
        name_rule(self.prefix)
        if not isinstance(self.min_rssi, int) or not -127 <= self.min_rssi <= 0:
            raise ValueError('最低 RSSI 必须为 -127 到 0 的整数')
        if not isinstance(self.max_attempts, int) or not 1 <= self.max_attempts <= 100:
            raise ValueError('最大尝试次数为 1～100，包含首次尝试')
        if type(self.concurrency) is not int or not 1 <= self.concurrency <= 20:
            raise ValueError('同时台数必须为1到20的整数')
        if not self.watch_enabled and not self.modem:
            raise ValueError('请选择手表 OTA 文件或启用 modem 升级并选择补丁包')
        if self.watch_enabled:
            info = parse_ota_file(self.firmware)
            if not info.fw_version_bytes:
                raise ValueError('OTA 文件没有有效包头信息')
            if not re.fullmatch(r'\d+\.\d+\.\d+', self.target_watch_version):
                raise ValueError('请填写固件版本号，例如 1.16.23；支持从文件名 _V1_16_23_ 自动提取')
            if not math.isfinite(self.reconnect_timeout) or self.reconnect_timeout <= 0:
                raise ValueError('回连超时必须为大于 0 的秒数')
            if info.kind == 'RAW' or '解析失败' in info.kind:
                raise ValueError('请选择包含字符串版本号的 AOTA/ACTT 手表升级包')
        if self.modem:
            source = Path(self.modem_file)
            if not source.is_file() or not 0 < source.stat().st_size <= 256 * 65536:
                raise ValueError('请选择非空且不超过 16MiB 的 modem 补丁包')
            modem_command(self.remote)
            if not self.modem_source.strip() or not self.modem_target.strip():
                raise ValueError('请填写 modem 当前版本和目标版本')
            if self.modem_source.strip() == self.modem_target.strip():
                raise ValueError('modem 当前版本和目标版本不能相同')
            for timeout in (self.send_timeout, self.wait_timeout):
                if not math.isfinite(timeout) or timeout <= 0:
                    raise ValueError('发送超时、升级超时必须为大于 0 的秒数')
        return self

    @property
    def watch_enabled(self):
        return bool(self.firmware.strip())

    @property
    def target_watch_version(self):
        return self.local_version.strip() or version_from_filename(self.firmware)
