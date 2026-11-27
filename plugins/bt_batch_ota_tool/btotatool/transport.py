# -*- coding: utf-8 -*-
"""
传输层：BLE / SPP 两种通道，统一成 Java ``IBluzIO`` 的语义。

Java 参考：
    com.actions.actionsfactory.IBluzIO   —— write / flush / read / readInt / readShort / read
    com.actions.ibluz.device.bluzdevice.BluzDeviceSpp  —— 经典蓝牙串口
    com.actions.ibluz.device.bluzdevice.BluzDeviceBle  —— BLE GATT

两种通道都实现同一套接口，上层 OTA 状态机无需关心底下是 BLE 还是 SPP。
"""

from __future__ import annotations

import asyncio
import re
import threading
import time
from typing import Callable, List, Optional, Tuple

from . import protocol
from .ble_lifecycle import BleLoopOwner, release_winrt_resources

# ---------------------------------------------------------------- 默认 UUID

#: OTA over BLE 的 service / write / notify
OTA_BLE_SERVICE = "e49a25f8-f69a-11e8-8eb2-f2801f1b9fd1"
OTA_BLE_WRITE = "e49a25e0-f69a-11e8-8eb2-f2801f1b9fd1"
OTA_BLE_READ = "e49a28e1-f69a-11e8-8eb2-f2801f1b9fd1"

#: OTA over SPP 用的 SPP UUID —— 安卓 app 的 ``Global.OTA_SPP_UUID`` 就是它，
#: OTA 走 SPP 时 app 会 ``setUUID("00006666-...")`` 再 ``createInsecureRfcommSocketToServiceRecord``。
#: 设备的 OTA 解析器只监听这个 0x6666 通道（0x1101 是 app 的 LOG 日志串口，不是 OTA）。
OTA_SPP_UUID = "00006666-0000-1000-8000-00805F9B34FB"

#: 标准串口(Serial Port) UUID —— 设备登记为「串口」的那个，安卓 ``BluzDeviceSpp`` 的默认
#: mUUID 是它，但 app 做 OTA 前会用 ``setUUID(0x6666)`` 覆盖掉。这里仅作为兜底：若某设备
#: 的 OTA 走的是标准串口（没暴露 0x6666），才退到这里。
SPP_SERIAL_UUID = "00001101-0000-1000-8000-00805F9B34FB"

#: 其余功能模块的 BLE UUID（后续功能扩展时用）
WATCH_BLE_SERVICE = "e49a4001-f69a-11e8-8eb2-f2801f1b9fd1"
WATCH_BLE_WRITE = "e49a4002-f69a-11e8-8eb2-f2801f1b9fd1"
WATCH_BLE_READ = "e49a4003-f69a-11e8-8eb2-f2801f1b9fd1"
LOG_BLE_SERVICE = "e49a3001-f69a-11e8-8eb2-f2801f1b9fd1"
LOG_BLE_WRITE = "e49a3002-f69a-11e8-8eb2-f2801f1b9fd1"
LOG_BLE_READ = "e49a3003-f69a-11e8-8eb2-f2801f1b9fd1"


class TransportError(Exception):
    """通道层错误。"""


async def _await_winrt(op):
    """
    等待一个 winrt 异步操作完成并返回结果。

    本机 winrt 构建里，StreamSocket / Bluetooth 的 ``*Async`` 方法返回的是可直接
    ``await`` 的对象；但 **DataReader.load_async / DataWriter.store_async** 返回的
    ``DataReaderLoadOperation / DataWriterStoreOperation`` **没有 __await__**，
    必须用 ``completed`` 回调桥接到 asyncio future。这个 helper 两种都兼容。
    """
    if hasattr(op, "__await__"):
        return await op
    loop = asyncio.get_event_loop()
    fut = loop.create_future()

    def _done(sender, status):
        try:
            st = int(status)
            if st == 1:  # Completed
                try:
                    res = sender.get_results()
                except Exception:
                    res = None
                loop.call_soon_threadsafe(fut.set_result, res)
            elif st == 2:  # Error
                try:
                    err = sender.error_code
                except Exception:
                    err = RuntimeError("winrt 异步操作失败")
                exc = err if isinstance(err, Exception) else RuntimeError(str(err))
                loop.call_soon_threadsafe(fut.set_exception, exc)
            # 0=Started / 3=Canceled 不处理
        except Exception as e:  # noqa: BLE001
            loop.call_soon_threadsafe(fut.set_exception, e)

    try:
        st_now = int(op.status)
    except Exception:
        st_now = 0
    if st_now == 1:  # 设置回调前就已经完成
        fut.set_result(op.get_results())
        return await fut
    if st_now == 2:
        try:
            err = op.error_code
        except Exception:
            err = RuntimeError("winrt 异步操作失败")
        exc = err if isinstance(err, Exception) else RuntimeError(str(err))
        fut.set_exception(exc)
        return await fut
    op.completed = _done
    return await fut


