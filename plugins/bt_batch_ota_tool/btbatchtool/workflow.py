"""Sequential watch -> modem workflow. Never replay an unclosed modem job."""
import time
from dataclasses import dataclass

from ..btotatool.transport import TransportError
from .device import Device
from .control import ConnectionSetupError, VersionNotReady
from .settings import modem_command, modem_remove_command, modem_version, watch_version
from .shell import ResultPending
from ..btotatool.device_services import completion_notice


class Retryable(TransportError):
    """No local modem job remains; another bounded attempt is allowed."""


class NeedsAttention(TransportError):
    """Do not replay the upgrade; it may still be flashing."""


class AwaitingWatch(Exception):
    """OTA is accepted; resume verification after a fresh advertisement."""
    def __init__(self, deadline, detail='等待设备重启后重新广播'):
        super().__init__(detail)
        self.deadline, self.detail = deadline, detail


@dataclass
class Checkpoint:
    watch_done: bool = False
    watch_transferred: bool = False
    watch_skipped: bool = False
    modem_submitted: bool = False
    modem_source: str = ''
    modem_target: str = ''
    watch_actual: str = ''
    watch_waiting: bool = False
    watch_deadline: float = 0
    watch_last_check: str = ''
    watch_refresh_services: bool = False
    alert_result: str = ''


