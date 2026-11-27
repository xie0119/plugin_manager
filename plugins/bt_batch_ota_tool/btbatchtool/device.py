"""Hardware adapter; job policy lives in workflow.py, not in BLE callbacks."""
import time
import threading

from ..btotatool import transport as tp
from ..btotatool.file_debug import FileDebugSession
from ..btotatool.ota import OtaClient, OtaState
from .control import BoundedBleTransport, VersionNotReady, connect_control, read_version, execute_shell
from .settings import watch_version
from ..btotatool.ble_scan import forget_device
from ..btotatool.device_services import read_device_version, read_device_information, connection_unusable


class Device:
    def __init__(self, address, settings, log, report):
        self.address, self.settings = address, settings
        self.log, self.report = log, report
        self.on_version = None
        self.io = None
        self._fresh_services = False
        self._control_only = False
        self.on_information = None
        self._information_read = False
        self._keep_verification_link = False

    def _transport_log(self, message):
        if message.startswith(('[BLE 清理 ', '[BLE 建连 ', '0x80 ')):
            self.log(message)

    def control(self, timeout=20):
        if self.io is None:
            self.log('正在连接设备')
            options = {'fresh_services': True} if self._fresh_services else {}
            if self._control_only:
                options['control_only'] = True
            else:
                if self.settings.modem:
                    options['include_file'] = True
                if not self.settings.watch_enabled:
                    options['include_ota'] = False
            self.io = connect_control(self.address, self.settings.shine, self._transport_log,
                                      timeout=timeout, **options)
        self.io._operation_timeout = timeout
        return self.io

    def close(self):
        io, self.io = self.io, None
        if io:
            io.close()

    def reconnect(self):
        self.close()
        return self.control()

    def version(self, kind):
        actual = self.read_actual_version(kind)
        if self.on_version:
            self.on_version(kind, actual)
        if not self._information_read:
            self._information_read = True
            io = self.control()
            read_device_information(io, self.log, self.information)
            if connection_unusable(io):
                self.close()
        return actual

    def information(self, label, value):
        if self.on_information:
            self.on_information(label, value)

    def read_actual_version(self, kind, timeout=8):
        deadline = None
        def connect():
            nonlocal deadline
            if deadline is None:
                io = self.control()
                deadline = time.monotonic() + timeout
                return io
            return self.control(timeout=max(.001, min(5, deadline - time.monotonic())))
        return read_device_version(connect, self.close, kind, read_version, timeout,
                                   self.log, self.information)

    def completion_alert(self):
        self.control(timeout=3).completion_alert(timeout=3)

    def status(self):
        return self.control().query_fota()

    def command(self, command):
        return execute_shell(self.control(), command)

    def upload(self):
        io = self.control()
        self.log('正在将 modem 补丁上传到设备')
        session = FileDebugSession(self.address, on_log=lambda message: None,
            factory=lambda address, on_log=None: io.open_file_channel(),
            progress=lambda done, total: self.report('上传modem 补丁到设备 %.1f%%' %
                                                     (100 * done / total if total else 0)))
        try:
            session.execute(lambda client: client.upload(self.settings.modem_file,
                                self.settings.remote, overwrite=self.settings.overwrite))
        except Exception as error:
            # An uncertain file stream is never replayed on this connection.
            self.close()
            raise tp.TransportError('modem 补丁上传失败：%s' % error) from error
        finally:
            session.close()
        self.log('modem 补丁已上传到设备，文件校验通过：%s' % self.settings.remote)
        self.log('modem 上传完成，复用现有蓝牙连接查询版本并启动升级')

    def upgrade_watch(self):
        io, self.io = self.io, None
        self.log('正在准备固件传输')
        if io is not None:
            try:
                io.use_ota_service()
            except Exception as error:
                io.close()
                io = None
                self.log('原连接无法切换 OTA，释放后重新连接：%s' % error)
            else:
                self.log('复用版本查询连接，已切换到 OTA 服务')
        if io is None:
            io = BoundedBleTransport(self.address, service_uuid=tp.OTA_BLE_SERVICE,
                                 write_uuid=tp.OTA_BLE_WRITE, notify_uuid=tp.OTA_BLE_READ,
                                 strict_service=True, auto_subscribe=False,
                                 services=[tp.OTA_BLE_SERVICE],
                                 on_log=self._transport_log)
        errors = []

        def ota_error(code, message):
            text = '设备固件升级失败（错误码%s）：%s' % (code, message)
            errors.append(text)
            self.log(text)

        client = OtaClient(io, on_log=lambda message: self.log(message)
                          if message.startswith('设备参数：') else None,
            on_progress=lambda done, total: self.report('发送设备固件 %.1f%%' %
                                                        (100 * done / total if total else 0)),
            on_error=ota_error)
        try:
            client.set_file(self.settings.firmware)
            client.prepare()  # Subscribe only after the 7B skip decision.
            deadline = time.monotonic() + 600
            while client.state not in (OtaState.TRANSFERRED, OtaState.ERROR, OtaState.IDLE):
                if time.monotonic() >= deadline:
                    raise tp.TransportError('设备 OTA 超时（10分钟）')
                time.sleep(.1)
            if client.state != OtaState.TRANSFERRED:
                raise tp.TransportError(errors[-1] if errors else '设备固件传输中断，设备未完成校验')
        finally:
            client.close()
            io.close()
            forget_device(self.address)
            if client._thread:
                client._thread.join(timeout=2)
        self.log('设备固件传输校验通过，等待设备重启并完成本地安装')

    def verify_watch_once(self, target, timeout=8, fresh_services=False):
        """Called only after the scheduler observes this watch advertising again."""
        self.close()
        self._fresh_services = fresh_services
        self._control_only = True
        deadline = time.monotonic() + timeout
        verified = False
        try:
            io = self.control(timeout=min(5, timeout))
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise tp.TransportError('回连连接超时，尚未读取版本')
            io._operation_timeout = min(3, remaining)
            actual = self.read_actual_version(2, timeout=min(3, remaining))
            if self.on_version:
                self.on_version(2, actual)
            if time.monotonic() > deadline:
                raise tp.TransportError('版本应答超过本次核验期限')
            if watch_version(actual) != watch_version(target):
                raise tp.TransportError('设备版本尚未更新：实际=%s，目标=%s' % (actual, target))
            self.log('安装后版本确认：%s' % actual)
            verified = True
            return actual
        finally:
            if not (verified and self._keep_verification_link):
                self.close()
            self._fresh_services = False
            self._control_only = False

    def verify_watch(self, target):
        self.close()
        self.log('等待设备安装完成后回连，超时=%g秒；目标版本=%s' %
                 (self.settings.reconnect_timeout, target))
        return self.wait_version(2, target, self.settings.reconnect_timeout, normalize=watch_version)

    def wait_version(self, kind, target, timeout=60, normalize=lambda value: value.strip()):
        started = time.monotonic()
        deadline = started + timeout
        finished = threading.Event()
        label = '回连核验' if kind == 2 else 'modem 核验'

        def progress():
            elapsed = min(int(time.monotonic() - started), timeout)
            self.report('%s %g/%g 秒' % (label, elapsed, timeout))

        def tick():
            # BLE connection/query calls may block; keep UI time advancing independently.
            while not finished.wait(1):
                progress()
                if time.monotonic() >= deadline:
                    break

        progress()
        ticker = threading.Thread(target=tick, name='version-wait-%s' % self.address, daemon=True)
        ticker.start()
        previous_options = self._control_only, self._fresh_services
        if kind == 5:
            # Keep the status-query link if it still works. A replacement link
            # only needs Cyfit, and a modem reboot does not change the GATT table.
            self._control_only = True
        try:
            return self._wait_version(kind, target, deadline, normalize)
        finally:
            self._control_only, self._fresh_services = previous_options
            finished.set()
            # No old wait message may overwrite the next stage's status.
            ticker.join()

    def _wait_version(self, kind, target, deadline, normalize):
        last = '尚未读取'
        modem = kind == 5
        connect_timeout, query_timeout, retry_interval = (5, 3, .5) if modem else (20, 8, 2)
        last_logged = None

        def waiting(message):
            nonlocal last_logged
            if modem and message != last_logged:
                self.log(message)
                last_logged = message

        while time.monotonic() < deadline:
            try:
                io = self.control(timeout=min(connect_timeout, deadline - time.monotonic()))
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                budget = min(query_timeout, remaining)
                io._operation_timeout = budget
                previous_deadline = getattr(io, '_operation_deadline', None)
                try:
                    # Include the GATT write in the response budget, rather
                    # than allowing a blocked write and response wait to add up.
                    if modem:
                        io._operation_deadline = time.monotonic() + budget
                    actual = self.read_actual_version(kind, timeout=budget)
                finally:
                    if modem:
                        io._operation_deadline = previous_deadline
                last = actual
                if self.on_version:
                    self.on_version(kind, actual)
                if time.monotonic() <= deadline and normalize(actual) == normalize(target):
                    self.log('版本确认：%s' % actual)
                    return actual
                if modem:
                    self._fresh_services = False
                    waiting('modem 版本尚未更新：实际=%s，目标=%s；保留连接继续核验' % (actual, target))
            except VersionNotReady as error:
                last = str(error)
                if modem:
                    self._fresh_services = False
                    waiting('modem 版本暂未就绪，保留连接等待设备更新版本')
                else:
                    self.close()
            except Exception as error:
                last = str(error)
                self.close()
                if modem:
                    self._fresh_services = True
                    waiting('modem 核验连接或查询失败，下次重连刷新服务：%s' % error)
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(retry_interval, remaining))
        label = '设备回连核验' if kind == 2 else 'modem 版本核验'
        raise tp.TransportError('%s超时：目标版本=%s；最近结果=%s' % (label, target, last))