def _ibuffer_to_bytes(ibuf) -> bytes:
    """把 winrt IBuffer 转成 Python bytes（本机 winrt 的 IBuffer 不实现 buffer 协议）。"""
    import winrt.windows.security.cryptography as cg
    return bytes(cg.CryptographicBuffer.copy_to_byte_array(ibuf))


# ---------------------------------------------------------------- 基类


class Transport:
    """字节流通道抽象。对应 Java 的 IBluzIO。"""

    kind = "?"

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def flush(self) -> None:
        pass

    def read(self, n: int, timeout: float = 5.0) -> bytes:
        """最多读 n 字节；超时返回已有数据（可能为空）。"""
        raise NotImplementedError

    def read_exactly(self, n: int, timeout: float = 5.0) -> bytes:
        """读满 n 字节才返回；超时抛 TransportError。"""
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while len(buf) < n:
            remain = deadline - time.monotonic()
            if remain <= 0:
                raise TransportError("读超时：期望 %d 字节，实际 %d 字节" % (n, len(buf)))
            chunk = self.read(n - len(buf), remain)
            if not chunk:
                time.sleep(0.005)
                continue
            buf += chunk
        return bytes(buf)

    def read_byte(self) -> int:
        return self.read_exactly(1)[0]

    def read_int(self) -> int:
        """4 字节小端。"""
        return protocol.u32le(self.read_exactly(4))

    def read_short(self) -> int:
        """2 字节小端。"""
        return protocol.u16le(self.read_exactly(2))

    def close(self) -> None:
        pass

    def start_notifications(self) -> None:
        """Stream transports need no subscription; BLE overrides this."""
        pass

    def describe(self) -> str:
        return self.kind


# ---------------------------------------------------------------- SPP


def _spp_service_uuid(svc) -> str:
    """
    从 WinRT 的 ``RfcommDeviceService`` 取服务 UUID（小写、去花括号）。

    优先用 ``service_id.as_string()``（标准 UUID 串）；
    兜底用 ``connection_service_name`` 里匹配的 UUID（实测里这个字符串也含 UUID）。
    """
    try:
        sid = getattr(svc, "service_id", None)
        if sid is not None:
            s = sid.as_string()
            if s:
                return s.strip("{}").lower()
    except Exception:
        pass
    name = getattr(svc, "connection_service_name", None) or ""
    m = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27}", str(name))
    if m:
        return m.group(0).lower()
    return ""


class SppTransport(Transport):
    """经典蓝牙串口（Windows 上是配对后出现的虚拟 COM 口）。"""

    kind = "SPP"

    def __init__(self, port: str, baudrate: int = 115200):
        try:
            import serial  # pyserial
        except ImportError as e:  # pragma: no cover
            raise TransportError("缺少 pyserial，请先 pip install pyserial") from e
        try:
            self._ser = serial.Serial(port=port, baudrate=baudrate, timeout=0.2, write_timeout=10)
        except Exception as e:
            raise TransportError("打开 %s 失败：%s" % (port, e)) from e
        self._port = port

    def write(self, data: bytes) -> None:
        self._ser.write(data)

    def flush(self) -> None:
        self._ser.flush()

    def read(self, n: int, timeout: float = 5.0) -> bytes:
        self._ser.timeout = max(0.02, min(0.5, timeout))
        try:
            return self._ser.read(n) or b""
        finally:
            self._ser.timeout = 0.2

    def close(self) -> None:
        try:
            self._ser.close()
        except Exception:
            pass

    def describe(self) -> str:
        return "SPP %s" % self._port


