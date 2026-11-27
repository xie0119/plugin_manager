"""Version 3 debug file RPC, over the log GATT service (not the OTA service)."""
import os
import struct
import threading
import zlib
from pathlib import Path

from .transport import (BleTransport, TransportError, LOG_BLE_SERVICE,
                        LOG_BLE_WRITE, LOG_BLE_READ)

CHUNK = 160
BATCH_CHUNK = 4096
BATCH_COMMAND = 0x3e
DOWNLOAD_COMMAND = 0x3f


class DebugBleTransport(BleTransport):
    def __init__(self, address, on_log=None):
        try:
            super().__init__(address, LOG_BLE_SERVICE, LOG_BLE_WRITE,
                             LOG_BLE_READ, on_log=on_log, write_gap=0,
                             strict_service=True, services=[LOG_BLE_SERVICE])
            self._logln('文件通道：MTU=%d，最大写入=%d' %
                        (self._client.mtu_size, min(max(20, self._client.mtu_size - 3), 240)))
        except Exception:
            if hasattr(self, '_loop_thread'):
                self.close()
            raise

    def _run(self, coro):
        future = self._submit(coro)
        try:
            return future.result(timeout=20)
        except Exception:
            future.cancel()
            raise

    def _resolve_chars(self, service_uuid, write_uuid, notify_uuid):
        # Never fall back to an OTA characteristic on old firmware.
        service = next((s for s in self._client.services
                        if s.uuid.lower() == service_uuid.lower()), None)
        wanted = {write_uuid.lower(), notify_uuid.lower()}
        if service is None or not wanted.issubset(
                {c.uuid.lower() for c in service.characteristics}):
            raise TransportError('手表没有文件调试所需的日志服务，请先升级配套固件')
        super()._resolve_chars(service_uuid, write_uuid, notify_uuid)

    def write(self, data):
        # Match OTA's whole-frame WinRT writes when the negotiated MTU permits.
        # Some backends keep the characteristic's cached write size at 20.
        mtu = getattr(self._client, 'mtu_size', 23) or 23
        limit = min(max(20, mtu - 3), 240)
        for start in range(0, len(data), limit):
            super().write(data[start:start + limit])