class Workflow:
    def __init__(self, settings, device_factory=Device, clock=time.monotonic, sleep=time.sleep,
                 on_version=None, on_information=None):
        self.settings, self.device_factory = settings, device_factory
        self.clock, self.sleep = clock, sleep
        self.checkpoints = {}
        self.on_version = on_version
        self.on_information = on_information
        self.defer_watch_verify = False

    def _verify_watch(self, device, point, target):
        device._keep_verification_link = self.settings.alert_on_success
        if not self.defer_watch_verify:
            return device.verify_watch(target)
        if not point.watch_waiting:
            point.watch_waiting = True
            point.watch_deadline = self.clock() + self.settings.reconnect_timeout
            raise AwaitingWatch(point.watch_deadline)
        remaining = point.watch_deadline - self.clock()
        if remaining <= 0:
            raise NeedsAttention('手表已传包，但安装回连核验超时；最近结果：%s' %
                                 (point.watch_last_check or '未发现重启后的广播'))
        try:
            options = {'fresh_services': True} if point.watch_refresh_services else {}
            actual = device.verify_watch_once(target, timeout=min(8, remaining), **options)
        except Exception as error:
            point.watch_last_check = str(error)
            point.watch_refresh_services = True
            if self.clock() >= point.watch_deadline:
                raise NeedsAttention('手表已传包，但安装回连核验超时：%s' % error) from error
            raise AwaitingWatch(point.watch_deadline, str(error)) from error
        if self.clock() > point.watch_deadline:
            raise NeedsAttention('设备版本返回时已超过回连核验期限，请检查实际版本')
        point.watch_waiting = False
        return actual

    def verify_pending(self, address, log, report):
        """Read back watch version without starting a modem transfer here."""
        point = self.checkpoints[address.upper()]
        if not point.watch_transferred:
            raise NeedsAttention('该设备没有已完成传输的手表固件，请检查任务状态')
        device = self.device_factory(address, self.settings, log, report)
        if self.on_information:
            device.on_information = lambda label, value: self.on_information(address, label, value)
        if self.on_version:
            device.on_version = lambda kind, value: self.on_version(address, kind, value)
        try:
            report('已发现回连广播，正在读取安装后的手表版本')
            point.watch_actual = self._verify_watch(device, point, self.settings.target_watch_version)
            point.watch_done = True
            result = self._watch_result(point)
            return result if self.settings.modem else self._complete(device, point, result)
        finally:
            device.close()

    def run(self, address, log, report):
        cfg = self.settings
        point = self.checkpoints.setdefault(address.upper(), Checkpoint())
        device = self.device_factory(address, cfg, log, report)
        if self.on_information:
            device.on_information = lambda label, value: self.on_information(address, label, value)
        if self.on_version:
            device.on_version = lambda kind, value: self.on_version(address, kind, value)
        try:
            if point.modem_submitted:
                raise NeedsAttention('上一轮 modem 升级尚未结束或结果未知，请检查设备版本后再试')
            target = cfg.target_watch_version
            if cfg.watch_enabled and not point.watch_done:
                if point.watch_transferred:
                    # Validation succeeded previously; a reconnect failure is not
                    # a reason to transmit the same watch package again.
                    report('等待手表安装完成后回连并确认版本')
                    point.watch_actual = self._verify_watch(device, point, target)
                    if not self.defer_watch_verify:
                        self._display_modem_version(device, log)
                    point.watch_done = True
                else:
                    report('查询手表版本')
                    try:
                        current = device.version(2)
                    except ConnectionSetupError as error:
                        current = ''
                        log('版本查询连接未就绪，当前版本未知，继续尝试手表 OTA：%s' % error)
                    except (TransportError, OSError) as error:
                        # Legacy firmware may expose OTA without Cyfit/7B.
                        # A broken 7B byte stream does not prove the BLE link
                        # is lost. Let upgrade_watch try the cached OTA service
                        # on this link before opening a replacement connection.
                        current = ''
                        log('升级前 7B 版本检查不可用，当前版本未知，继续尝试手表 OTA：%s' % error)
                    else:
                        log('手表版本：设备=%s，固件=%s' % (current, target))
                        self._display_modem_version(device, log)
                    point.watch_actual = current
                    if current and cfg.skip_same and watch_version(current) == target:
                        point.watch_done, point.watch_skipped = True, True
                        log('手表已是目标版本 %s，跳过升级' % current)
                    else:
                        report('准备更新手表固件')
                        device.upgrade_watch()
                        point.watch_transferred = True
                        report('固件传输完成，等待手表重启安装')
                        point.watch_actual = self._verify_watch(device, point, target)
                        point.watch_done = True
            elif cfg.watch_enabled:
                # A modem retry must not retransmit an already completed OTA.
                report('手表版本已核对，正在重试 modem 升级')
            else:
                report('未选择手表 OTA 文件，仅执行 modem 升级')
            if not cfg.modem:
                return self._complete(device, point, self._watch_result(point))

            report('查询 modem 状态及版本')
            busy, _, _ = device.status()
            if busy:
                raise NeedsAttention('设备已有 modem 升级任务，请等待完成后再试')
            current = device.version(5).strip()
            if not point.modem_target:
                source = modem_version(cfg.modem_source, current)
                target = modem_version(cfg.modem_target, current)
                point.modem_source, point.modem_target = source, target
            log('modem 版本：设备=%s，要求当前=%s，目标=%s' %
                (current, point.modem_source, point.modem_target))
            if current == point.modem_target:
                return self._complete(device, point,
                    '%s；modem 已是目标版本：%s，跳过升级' % (self._watch_result(point), current))
            if current != point.modem_source:
                return '%s；modem 当前版本不匹配，未升级：实际=%s，要求=%s' % (
                    self._watch_result(point), current, point.modem_source)
            report('上传 modem 补丁包')
            device.upload()
            # Reconnect after file upload and recheck eligibility immediately
            # before the mutating command. No AT query is used for versions.
            busy, _, _ = device.status()
            current = device.version(5).strip() if not busy else ''
            if busy:
                raise NeedsAttention('设备正在执行其他 modem 升级任务，已停止本次操作')
            if current == point.modem_target:
                return self._complete(device, point,
                    '%s；modem 已是目标版本：%s，跳过升级' % (self._watch_result(point), current)
                    + self._remove_modem_package(device))
            if current != point.modem_source:
                return ('%s；上传后 modem 版本不匹配，已停止升级：实际=%s，要求=%s' % (
                    self._watch_result(point), current, point.modem_source)
                        + self._remove_modem_package(device))
            started = self.clock()
            try:
                result = device.command(modem_command(cfg.remote))
            except ResultPending as error:
                point.modem_submitted = True
                log('未收到升级启动确认，正在查询设备进度，请等待')
                return self._monitor(device, point, started, False, log, report)
            if result.code:
                reason = '设备未接受 modem 升级请求（错误码%s）：%s' % (
                    result.code, result.output.strip())
                if self._status(device)[0]:
                    raise NeedsAttention(reason + '；设备仍在升级，保留补丁包并停止自动重试')
                raise Retryable(reason + self._remove_modem_package(device))
            point.modem_submitted = True
            log('modem 升级已启动，正在等待手表传输补丁并写入模组')
            return self._monitor(device, point, started, True, log, report)
        finally:
            device.close()

    def _watch_result(self, point):
        if not self.settings.watch_enabled:
            return '未选择手表 OTA，已跳过'
        if not point.watch_actual:
            return '手表已完成'
        action = '手表同版本跳过' if point.watch_skipped else '手表升级完成'
        return '%s：%s' % (action, point.watch_actual)

    def _modem_result(self, point, actual):
        return '%s；modem 升级完成：%s' % (self._watch_result(point), actual)

    def _complete(self, device, point, result, modem_changed=False):
        if (self.settings.alert_on_success and
                ((point.watch_done and point.watch_transferred) or modem_changed)):
            if not point.alert_result:
                point.alert_result = '；亮屏长震发送结果未确认'
                point.alert_result = completion_notice(lambda: device.completion_alert(), device.log)
            return result + point.alert_result
        return result

    def _display_modem_version(self, device, log):
        if self.on_version:
            try:
                device.version(5)
            except VersionNotReady:
                log('设备暂未提供 modem 版本')
            except Exception as error:
                # Display-only read; modem eligibility is checked again before upgrading.
                device.close()
                log('modem 版本暂未读取：%s' % error)

    def _status(self, device):
        try:
            return device.status()
        except Exception as error:
            device.log('蓝牙连接中断，正在重连并继续查询 modem 升级进度')
            try:
                device.reconnect()
                return device.status()
            except Exception as again:
                raise NeedsAttention('重连失败，无法读取 modem 升级结果，请检查设备：%s' % again) from again

    def _verify_modem(self, device, point):
        try:
            actual = device.wait_version(5, point.modem_target, 60)
        except Exception as error:
            # status=0 established that this local job is over. Eligibility is
            # checked again on the next attempt, never inferred from percent.
            point.modem_submitted = False
            raise Retryable(str(error) + self._remove_modem_package(device)) from error
        point.modem_submitted = False
        result = self._modem_result(point, actual) + self._remove_modem_package(device)
        return self._complete(device, point, result, modem_changed=True)

    def _remove_modem_package(self, device):
        """Only called after this attempt's uploaded package is no longer in use."""
        remote = self.settings.remote
        device.log('正在删除 modem 升级包：' + remote)
        try:
            result = device.command(modem_remove_command(remote))
            # Zephyr's fs shell maps fs_unlink errors to -ENOEXEC (-8),
            # retaining the original errno in its output.
            missing = (result.code == -2 or (result.code == -8 and
                       'Failed to remove %s (-2)' % remote in result.output))
            if result.code != 0 and not missing:
                raise TransportError('错误码%s：%s' % (result.code, result.output.strip()))
        except Exception as error:
            warning = '；modem 升级包删除失败：%s（%s）' % (remote, error)
            device.log(warning[1:])
            return warning
        device.log('modem 升级包已清理：' + remote)
        return ''

    def _monitor(self, device, point, started, accepted, log, report):
        last_send, last_progress = 0, 0
        send_changed, flash_changed, seen_busy = started, None, False
        while True:
            busy, send_progress, progress = self._status(device)
            now = self.clock()
            if not busy:
                if accepted or seen_busy:
                    report('modem 写入已结束，正在读取安装后的版本')
                    return self._verify_modem(device, point)
                report('正在等待设备确认 modem 升级请求')
                # A lost execute ACK and cached 0/100 do not prove this command
                # ran. Cancel through Shell to settle the queued command first.
                if now - started >= self.settings.send_timeout:
                    return self._cancel(device, point, '命令提交结果未知', log, report)
                self.sleep(1)
                continue
            seen_busy = True
            if send_progress != last_send:
                log('发送到 modem：%d%%' % send_progress)
                last_send, send_changed = send_progress, now
            # Transfer completion starts the upgrade timer even while flash=0
            # (verification/reboot). A query reply alone never resets a timer.
            if flash_changed is None and (send_progress == 100 or progress > 0):
                flash_changed = now
            if progress != last_progress:
                log('modem 烧写进度：%d%%' % progress)
                last_progress, flash_changed = progress, now
            flashing = flash_changed is not None
            report('modem 传输 %d%% | 写入 %d%%' % (send_progress, progress))
            timeout = (now - flash_changed >= self.settings.wait_timeout if flashing else
                       now - send_changed >= self.settings.send_timeout)
            if timeout:
                reason = ('modem 烧写进度连续不变超时' if flashing else
                          '发送到 modem 进度连续不变超时（发送%d%%）' % send_progress)
                return self._cancel(device, point, reason, log, report)
            self.sleep(1)

    def _cancel(self, device, point, reason, log, report):
        report(reason + '，请求取消')
        log(reason + '；正在请求取消本次 modem 升级')
        try:
            result = device.command('xfn modem fota cancel')
        except ResultPending:
            result = None
            log('未收到取消确认，正在等待设备结束本次升级')
        except Exception as error:
            raise NeedsAttention('取消无法确认，保留当前升级任务：%s' % error) from error
        if result and result.code == -16:
            raise NeedsAttention(reason + '；modem 正在写入固件，设备拒绝取消，请等待结束后检查版本')
        if result and result.code not in (0, -2):
            raise NeedsAttention('设备拒绝取消升级（错误码%s）：%s' % (result.code, result.output.strip()))
        deadline = self.clock() + 75
        while self.clock() < deadline:
            busy, _, _ = self._status(device)
            if not busy:
                # The command may have succeeded just as cancellation arrived.
                actual = ''
                try:
                    actual = device.version(5).strip()
                except Exception:
                    device.close()
                point.modem_submitted = False
                cleanup_warning = self._remove_modem_package(device)
                if actual == point.modem_target:
                    return self._modem_result(point, actual) + cleanup_warning
                raise Retryable(reason + '；本轮任务已退出，可重新尝试' + cleanup_warning)
            report('等待设备结束升级 %g/75 秒，当前任务仍在运行' %
                   min(int(self.clock() - (deadline - 75)), 75))
            self.sleep(1)
        raise NeedsAttention(reason + '；取消请求发出75秒后设备仍在升级，请检查设备，已停止自动重试')