class SppPort:
    """一个串口条目，重点标出它是不是【蓝牙 SPP（RFCOMM）】虚拟口。"""

    __slots__ = ("port", "desc", "is_bluetooth", "service_uuid", "hwid")

    def __init__(self, port: str, desc: str, hwid: str):
        self.port = port
        self.desc = desc
        self.hwid = hwid or ""
        # 蓝牙 SPP 虚拟口的 PnP 实例 ID 形如
        #   BTHENUM\{00001101-0000-1000-8000-00805F9B34FB}_LOCALMFG&..._C00000000
        # 用它判定比匹配描述文本可靠（描述文本会随系统语言变化）。
        self.is_bluetooth = self.hwid.upper().startswith("BTHENUM")
        self.service_uuid = ""
        if self.is_bluetooth:
            m = re.search(r"\{([0-9A-Fa-f-]{36})\}", self.hwid)
            if m:
                self.service_uuid = m.group(1).lower()

    def label(self) -> str:
        if self.is_bluetooth:
            extra = " [%s]" % self.service_uuid[:8] if self.service_uuid else ""
            return "%s  %s  蓝牙SPP%s" % (self.port, self.desc, extra)
        return "%s  %s" % (self.port, self.desc)

    def __repr__(self) -> str:  # pragma: no cover
        return "<SppPort %s bt=%s>" % (self.port, self.is_bluetooth)


def list_spp_ports(bluetooth_only: bool = False) -> List[SppPort]:
    """
    列出串口。

    bluetooth_only=True 时只返回**蓝牙 SPP（RFCOMM）虚拟口**
    （Windows 表现为「蓝牙链接上的标准串行 (COMxx)」），把 USB 串口、主板物理口排除掉。
    """
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    out = []
    for p in list_ports.comports():
        sp = SppPort(p.device, p.description or "", p.hwid or "")
        if bluetooth_only and not sp.is_bluetooth:
            continue
        out.append(sp)
    return out


# ---------------------------------------------------------------- WinRT RFCOMM（直连，无需 COM 口）