class FileDebugClient:
    def __init__(self, transport, cancel=None, progress=None):
        self.transport = transport
        self.cancel = cancel or threading.Event()
        self.progress = progress or (lambda done, total: None)
        self.sequence = 0
        self.broken = False
        self.version = None
        self.upload_chunk = CHUNK
        self.upload_window = 1
        self.max_request = 200
        self.batch_upload = False
        self.download_chunk = CHUNK
        self.download_command = 0x33
        self.download_negotiated = False

    def check_cancel(self):
        if self.cancel.is_set():
            raise InterruptedError('文件操作已取消')

    def _send_request(self, command, data=b'', check_cancel=True):
        if self.broken:
            raise TransportError('通道已失步，请重新连接')
        if check_cancel:
            self.check_cancel()
        self.sequence = (self.sequence + 1) & 0xffff
        body = struct.pack('<H', self.sequence) + data
        if len(body) > self.max_request:
            raise ValueError('文件请求太长')
        try:
            self.transport.write(struct.pack('<BBBH', 0x1d, command, 0x81, len(body)) + body)
        except Exception:
            self.broken = True
            raise
        return self.sequence

    def _read_reply(self, command, sequence):
        try:
            # A previous real-time logger may finish with ACK/TX_END before RPC.
            for _ in range(16):
                head = self.transport.read_exactly(5, timeout=10)
                service, cmd, kind, length = struct.unpack('<BBBH', head)
                maximum = max(512, self.download_chunk + 14) if command == DOWNLOAD_COMMAND else 512
                if service != 0x1d or kind != 0x81 or length > maximum:
                    raise TransportError('文件响应头无效')
                reply = self.transport.read_exactly(length, timeout=10)
                if cmd in (0x10, 0xf0, 0xf1):
                    continue
                if cmd != command or len(reply) < 6:
                    raise TransportError('文件响应命令不匹配')
                status, request_id = struct.unpack_from('<iH', reply)
                if request_id != sequence:
                    raise TransportError('文件响应序号不匹配')
                break
            else:
                raise TransportError('未收到文件响应')
        except Exception:
            self.broken = True
            raise
        if status:
            messages = {-2: '路径不存在', -17: '文件或临时文件已存在，上传不覆盖旧文件',
                        -16: '设备文件操作正忙', -22: '路径或参数无效',
                        -28: '手表存储空间不足', -5: '文件读写或校验失败',
                        -39: '目录非空，不能删除', -13: '权限不足或文件正被占用'}
            raise OSError(status, messages.get(status, '手表文件操作失败 (%d)' % status))
        return reply[6:]

    def rpc(self, command, data=b'', check_cancel=True):
        sequence = self._send_request(command, data, check_cancel)
        return self._read_reply(command, sequence)

    def info(self):
        self.batch_upload = False
        try:
            data = self.rpc(0x38, b'\x02')
        except OSError as error:
            if error.errno not in (-22, -95):
                raise
            data = None
        if data is not None and len(data) == 6:
            version, chunk, window, flags, command = struct.unpack('<BHBBB', data)
            if (version != 3 or not 1 <= chunk <= BATCH_CHUNK or window != 1 or
                    flags != 1 or command != BATCH_COMMAND):
                raise TransportError('文件批量上传参数无效')
            self.version, self.upload_chunk, self.upload_window = version, chunk, window
            self.max_request = chunk + 10
            self.batch_upload = True
            log = getattr(self.transport, '_logln', None)
            if log:
                log('文件高速上传：每批%d字节，连续发送，整批CRC和一次确认' % chunk)
            return self.version
        if data is not None and (len(data) not in (1, 4) or data[0] not in (2, 3)):
            raise TransportError('文件批量上传能力响应无效')
        try:
            data = self.rpc(0x38, b'\x01')
        except OSError as error:
            if error.errno not in (-22, -95):
                raise
            data = self.rpc(0x38)  # Older firmware rejects optional selectors.
        if len(data) not in (1, 4) or data[0] not in (2, 3):
            raise TransportError('不支持的文件调试协议版本，请升级配套固件')
        self.version = data[0]
        self.upload_chunk, self.upload_window, self.max_request = CHUNK, 1, 200
        if len(data) == 4:
            chunk, window = struct.unpack_from('<HB', data, 1)
            # Bound all outstanding requests below the existing 512-byte ring.
            if not 1 <= chunk <= 229 or not 1 <= window <= 2:
                raise TransportError('文件上传参数无效')
            self.upload_chunk, self.upload_window = chunk, window
            self.max_request = max(200, chunk + 6)
        log = getattr(self.transport, '_logln', None)
        if log:
            log('文件上传：每块%d字节，每批%d块' %
                (self.upload_chunk, self.upload_window))
        return self.version

    def path(self, value):
        # Send the exact user-selected filesystem path; no filename or disk mapping.
        import re
        if not re.match(r'^/[A-Za-z0-9_-]+:/', value):
            raise ValueError('请输入完整手表路径，例如 /NOR:/ 或 /NAND:/')
        if '\\' in value or any(part in ('.', '..') for part in value.split('/')):
            raise ValueError('路径不能包含反斜杠、. 或 ..')
        encoded = value.encode('utf-8')
        if not encoded or len(encoded) >= 128 or b'\0' in encoded:
            raise ValueError('手表路径必须少于128字节，且不能包含NUL')
        return encoded

    def release(self):
        if not self.broken:
            self.rpc(0x37, check_cancel=False)

    def list_dir(self, path):
        self.rpc(0x30, self.path(path))
        entries = []
        try:
            while True:
                data = self.rpc(0x31)
                if not data:
                    return entries
                if len(data) < 6:
                    raise TransportError('目录响应不完整')
                entries.append((data[5:].decode('utf-8'), data[0] == 1,
                                struct.unpack_from('<I', data, 1)[0]))
        finally:
            self.release()

    def download_info(self):
        if self.download_negotiated:
            return
        try:
            data = self.rpc(0x38, b'\x03')
        except OSError as error:
            if error.errno not in (-22, -95):
                raise
            data = None
        if data is not None and len(data) == 5:
            version, chunk, command, flags = struct.unpack('<BHBB', data)
            if version != 3 or not 1 <= chunk <= BATCH_CHUNK or command != DOWNLOAD_COMMAND or flags != 1:
                raise TransportError('文件批量下载参数无效')
            self.download_chunk, self.download_command = chunk, command
        elif data is not None and (len(data) not in (1, 4) or data[0] not in (2, 3)):
            raise TransportError('文件批量下载能力响应无效')
        self.download_negotiated = True
        log = getattr(self.transport, '_logln', None)
        if log:
            log(('文件高速下载：每批%d字节，连续返回，整批CRC校验' if self.download_command == DOWNLOAD_COMMAND
                 else '文件下载：每块%d字节，兼容旧固件') % self.download_chunk)

    def download(self, remote, local):
        target = Path(local)
        # Exclusive temporary output; never leave a partial file under final name.
        partial = target.with_name(target.name + '.btpart')
        opened = False
        try:
            self.download_info()
            data = self.rpc(0x32, self.path(remote))
            opened = True
            if len(data) != 4:
                raise TransportError('下载大小响应无效')
            size, = struct.unpack('<I', data)
            with partial.open('xb') as output:
                try:
                    offset = 0
                    while offset < size:
                        count = min(self.download_chunk, size - offset)
                        data = self.rpc(self.download_command, struct.pack('<IH', offset, count))
                        if len(data) != 8 + count:
                            raise TransportError('下载分块长度错误')
                        received, crc = struct.unpack_from('<II', data)
                        block = data[8:]
                        if received != offset or zlib.crc32(block) & 0xffffffff != crc:
                            raise TransportError('下载分块校验失败')
                        output.write(block)
                        offset += count
                        self.progress(offset, size)
                    output.flush()
                    os.fsync(output.fileno())
                except BaseException:
                    # Windows requires closing before deleting.
                    output.close()
                    partial.unlink()
                    raise
            try:
                self.check_cancel()
                os.replace(str(partial), str(target))
            except BaseException:
                partial.unlink()
                raise
            self.progress(size, size)
            return size
        finally:
            if opened:
                self.release()

    def upload(self, local, remote, overwrite=False):
        with open(local, 'rb') as source:
            size = os.fstat(source.fileno()).st_size
            if size > 0x7fffffff:
                raise ValueError('文件超过2GB限制')
            if self.version is None:
                self.info()
            if overwrite:
                self.require_management()
            self.rpc(0x3d if overwrite else 0x34, struct.pack('<I', size) + self.path(remote))
            try:
                offset, crc = 0, 0
                while offset < size:
                    self.check_cancel()
                    if self.batch_upload:
                        count = min(self.upload_chunk, size - offset)
                        block = source.read(count)
                        if len(block) != count:
                            raise OSError('上传期间本地文件大小发生变化')
                        block_crc = zlib.crc32(block) & 0xffffffff
                        data = self.rpc(BATCH_COMMAND, struct.pack('<II', offset, block_crc) + block)
                        if len(data) != 4 or struct.unpack('<I', data)[0] != offset + count:
                            self.broken = True
                            raise TransportError('上传整批确认错误')
                        crc = zlib.crc32(block, crc) & 0xffffffff
                        offset += count
                        self.progress(offset, size)
                        continue
                    blocks, end = [], offset
                    for _ in range(self.upload_window):
                        if end == size:
                            break
                        count = min(self.upload_chunk, size - end)
                        block = source.read(count)
                        if len(block) != count:
                            raise OSError('上传期间本地文件大小发生变化')
                        blocks.append((end, block))
                        end += count
                    # Send a bounded batch first, then consume every ordered ACK.
                    # Never retry a write whose acceptance is unknown.
                    self.check_cancel()
                    pending = []
                    for start, block in blocks:
                        sequence = self._send_request(
                            0x35, struct.pack('<I', start) + block, check_cancel=False)
                        pending.append((sequence, start + len(block)))
                    failure = None
                    for sequence, expected in pending:
                        try:
                            data = self._read_reply(0x35, sequence)
                        except OSError as error:
                            if self.broken:
                                raise
                            if failure is None:
                                failure = error
                            continue  # Drain later ACKs before releasing the file.
                        if len(data) != 4 or struct.unpack('<I', data)[0] != expected:
                            if failure is None:
                                failure = TransportError('上传分块确认错误')
                    if failure is not None:
                        raise failure
                    for _, block in blocks:
                        crc = zlib.crc32(block, crc) & 0xffffffff
                    offset = end
                    self.progress(offset, size)
                self.check_cancel()
                self.rpc(0x36, struct.pack('<I', crc))
                self.progress(size, size)
                return size
            finally:
                self.release()

    def require_management(self):
        if self.version is None:
            self.info()
        if self.version < 3:
            raise TransportError('文件增删改查需要协议v3，请升级手表固件')

    def create_file(self, remote):
        self.rpc(0x34, struct.pack('<I', 0) + self.path(remote))
        try:
            self.rpc(0x36, struct.pack('<I', 0))
        finally:
            self.release()

    def stat(self, remote):
        self.require_management()
        data = self.rpc(0x39, self.path(remote))
        if len(data) != 5:
            raise TransportError('文件信息响应不完整')
        return data[0] == 1, struct.unpack_from('<I', data, 1)[0]

    def delete(self, remote):
        self.require_management()
        self.rpc(0x3a, self.path(remote))

    def rename(self, source, target):
        self.require_management()
        source, target = self.path(source), self.path(target)
        self.rpc(0x3b, bytes([len(source)]) + source + target)

    def mkdir(self, remote):
        self.require_management()
        self.rpc(0x3c, self.path(remote))


