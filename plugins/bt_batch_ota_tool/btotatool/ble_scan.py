"""Bounded streaming BLE discovery and a short-lived connection cache."""
import asyncio
from collections import namedtuple
import threading
import time

_CACHE_SECONDS = 120.0
_CACHE_LIMIT = 256
_devices = {}
_cache_lock = threading.Lock()
_AdvertisementPair = namedtuple('AdvertisementPair', 'adv scan')


class ScanThreadTimeout(RuntimeError):
    pass


def remember_device(device):
    with _cache_lock:
        now = time.monotonic()
        for key in list(_devices):
            if now - _devices[key][0] > _CACHE_SECONDS:
                del _devices[key]
        _devices[str(device.address).upper()] = (now, device)
        while len(_devices) > _CACHE_LIMIT:
            del _devices[min(_devices, key=lambda key: _devices[key][0])]


def forget_device(address):
    with _cache_lock:
        _devices.pop(str(address).upper(), None)


def cached_device(address):
    if not isinstance(address, str):
        return address
    with _cache_lock:
        item = _devices.get(address.upper())
        if item and time.monotonic() - item[0] <= _CACHE_SECONDS:
            return item[1]
        _devices.pop(address.upper(), None)
    return address


class ScanSession:
    def __init__(self, timeout, on_device=None, match=None, target_count=0,
                 quiet_timeout=None, cancel=None, passive=False):
        self.timeout = max(0.0, float(timeout))
        self.on_device, self.match = on_device, match or (lambda row: True)
        self.target_count, self.quiet_timeout = max(0, target_count), quiet_timeout
        self.cancel = cancel or threading.Event()
        self.passive = passive
        self.lock = threading.RLock()
        self.closed = threading.Event()
        self.results, self.matched = {}, set()
        self.started = self.last_new = time.monotonic()

    def record(self, address, name, rssi):
        with self.lock:
            if self.closed.is_set():
                return
            address = address.upper()
            previous = self.results.get(address)
            name = name or (previous[0] if previous else '')
            rssi = max(rssi, previous[1]) if previous else rssi
            row = (address, name, rssi)
            self.results[address] = (name, rssi)
            if self.match(row) and address not in self.matched:
                self.matched.add(address)
                self.last_new = time.monotonic()
            # Show new devices and late scan-response names immediately. RSSI
            # changes alone do not flood the GUI queue; final results refresh it.
            publish = previous is None or name != previous[0]
        if publish and self.on_device and not self.closed.is_set():
            self.on_device(row)

    def done(self):
        if self.cancel.is_set() or self.closed.is_set():
            return True
        with self.lock:
            now = time.monotonic()
            if now - self.started >= self.timeout:
                return True
            if self.target_count and len(self.matched) >= self.target_count:
                return True
            return bool(self.quiet_timeout is not None and self.matched and
                        now - self.started >= 1.0 and
                        now - self.last_new >= self.quiet_timeout)

    def snapshot(self):
        with self.lock:
            return sorted(((address, name, rssi) for address, (name, rssi)
                           in self.results.items()), key=lambda row: -row[2])


def _native_device(address, name, pair, rssi):
    from bleak.backends.device import BLEDevice
    try:
        return BLEDevice(address, name, pair, rssi)
    except TypeError:  # New Bleak versions removed the deprecated RSSI argument.
        return BLEDevice(address, name, pair)


def _rssi(args):
    value = getattr(args, 'raw_signal_strength_in_dbm', None)
    if value is None:
        value = getattr(args, 'raw_signal_strength_in_d_bm', None)
    try:
        return int(value)
    except (ValueError, TypeError):
        return -999


def _scan_winrt(session):
    import winrt.windows.devices.bluetooth.advertisement as adv
    pairs = {}
    errors = []

    def received(sender, args):
        if session.closed.is_set():
            return
        # Keep the complete backend details, including advertisement type/address
        # metadata, so Bleak can connect without starting another watcher.
        with session.lock:
            key = int(args.bluetooth_address)
            raw = pairs.get(key, _AdvertisementPair(None, None))
            if args.advertisement_type == adv.BluetoothLEAdvertisementType.SCAN_RESPONSE:
                raw = _AdvertisementPair(raw.adv, args)
            else:
                raw = _AdvertisementPair(args, raw.scan)
            pairs[key] = raw
            name = ''
            for event in raw:
                if event is not None and event.advertisement.local_name:
                    name = event.advertisement.local_name
            value = '%012X' % key
            address = ':'.join(value[i:i + 2] for i in range(0, 12, 2))
            rssi = _rssi(args)
            remember_device(_native_device(address, name, raw, rssi))
            session.record(address, name, rssi)

    def worker():
        watcher = token = None
        try:
            if session.done():
                return
            watcher = adv.BluetoothLEAdvertisementWatcher()
            watcher.scanning_mode = (adv.BluetoothLEScanningMode.PASSIVE if session.passive
                                     else adv.BluetoothLEScanningMode.ACTIVE)
            token = watcher.add_received(received)
            watcher.start()
            while not session.done():
                session.cancel.wait(0.05)
        except Exception as error:
            errors.append(error)
        finally:
            session.closed.set()
            if watcher is not None:
                try:
                    watcher.stop()
                except Exception as error:
                    errors.append(error)
                try:
                    if token is not None:
                        watcher.remove_received(token)
                except Exception as error:
                    errors.append(error)

    thread = threading.Thread(target=worker, name='ble-scan', daemon=True)
    thread.start()
    # The owner thread stops and releases its watcher before returning. No
    # unconditional 300ms sleep, and cancellation wakes within 50ms.
    thread.join(session.timeout + 3)
    if thread.is_alive():
        session.closed.set()
        thread.join(1)
        if thread.is_alive():
            raise ScanThreadTimeout('BLE scan thread did not stop; reconnect the adapter')
    if errors:
        raise errors[0]
    return session.snapshot()


def _scan_bleak(session):
    from bleak import BleakScanner

    def received(device, data):
        if session.closed.is_set():
            return
        remember_device(device)
        session.record(device.address, data.local_name or device.name or '', data.rssi)

    async def run():
        options = {'scanning_mode': 'passive'} if session.passive else {}
        scanner = BleakScanner(detection_callback=received, **options)
        try:
            if session.done():
                return
            await scanner.start()
            while not session.done():
                await asyncio.sleep(0.05)
        finally:
            session.closed.set()
            await scanner.stop()

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(run())
    finally:
        loop.close()
    return session.snapshot()


def scan_ble(timeout=10.0, on_log=None, on_device=None, *, match=None,
             target_count=0, quiet_timeout=None, cancel=None, passive=False):
    options = dict(on_device=on_device, match=match, target_count=target_count,
                   quiet_timeout=quiet_timeout, cancel=cancel, passive=passive)
    session = ScanSession(timeout, **options)
    started = time.monotonic()
    try:
        rows = _scan_winrt(session)
    except ScanThreadTimeout:
        raise
    except Exception as error:
        if cancel is not None and cancel.is_set():
            return session.snapshot()
        if on_log:
            on_log('WinRT BLE 扫描失败，回退 Bleak：%s' % error)
        session = ScanSession(max(0.0, timeout - (time.monotonic() - started)), **options)
        rows = _scan_bleak(session)
    if on_log:
        on_log('扫描完成：%d 个设备，耗时 %.2f 秒' % (len(rows), time.monotonic() - started))
    return rows