class WinRfcommTransport(Transport):
    """
    Windows 上**直接**用 WinRT RFCOMM socket 连 SPP（和安卓 app 完全一样：
    ``createInsecureRfcommSocketToServiceRecord(0x1101)``），**不需要系统预先建出 COM 口**。

    这正是「点连接就能连」的关键：安卓 app 就是这样连的，根本不碰串口。
    本机 ``BluetoothFindFirstRadio`` 返回 NULL、无法用 bthprops 强制建口，
    所以走这条 WinRT 直连路——实测 a566 能枚举到 0x1101 服务、直连可用。

    读：起一个 reader 协程持续从 InputStream 拉数据进缓冲（同 BleTransport 的 _BleChannel）。
    写：用 DataWriter 把字节流进 OutputStream。
    """

    kind = "SPP"

    #: OTA 工具：优先连 OTA 专用 SPP 通道 0x6666（安卓 app 做 SPP OTA 连的就是它）；
    #: 找不到 0x6666 再退到标准串口 0x1101；都没有就任意 RFCOMM。
    _PREFER = (
        OTA_SPP_UUID,
        SPP_SERIAL_UUID,
    )

    def __init__(self, mac: str, service_uuid: Optional[str] = None,
                 on_log: Optional[Callable[[str], None]] = None,
                 read_timeout: float = 5.0, connect_timeout: float = 15.0):
        try:
            import asyncio  # noqa: F401
            import winrt.windows.devices.bluetooth as _bt  # noqa: F401
            import winrt.windows.devices.bluetooth.rfcomm as _rf  # noqa: F401
            import winrt.windows.networking.sockets as _sk  # noqa: F401
            import winrt.windows.storage.streams as _st  # noqa: F401
        except ImportError as e:  # pragma: no cover
            raise TransportError("缺少 winrt，请先 pip install winrt") from e

        self._log = on_log or (lambda m: None)
        self._mac = mac
        self._service_uuid = service_uuid
        self._channel = _BleChannel()
        self._read_timeout = read_timeout
        self._connect_timeout = connect_timeout
        self._socket = None
        self._writer = None
        self._reader = None
        self._closed = False
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever,
                                            name="rfcomm-loop", daemon=True)
        self._loop_thread.start()
        try:
            self._socket, self._writer, self._used_uuid = self._run(
                self._connect(mac, service_uuid))
        except Exception as e:
            self.close()
            raise TransportError("RFCOMM 直连失败：%s" % e) from e
        self._reader = _st.DataReader(self._socket.input_stream)
        self._reader.input_stream_options = _st.InputStreamOptions.PARTIAL
        # 关键：reader 是 while True 的常驻协程，必须用 run_coroutine_threadsafe
        # **分派**到事件循环线程后立即返回，**不能**用 self._run(...) 等它结束——
        # 否则构造函数会卡死在子线程里，_on_connected 永远收不到连接结果，
        # 导致 GUI 以为没连上、点升级又要求连接、再连又因通道被占而失败。
        asyncio.run_coroutine_threadsafe(self._start_reader(), self._loop)

    # -- 内部工具 --------------------------------------------------

    def _run(self, coro):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result()

    def _logln(self, msg: str) -> None:
        try:
            self._log(msg)
        except Exception:
            pass

    async def _connect(self, mac, service_uuid):
        import winrt.windows.devices.bluetooth as bt
        import winrt.windows.devices.bluetooth.rfcomm as rfcomm
        import winrt.windows.networking.sockets as sockets
        import winrt.windows.storage.streams as streams
        from . import btwinrt

        _t_pre = time.monotonic()
        did = btwinrt._device_id(mac)
        self._logln("[计时] 解析设备id(含缓存/枚举) %.2fs" % (time.monotonic() - _t_pre))
        if not did:
            raise TransportError("找不到设备 %s 的系统 id（设备是否离线/未配对？）" % mac)
        _t0 = time.monotonic()
        bd = await bt.BluetoothDevice.from_id_async(did)
        if bd is None:
            raise TransportError("打不开设备 %s（离线或未配对）—— "
                                 "请在 Windows 蓝牙设置里连上它再重试" % mac)
        self._logln("[计时] 打开设备对象 %.2fs" % (time.monotonic() - _t0))
        _t1 = time.monotonic()
        res = await bd.get_rfcomm_services_async()
        svcs = list(getattr(res, "services", None) or [])
        self._logln("[计时] SDP枚举 %d 个服务 %.2fs" % (len(svcs), time.monotonic() - _t1))
        if not svcs:
            raise TransportError("设备 %s 当前没有可用的 RFCOMM 服务"
                                 "（设备离线，或 Windows 未授权访问串口服务）" % mac)

        # 选目标服务：service_uuid 优先，其次 0x1101 串口，其次 0x6666，最后任意
        order = []
        if service_uuid:
            order.append(service_uuid.lower().strip("{}"))
        order += [u.lower().strip("{}") for u in self._PREFER]
        target = None
        for u in order:
            for s in svcs:
                if _spp_service_uuid(s) == u:
                    target = s
                    break
            if target is not None:
                break
        if target is None:
            target = svcs[0]

        used = _spp_service_uuid(target)
        # 连前先请求访问权限（配对后通常已允许，但确认一下更稳）
        try:
            st = await target.request_access_async()
            code = int(st) if hasattr(st, "value") else int(st)
            if code != 1:
                self._logln("RFCOMM 服务访问未被允许(code=%s)，仍尝试连接" % code)
        except Exception as e:
            self._logln("request_access 异常：%s" % e)

        socket = sockets.StreamSocket()
        # 本机 winrt 没投影 RfcommDeviceService.Connection，改用
        # StreamSocket.ConnectAsync(ConnectionHostName, ConnectionServiceName) —— 等价的
        # 不安全 RFCOMM 直连（与安卓 createInsecureRfcommSocket 一致）。
        # 注意 serviceName 必须传**完整的规范服务名串**（来自 connection_service_name），
        # 不是裸 UUID —— 操作系统靠它定位 RFCOMM channel。
        conn_obj = getattr(target, "connection", None)
        try:
            _t2 = time.monotonic()
            if conn_obj is not None:
                await asyncio.wait_for(socket.connect_async(conn_obj),
                                       timeout=self._connect_timeout)
            else:
                host = target.connection_host_name
                svcname = target.connection_service_name or used
                await asyncio.wait_for(socket.connect_async(host, svcname),
                                       timeout=self._connect_timeout)
            self._logln("[计时] connect %.2fs (超时上限=%.0fs)" % (time.monotonic() - _t2, self._connect_timeout))
        except asyncio.TimeoutError:
            raise TransportError("RFCOMM 连接超时（%s 秒，设备可能不在范围内）" % self._connect_timeout)
        writer = None  # 实际 writer 在 _write_coro 里惰性创建（绕开 store_async 的坑）
        self._logln("RFCOMM 直连成功 %s 服务=%s" % (mac, used or "?"))
        return socket, writer, used

    async def _start_reader(self) -> None:
        reader = self._reader
        try:
            while not self._closed:
                size = await _await_winrt(reader.load_async(4096))
                if self._closed:
                    break
                if size == 0:
                    # 对端关闭或暂时无数据：缓一下继续等，不要空转爆 CPU
                    await asyncio.sleep(0.01)
                    continue
                # 本机 winrt 的 DataReader.read_bytes / read_buffer 对 bytearray 均报
                # "参数错误"，但 read_buffer 返回 IBuffer，再用 CryptographicBuffer
                # 转成 bytes 是稳定可用的。
                ibuf = reader.read_buffer(size)
                self._channel.feed(_ibuffer_to_bytes(ibuf))
        except Exception:
            # socket 断开/出错时退出循环，不再重连（上层会感知写失败）
            pass

    # -- Transport 接口 ---------------------------------------------

    def write(self, data: bytes) -> None:
        if not data:
            return
        self._run(self._write_coro(data))

    async def _write_coro(self, data):
        # 注意：winrt 的 DataWriter.write_bytes 只接受 Python list[int]，
        # 传 bytes / bytearray 会报"参数错误"。
        # 本机 winrt 上，DataWriter.store_async 作用在 StreamSocket 的 output_stream 时
        # 会稳定返回 E_INVALIDARG（疑似该 winrt 构建的 bug），所以改成更稳的写法：
        #   用 DataWriter 把字节装进一个 IBuffer（detach_buffer），
        #   再走 output_stream.write_async(buffer)。该写法在内存流与 socket 上均验证可用。
        import winrt.windows.storage.streams as streams
        if self._writer is None:
            self._writer = streams.DataWriter()
        self._writer.write_bytes(list(data))
        buf = self._writer.detach_buffer()
        op = self._socket.output_stream.write_async(buf)
        # 加超时：万一对端不收（设备离线/抽风），不要无限卡死调用线程。
        await asyncio.wait_for(_await_winrt(op), timeout=10.0)

    def read(self, n: int, timeout: float = 5.0) -> bytes:
        return self._channel.read(n, timeout)

    def close(self) -> None:
        self._closed = True
        try:
            if self._reader is not None:
                self._reader.close()
        except Exception:
            pass
        try:
            if self._writer is not None:
                self._writer.close()
        except Exception:
            pass
        try:
            if self._socket is not None:
                self._socket.close()
        except Exception:
            pass
        try:
            if self._loop is not None:
                # 必须先取消挂起的协程（reader 循环），否则 loop.close() 会因为
                # 还有 pending task 而抛异常被吞掉，旧 reader 协程会变成
                # "Task was destroyed but it is pending"，甚至和新连接抢数据。
                try:
                    pending = [t for t in asyncio.all_tasks(self._loop)
                               if not t.done()]
                    for t in pending:
                        t.cancel()
                except Exception:
                    pass
                self._loop.call_soon_threadsafe(self._loop.stop)
                self._loop_thread.join(timeout=3)
                try:
                    self._loop.close()
                except Exception:
                    pass
        except Exception:
            pass

    def describe(self) -> str:
        return "SPP(RFCOMM) %s" % self._mac