class FileDebugSession:
    """One connection per debug window; never replay a failed operation."""
    def __init__(self, address, cancel=None, progress=None, on_log=None,
                 factory=DebugBleTransport):
        self.address = address
        self.cancel = cancel or threading.Event()
        self.progress = progress
        self.on_log = on_log
        self.factory = factory
        self.transport = None
        self.client = None
        self.lock = threading.Lock()

    @property
    def connected(self):
        if self.transport is None:
            return False
        backend = getattr(self.transport, '_client', None)
        return bool(getattr(backend, 'is_connected', True))

    def _close(self):
        transport, self.transport, self.client = self.transport, None, None
        if transport:
            transport.close()

    def close(self):
        with self.lock:
            self._close()

    def execute(self, action):
        with self.lock:
            try:
                if not self.connected or (self.client and self.client.broken):
                    self._close()
                    self.transport = self.factory(self.address, on_log=self.on_log)
                    self.client = FileDebugClient(self.transport, self.cancel, self.progress)
                    try:
                        self.client.info()
                    except Exception:
                        self._close()
                        raise
                return action(self.client)
            except (OSError, ValueError):
                # A remote filesystem error or a completed cancellation does
                # not invalidate the byte stream. Keep the connection usable.
                raise
            except Exception:
                self._close()
                raise
            finally:
                if self.client and (self.client.broken or not self.connected):
                    self._close()
