"""Device Information Service reads and the independent CYFIT 0x80 indication."""
import time
import struct

from .transport import TransportError

DIS_SERVICE = '0000180a-0000-1000-8000-00805f9b34fb'
DIS_FIELDS = {
    0x2A27: '主板硬件版本', 0x2A24: '型号', 0x2A29: '制造商',
    0x2A25: '序列号', 0x2A23: 'System ID', 0x2A50: 'PnP ID',
}


def bluetooth_uuid(value):
    return '0000%04x-0000-1000-8000-00805f9b34fb' % value


def dis_text(raw):
    return bytes(raw).decode('utf-8').strip('\0 \t\r\n')


def dis_display(kind, raw):
    raw = bytes(raw)
    if kind == 0x2A23:
        if len(raw) != 8:
            raise ValueError('System ID 长度应为 8')
        return raw[::-1].hex().upper()
    if kind == 0x2A50:
        if len(raw) != 7:
            raise ValueError('PnP ID 长度应为 7')
        return '来源 %d / VID %04X / PID %04X / 产品版本 %04X' % struct.unpack('<BHHH', raw)
    return dis_text(raw)


def connection_unusable(io):
    if getattr(io, '_dis_invalid', False) is True:
        return True
    backend = getattr(io, '_client', None)
    try:
        return backend is not None and not backend.is_connected
    except OSError:
        return True


def read_device_version(connect, close, kind, legacy, timeout=8, log=None, information=None):
    log = log or (lambda message: None)
    information = information or (lambda label, value: None)
    io = connect()
    deadline = time.monotonic() + timeout
    short = 0x2A26 if kind == 2 else 0x2A28
    label = '手表固件版本' if kind == 2 else 'modem 版本'
    source = 'DIS 0x180A/0x%04X' % short
    try:
        raw = io.read_dis(short, timeout=min(1.2, timeout))
        if raw is None:
            raise ValueError('服务或特征缺失')
        actual = dis_text(raw)
        if not actual:
            raise ValueError('返回空值')
    except Exception as error:
        reason = '%s %s，回退 7B%02X' % (source, error, kind)
        log(reason)
        information(label + '回退原因', reason)
        if connection_unusable(io):
            close()
        left = deadline - time.monotonic()
        if left <= 0:
            raise TransportError('版本查询期限已到：' + reason) from error
        io = connect()
        source = '7B%02X' % kind
        information(label + '来源', source)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TransportError('版本查询重连已超过本次期限')
        previous_timeout = getattr(io, '_operation_timeout', None)
        previous_deadline = getattr(io, '_operation_deadline', None)
        io._operation_timeout = (min(remaining, previous_timeout)
            if isinstance(previous_timeout, (int, float)) else remaining)
        io._operation_deadline = (min(deadline, previous_deadline)
            if isinstance(previous_deadline, (int, float)) else deadline)
        try:
            actual = legacy(io, kind, timeout=remaining)
        except Exception:
            information(label, '')
            log('%s 未读取；来源=%s' % (label, source))
            raise
        finally:
            io._operation_timeout = previous_timeout
            io._operation_deadline = previous_deadline
    information(label + '来源', source)
    information(label, actual)
    log('%s=%s；来源=%s' % (label, actual, source))
    return actual


def read_device_information(io, log, information):
    deadline = time.monotonic() + 2
    for kind, label in DIS_FIELDS.items():
        left = deadline - time.monotonic()
        if left <= 0:
            break
        try:
            raw = io.read_dis(kind, timeout=min(.4, left))
            if raw:
                value = dis_display(kind, raw)
                information(label, value)
                log('DIS 0x%04X %s=%s' % (kind, label, value))
        except Exception as error:
            log('DIS %s 未读取：%s' % (label, error))
            if connection_unusable(io):
                break


def alert_packet(action=1):
    if action not in (0, 1):
        raise ValueError('0x80 动作必须为 0 或 1')
    packet = bytearray(16)
    packet[0:2] = bytes((0x80, action))
    packet[15] = sum(packet[:15]) & 255
    return bytes(packet)


def validate_alert_reply(reply, action=1):
    if len(reply) != 16:
        raise TransportError('0x80 应答长度错误：%d，要求 16' % len(reply))
    if reply[0] != 0x80:
        raise TransportError('0x80 应答命令错误')
    if reply[1] not in (0, 1):
        raise TransportError('0x80 应答动作无效')
    if (sum(reply[:15]) & 255) != reply[15]:
        raise TransportError('0x80 应答校验错误')
    if reply[1] != action:
        raise TransportError('0x80 应答动作不匹配')
    if any(reply[3:15]):
        raise TransportError('0x80 应答保留字段非零')
    errors = {1: '参数无效', 2: '设备忙', 3: '输出错误'}
    if reply[2]:
        raise TransportError('0x80 设备返回 %s（状态=%d）' % (errors.get(reply[2], '未知错误'), reply[2]))


def completion_notice(send, log):
    # This optional indication must never turn a verified installation into failure.
    try:
        send()
    except Exception as error:
        message = '亮屏长震指令未确认：%s（不自动重发）' % error
    else:
        message = '亮屏长震：设备已确认'
    log(message)
    return '；' + message