# ---------------------------------------------------------------- BLE


class _BleChannel:
    """BLE 通知缓冲：bleak 的回调线程写入，读取线程消费。"""

    def __init__(self):
        self._buf = bytearray()
        self._cv = threading.Condition()

    def feed(self, data: bytes) -> None:
        with self._cv:
            self._buf += data
            self._cv.notify_all()

    def read(self, n: int, timeout: float) -> bytes:
        deadline = time.monotonic() + timeout
        with self._cv:
            while not self._buf:
                remain = deadline - time.monotonic()
                if remain <= 0:
                    return b""
                self._cv.wait(min(remain, 0.2))
            take = min(n, len(self._buf))
            out = bytes(self._buf[:take])
            del self._buf[:take]
            return out


class BleTransport(Transport):
    """BLE GATT 通道（基于 bleak，Windows 10+ 走 WinRT）。"""

    kind = "BLE"
    _disconnect_timeout = 2.0
    _close_timeout = 4.0

    def __init__(
        self,
        address: str,
        service_uuid: str = OTA_BLE_SERVICE,
        write_uuid: str = OTA_BLE_WRITE,
        notify_uuid: str = OTA_BLE_READ,
        on_log: Optional[Callable[[str], None]] = None,
        write_gap: Optional[float] = None,
        auto_subscribe: Optional[bool] = None,
        strict_service: bool = False,
        use_cached_services: Optional[bool] = None,
        services: Optional[List[str]] = None,
    ):
        try:
            from bleak import BleakClient
        except ImportError as e:  # pragma: no cover
            raise TransportError("缺少 bleak，请先 pip install bleak") from e
        self._log = on_log or (lambda m: None)
        self._address = address
        self._lifecycle_lock = threading.RLock()
        self._closing = False
        self._shutdown_future = None
        self._client = None
        self._strict_service = strict_service
        # Match BtOtaTool: OTA uses the device-negotiated params.interval;
        # file transfer explicitly requests zero; other channels keep 3ms.
        self._write_gap = (0.0 if service_uuid.lower() == OTA_BLE_SERVICE.lower()
                           else 0.003) if write_gap is None else write_gap
        self._channel = _BleChannel()
        # 专用事件循环线程：bleak 的 coroutine 必须在同一个 loop 里跑，
        # 而 OTA 的读写会发生在多个线程（GUI 线程 / 读线程），所以用
        # run_coroutine_threadsafe 跨线程投递，而不是 run_until_complete。
        self._loop_owner = BleLoopOwner()
        self._loop = self._loop_owner.loop
        self._loop_thread = self._loop_owner.thread
        from .ble_scan import cached_device
        connection_target = cached_device(address)
        client_options = {} if use_cached_services is None else {
            "winrt": {"use_cached_services": use_cached_services}}
        if services is not None:
            client_options['services'] = services
        self._write_char = None
        self._notify_char = None
        self._mtu = 20
        self._subscribed = False
        self._notify_lock = threading.Lock()
        if auto_subscribe is None:
            auto_subscribe = service_uuid.lower() != OTA_BLE_SERVICE.lower()

        try:
            try:
                self._client = BleakClient(connection_target, **client_options)
            except TypeError:  # pragma: no cover - 老版本 bleak
                self._client = BleakClient(connection_target, loop=self._loop, **client_options)
            self._run(self._client.connect())
            self._resolve_chars(service_uuid, write_uuid, notify_uuid)
            if auto_subscribe:
                self.start_notifications()
        except Exception as e:
            from .ble_scan import forget_device
            forget_device(address)
            self.close()
            raise TransportError("BLE 初始化失败：%s" % (str(e) or type(e).__name__)) from e
        self._address = address

    def start_notifications(self) -> None:
        """Subscribe exactly once, immediately before the OTA handshake."""
        with self._notify_lock:
            if not self._subscribed:
                self._run(self._client.start_notify(self._notify_char, self._on_notify))
                self._subscribed = True

    # -- 内部工具 --------------------------------------------------

    def _run(self, coro):
        """把 coroutine 投递到专用事件循环线程并等待结果（可从任意线程调用）。"""
        fut = self._submit(coro)
        try:
            return fut.result(timeout=getattr(self, "_operation_timeout", None))
        except Exception:
            fut.cancel()
            raise

    def _submit(self, coro):
        # Serialize accepting work with close(), including the final loop exit.
        with self._lifecycle_lock:
            if self._closing or self._loop.is_closed():
                coro.close()
                raise TransportError('BLE 连接已关闭：%s' % self._address)
            return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _logln(self, msg: str) -> None:
        try:
            self._log(msg)
        except Exception:
            pass

    def _resolve_chars(self, service_uuid: str, write_uuid: str, notify_uuid: str) -> None:
        """按 UUID 找 service / write / notify 特征；找不到就按属性兜底。"""
        services = self._client.services
        svc = None
        for s in services:
            if s.uuid.lower() == service_uuid.lower():
                svc = s
                break
        if svc is None and getattr(self, "_strict_service", False):
            raise TransportError("设备未提供指定服务：%s" % service_uuid)
        if svc is None:
            # 兜底：取第一个同时具备可写与可通知/可指示特征的 service
            for s in services:
                chars = list(s.characteristics)
                if any(getattr(c, "write_type", None) for c in chars) and any(
                    "notify" in c.properties or "indicate" in c.properties for c in chars
                ):
                    svc = s
                    self._logln("未匹配到 service %s，兜底使用 %s" % (service_uuid, s.uuid))
                    break
        if svc is None:
            raise TransportError("设备上没有可用的 GATT service（目标 %s）" % service_uuid)

        chars = list(svc.characteristics)
        if getattr(self, "_strict_service", False):
            found = {c.uuid.lower() for c in chars}
            if write_uuid.lower() not in found or notify_uuid.lower() not in found:
                raise TransportError("设备缺少指定的读写特征，请确认配套固件")
        self._write_char = self._pick(chars, write_uuid, ("write", "write-without-response"))
        self._notify_char = self._pick(chars, notify_uuid, ("notify", "indicate"))
        if self._write_char is None:
            raise TransportError("service %s 下没有可写特征" % svc.uuid)
        if self._notify_char is None:
            raise TransportError("service %s 下没有可通知特征" % svc.uuid)
        self._logln("GATT 已就绪 service=%s write=%s notify=%s" % (svc.uuid, self._write_char.uuid, self._notify_char.uuid))
        mtu = getattr(self._client, "mtu_size", 23) or 23
        self._mtu = max(20, mtu - 3)

    @staticmethod
    def _pick(chars, want_uuid: str, props) -> Optional[object]:
        for c in chars:
            if c.uuid.lower() == want_uuid.lower():
                return c
        for c in chars:
            if any(p in c.properties for p in props):
                return c
        return None

    def _on_notify(self, _sender, data: bytearray) -> None:
        self._channel.feed(bytes(data))

    # -- Transport 接口 ---------------------------------------------

    def write(self, data: bytes) -> None:
        """
        写出数据。

        **不要手动按 MTU 切片**：bleak 内部会按后端自动分片
        （Windows/WinRT 的 WriteValueAsync 自己就做 ATT 分片，实测 mtu_size=247，
        整帧一次写入设备可正常解析；手动切片反而会粘包）。

        OTA 不额外等待；设备要求的间隔由 OtaClient 按协商参数执行。
        """
        if not data:
            return
        self._run(self._client.write_gatt_char(self._write_char, data, response=False))
        if self._write_gap > 0:
            time.sleep(self._write_gap)

    def flush(self) -> None:
        pass

    def read(self, n: int, timeout: float = 5.0) -> bytes:
        return self._channel.read(n, timeout)

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._loop.is_closed():
                return
            if self._shutdown_future is None:
                self._closing = True
                self._shutdown_future = asyncio.run_coroutine_threadsafe(self._shutdown_ble(), self._loop)
                self._shutdown_future.add_done_callback(self._loop_owner.stop)
            future = self._shutdown_future
        if threading.current_thread() is self._loop_thread:
            return
        try:
            future.result(timeout=self._close_timeout)
        except Exception as error:
            if not future.done():
                # A native WinRT Close can block. Leave its loop alive to finish
                # disposal instead of closing underneath its Windows callbacks.
                self._cleanup_log('清理尚未结束，正在后台完成；%s' % (str(error) or type(error).__name__))
                return
            self._cleanup_log('清理异常：%s' % (str(error) or type(error).__name__))
        self._loop_thread.join(timeout=0.5)

    def _cleanup_log(self, message):
        self._logln('[BLE 清理 %s] %s' % (self._address, message))

    async def _shutdown_ble(self):
        # Stop in-flight connect/write operations before disposing GATT objects.
        await self._cancel_ble_tasks()
        try:
            if self._client is not None:
                # Bleak disconnect removes local notification handlers itself;
                # no separate remote CCC write is needed before dropping a link.
                await asyncio.wait_for(self._client.disconnect(), timeout=self._disconnect_timeout)
        except asyncio.TimeoutError:
            self._cleanup_log('断开等待超时，释放本地 WinRT 会话')
        except Exception as error:
            self._cleanup_log('断开未完整结束：%s；继续释放本地会话' % (str(error) or type(error).__name__))
        finally:
            self._subscribed = False
            try:
                release_winrt_resources(self._client, self._notify_char, self._cleanup_log)
            except Exception as error:
                self._cleanup_log('本地会话释放异常：%s' % (str(error) or type(error).__name__))
        await self._cancel_ble_tasks()
        await self._loop.shutdown_asyncgens()

    async def _cancel_ble_tasks(self):
        # Cancellation is complete only when coroutine finally blocks have run.
        current = asyncio.current_task()
        pending = [task for task in asyncio.all_tasks() if task is not current and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def describe(self) -> str:
        return "BLE %s" % getattr(self, "_address", "?")


from .ble_scan import scan_ble
