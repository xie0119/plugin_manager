"""Continuous discovery with bounded concurrent devices and per-device retries."""
from collections import deque
import threading
import time

from ..btotatool.transport import scan_ble
from .settings import name_rule
from .discovery import ScanNames
from .control import _connection_lock
from .workflow import NeedsAttention, AwaitingWatch, Workflow


def select_devices(devices, rule, minimum, attempted):
    prefix, names = name_rule(rule)
    unique = {}
    for address, name, rssi in devices:
        key = address.upper()
        if (key in attempted or not name or
                not (name in names if names is not None else name.startswith(prefix)) or
                not isinstance(rssi, (int, float)) or not -127 <= rssi <= 0 or rssi < minimum):
            continue
        if key not in unique or rssi > unique[key][2]:
            unique[key] = (address, name, rssi)
    return sorted(unique.values(), key=lambda value: value[2], reverse=True)


class BatchRunner:
    def __init__(self, settings, workflow, event, scanner=scan_ble, retry_delay=5, clock=None):
        if type(settings.concurrency) is not int or not 1 <= settings.concurrency <= 20:
            raise ValueError('同时台数必须为1到20的整数')
        self.settings, self.workflow, self.event = settings, workflow, event
        self.scanner, self.retry_delay = scanner, retry_delay
        self.stop = threading.Event()
        self.attempted = set()
        self.concurrency = settings.concurrency
        self._queue = deque()
        self._workers = []
        self._verifier = None
        self._verified_ready = deque()
        self._continuations = set()
        self._last_scan = 0.0
        self.clock = clock or time.monotonic
        self._scan_names = ScanNames(settings.prefix, lambda: self.clock())
        self._filter_reasons = {}
        self._waiting = {}
        self._waiting_lock = threading.RLock()
        self._resume_queue = set()
        self._waiting_limit = max(4, self.concurrency)
        self._new_queue_limit = max(4, self.concurrency)
        self._last_wait_report = 0.0
        if isinstance(workflow, Workflow):
            workflow.defer_watch_verify = True

    def _device(self, address, name, rssi):
        total = self.settings.max_attempts
        for attempt in range(1, total + 1):
            def report(message, final=False):
                status = message if final else '第%d/%d次 | %s' % (attempt, total, message)
                self.event('device', (address, name, rssi, status))

            def log(message):
                self.event('log', '[%s] %s' % (address, message))
                report(message)

            report('开始处理')
            try:
                result = self.workflow.run(address, log, report)
            except AwaitingWatch as pending:
                with self._waiting_lock:
                    self._waiting[address.upper()] = dict(address=address, name=name, rssi=rssi,
                        deadline=pending.deadline, detail=pending.detail, queued=False)
                report('已传包，等待安装回连；已释放传输名额', final=True)
                return
            except NeedsAttention as error:
                log(str(error))
                report('结果未知，需要检查设备：%s' % error, final=True)
                return
            except Exception as error:
                log('本次失败：%s' % error)
                if attempt == total:
                    report('升级失败（已尝试%d次）：%s' % (total, error), final=True)
                    return
                if self.stop.is_set():
                    report('升级失败，已停止重试：%s' % error, final=True)
                    return
                retry_started = time.monotonic()
                while not self.stop.is_set():
                    elapsed = time.monotonic() - retry_started
                    remaining = self.retry_delay - elapsed
                    if remaining <= 0:
                        break
                    report('等待重试 %d/%g 秒；原因：%s' % (int(elapsed), self.retry_delay, error))
                    self.stop.wait(min(1, remaining))
                if self.stop.is_set():
                    report('升级失败，已停止重试：%s' % error, final=True)
                    return
            else:
                report(result, final=True)
                return

    def _reap(self):
        self._workers = [worker for worker in self._workers if worker.is_alive()]
        if self._verifier and not self._verifier.is_alive():
            self._verifier = None
        with self._waiting_lock:
            while self._verified_ready:
                row = self._verified_ready.popleft()
                self._continuations.add(row[0].upper())
                self._queue.appendleft(row)

    def _verify_device(self, address, name, rssi, pending):
        probe_logs = deque(maxlen=8)
        def report(message):
            self.event('device', (address, name, rssi, message))
        def log(message):
            # Decide whether this is still an ordinary wait before showing
            # short-probe cancellation/cleanup diagnostics to the operator.
            self.event('diagnostic', '[%s] %s' % (address, message))
            probe_logs.append(message)
        def emit_log(message):
            self.event('log', '[%s] %s' % (address, message))
        try:
            verify = getattr(self.workflow, 'verify_pending', self.workflow.run)
            result = verify(address, log, report)
        except AwaitingWatch as error:
            pending.update(deadline=error.deadline, detail=error.detail, queued=False,
                           diagnostics=list(probe_logs))
            with self._waiting_lock:
                self._waiting[address.upper()] = pending
            emit_log(self._waiting_message(pending))
            report(self._waiting_message(pending))
        except Exception as error:
            for message in list(probe_logs):
                emit_log(message)
            emit_log('回连核验失败：%s' % error)
            report('结果未知，需要检查设备：%s' % error)
        else:
            for message in list(probe_logs):
                if not message.startswith(('[BLE 清理 ', '[BLE 建连 ')):
                    emit_log(message)
            if self.settings.modem:
                report('%s；等待 modem 升级名额' % result)
                with self._waiting_lock:
                    self._verified_ready.append((address, name, rssi))
            else:
                report(result)

    def _needs_scan(self):
        waiting = self._waiting_snapshot()
        if any(not item['queued'] for item in waiting.values()) and self._verifier is None:
            # A returning watch must not wait for the next firmware transfer.
            interval = 6 if self._workers else 1
            return not self._last_scan or self.clock() - self._last_scan >= interval
        if self.stop.is_set() or self._new_device_room(waiting) <= 0:
            return False
        if self._workers:
            return not self._last_scan or self.clock() - self._last_scan >= 6
        if self._queue:
            return False
        # Waiting for installation does not hold a worker or a BLE connection.
        return not self._last_scan or self.clock() - self._last_scan >= 1

    def _waiting_snapshot(self):
        with self._waiting_lock:
            return dict(self._waiting)

    def _new_device_room(self, waiting):
        # Keep a small discovery queue even while all transfer slots are busy.
        # Returning watches and modem continuations have their own priority.
        priority = self._resume_queue | self._continuations
        queued = sum(row[0].upper() not in priority for row in self._queue)
        return max(0, min(self._waiting_limit - len(waiting), self._new_queue_limit - queued))

    def _maintain_waiting(self):
        now = self.clock()
        with self._waiting_lock:
            for key, pending in list(self._waiting.items()):
                if not pending['queued'] and now >= pending['deadline']:
                    del self._waiting[key]
                    self.event('device', (pending['address'], pending['name'], pending['rssi'],
                        '已传包，回连核验超时；最近探测：%s' % pending['detail']))
                    for message in pending.get('diagnostics', ()):
                        self.event('log', '[%s] 回连结束时的诊断：%s' % (pending['address'], message))
            if now - self._last_wait_report >= 1:
                self._last_wait_report = now
                for pending in self._waiting.values():
                    if pending['queued']:
                        continue
                    self.event('device', (pending['address'], pending['name'], pending['rssi'],
                        self._waiting_message(pending, now)))

    def _waiting_message(self, pending, now=None):
        now = self.clock() if now is None else now
        timeout = self.settings.reconnect_timeout
        elapsed = min(timeout, max(0, timeout - (pending['deadline'] - now)))
        return '等待安装回连 %d/%g 秒' % (int(elapsed), timeout)

    def _scan_devices(self):
        # Defer without blocking the scheduler or consuming the scan cooldown.
        # Existing links can still transfer while this short scan owns the slot.
        if not _connection_lock.acquire(blocking=False):
            return None
        try:
            return self._scan_devices_locked()
        finally:
            _connection_lock.release()

    def _scan_devices_locked(self):
        shown = set()
        waiting = self._waiting_snapshot()
        background = bool(self._workers)
        allow_new = not self.stop.is_set() and self._new_device_room(waiting) > 0
        def matches(row):
            row = self._scan_names.resolve(row)
            return row[0].upper() in waiting or (allow_new and bool(select_devices([row],
                self.settings.prefix, self.settings.min_rssi, self.attempted)))
        def discovered(row):
            eligible = matches(row)
            row = self._scan_names.resolve(row)
            address, name, rssi = row
            if eligible and address.upper() not in shown:
                shown.add(address.upper())
                message = '已发现重启后的广播，等待核验' if address.upper() in waiting else '已发现，等待连接'
                self.event('device', (address, name, rssi, message))
        # Collect the whole short window when busy or awaiting a return, so a
        # new device cannot end the scan before an older watch advertises.
        return self.scanner(timeout=1.5 if background else (2 if waiting else 6), match=matches,
                            target_count=0 if background or waiting else self.concurrency,
                            quiet_timeout=1.0, cancel=None if self.stop.is_set() else self.stop,
                            # New names can be in scan responses; use active
                            # scanning when discovering new devices, even busy.
                            passive=background and not allow_new,
                            on_device=discovered,
                            on_log=lambda message: self.event('log', message))

    def _accept_scan(self, devices):
        devices = [self._scan_names.resolve(row) for row in devices]
        now = self.clock()
        with self._waiting_lock:
            returning = []
            for address, name, rssi in devices:
                key = address.upper()
                pending = self._waiting.get(key)
                if pending and not pending['queued'] and now < pending['deadline']:
                    pending['queued'] = True
                    self._resume_queue.add(key)
                    returning.append((address, pending['name'], rssi))
            # Oldest deadlines are verified first, ahead of newly found devices.
            returning.sort(key=lambda row: self._waiting[row[0].upper()]['deadline'])
            self._queue.extendleft(reversed(returning))
            room = self._new_device_room(self._waiting)
        if self.stop.is_set():
            return
        excluded = self.attempted | set(self._waiting_snapshot()) | self._resume_queue
        prefix, names = name_rule(self.settings.prefix)
        no_name, wrong_name, weak, handled, eligible = 0, 0, 0, 0, 0
        for address, name, rssi in devices:
            key = address.upper()
            if key in excluded:
                handled += 1
            elif not name:
                no_name += 1
            elif not (name in names if names is not None else name.startswith(prefix)):
                wrong_name += 1
            elif not isinstance(rssi, (int, float)) or not -127 <= rssi <= 0 or rssi < self.settings.min_rssi:
                weak += 1
                reason = 'RSSI=%s，未达到门槛 %s' % (rssi, self.settings.min_rssi)
                if self._filter_reasons.get(key) != 'weak':
                    self.event('log', '[%s] 扫描未入队：名称=%s，%s' % (address, name, reason))
                self._filter_reasons[key] = 'weak'
                self.event('device', (address, name, rssi, '未入队：' + reason))
            else:
                eligible += 1
                self._filter_reasons.pop(key, None)
        while len(self._filter_reasons) > 256:
            self._filter_reasons.pop(next(iter(self._filter_reasons)))
        if devices:
            self.event('log', '扫描筛选：可入队%d，已处理/排队%d，信号不足%d，无名称%d，名称不匹配%d；RSSI门槛=%d' %
                (eligible, handled, weak, no_name, wrong_name, self.settings.min_rssi))
        for address, name, rssi in select_devices(devices, self.settings.prefix,
                self.settings.min_rssi, excluded)[:room]:
            self.event('log', '[%s] 扫描入队：名称=%s，RSSI=%s' % (address, name, rssi))
            self.attempted.add(address.upper())
            self._queue.append((address, name, rssi))

    def _fill(self):
        # Version queries have a separate single slot. Modem uploads still use
        # the configured transfer concurrency after the version is confirmed.
        if self._verifier is None:
            for row in list(self._queue):
                address, name, rssi = row
                key = address.upper()
                if key not in self._resume_queue:
                    continue
                self._queue.remove(row)
                self._resume_queue.discard(key)
                with self._waiting_lock:
                    pending = self._waiting.pop(key)
                self._verifier = threading.Thread(target=self._verify_device,
                    args=(address, name, rssi, pending), name='verify-%s' % address, daemon=True)
                self._verifier.start()
                break
        while self._queue and len(self._workers) < self.concurrency:
            address, name, rssi = self._queue.popleft()
            key = address.upper()
            if key in self._resume_queue:
                self._queue.appendleft((address, name, rssi))
                break
            continuing = key in self._continuations
            if self.stop.is_set() and not continuing:
                continue
            self._continuations.discard(key)
            worker = threading.Thread(target=self._device, args=(address, name, rssi),
                                      name='batch-%s' % address, daemon=True)
            worker.start()
            self._workers.append(worker)

    def run(self):
        try:
            while True:
                self._reap()
                self._maintain_waiting()
                self._fill()
                waiting = self._waiting_snapshot()
                if self.stop.is_set():
                    self._queue = deque(row for row in self._queue
                        if row[0].upper() in self._resume_queue | self._continuations)
                    if not self._workers and not self._verifier and not waiting and not self._queue:
                        break
                if self._needs_scan() and (not self.stop.is_set() or waiting):
                    try:
                        devices = self._scan_devices()
                    except Exception as error:
                        self.event('log', '扫描失败：%s' % error)
                        if self.stop.is_set():
                            time.sleep(1)
                        else:
                            self.stop.wait(3)
                        continue
                    if devices is not None:
                        self._last_scan = self.clock()
                        self._accept_scan(devices)
                self._fill()
                if self.stop.is_set():
                    time.sleep(0.05)
                else:
                    self.stop.wait(0.05 if self._workers or self._waiting_snapshot() else 1)
        except Exception as error:
            self.event('log', '批量任务停止：%s' % error)
        finally:
            # Stop/close leaves in-flight upgrades running to completion.
            for worker in self._workers:
                worker.join()
            if self._verifier:
                self._verifier.join()
            self.event('done', None)
