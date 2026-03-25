from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Set
import os
import sys

from core.logging_config import get_plugin_logger

from bluetooth.backends import BluetoothBackend
from bluetooth.service import normalize_mac, normalize_uuid


# CCCD (Client Characteristic Configuration Descriptor) UUID = 0x2902
# 用于启用/禁用 notify/indicate。
CCCD_UUID_N = normalize_uuid("00002902-0000-1000-8000-00805f9b34fb")


def _env_bool(name: str) -> bool:
    return os.environ.get(name, "0").strip().lower() in ("1", "true", "yes", "on")


def _use_discovered_connect() -> bool:
    """是否走「先 LE 扫描再 connect」（用扫描到的 QBluetoothDeviceInfo 建连）。

    - ``BLE_CONNECT_USE_DISCOVERED=0/false/no``：强制 **仅 MAC 直连**（易在 Windows 上报 ConnectionError）
    - ``BLE_CONNECT_USE_DISCOVERED=1/true/yes``：强制 **扫描后连接**
    - **未设置**：**Windows 默认 True**，其余平台默认 False（与旧行为兼容）
    """
    raw = os.environ.get("BLE_CONNECT_USE_DISCOVERED", "").strip().lower()
    if raw in ("0", "false", "no", "off"):
        return False
    if raw in ("1", "true", "yes", "on"):
        return True
    return sys.platform == "win32"


def _qt_ble_error_hints(err: object) -> str:
    """将 Qt Bluetooth 错误映射为简短中文排查提示（日志用）。"""
    s = str(err)
    if "ConnectionError" in s or "ConnectionError" in type(err).__name__:
        return (
            "ConnectionError：链路层连接失败。Windows 下默认已使用「先扫描再连」；若仍失败请检查设备广播/距离/是否被手机占用。"
            "可设 BLE_DEBUG_SCAN=1 看扫描列表。若需强制仅 MAC 直连可设 BLE_CONNECT_USE_DISCOVERED=0。"
        )
    if "RemoteHostClosed" in s:
        return "对端主动断开：检查设备端是否拒绝连接或协议超时。"
    return ""


@dataclass
class _QtMacState:
    controller: object
    closing: bool = False

    # service_uuid -> QLowEnergyService
    service_by_uuid: Dict[str, object] = field(default_factory=dict)
    services: list[object] = field(default_factory=list)

    # char_uuid -> QLowEnergyCharacteristic
    char_map: Dict[str, object] = field(default_factory=dict)
    # char_uuid -> QLowEnergyService
    char_service_map: Dict[str, object] = field(default_factory=dict)

    # char_uuid -> cccd value (little-endian bytes)
    pending_cccd: Dict[str, bytes] = field(default_factory=dict)
    # owner layer keeps notify_enabled set separately; backend uses pending_cccd only.

    # discovery bookkeeping
    service_uuids: Set[str] = field(default_factory=set)
    created_services: Set[str] = field(default_factory=set)

    # prevent processing after state is cleaned
    disconnected_called: bool = False


class QtBluetoothBackend(BluetoothBackend):
    """
    基于 PySide6.QtBluetooth 的 BLE Central 后端（已工程化收敛版）。

    设计要点：
    - service discovery：只收集 service UUID，统一在 discoveryFinished 创建 service 并 discoverDetails
    - characteristics mapping：在每个 service 的 RemoteServiceDiscovered 状态下统一填充 char_map / char_service_map
    - notify：不使用 startNotify/stopNotify；改为写 CCCD（descriptor 0x2902）
    - close：标记 closing，等 disconnected 回调触发后再清理 state，避免竞态回调访问已删除对象
    """

    def __init__(self) -> None:
        self._logger = get_plugin_logger("QtBluetoothBackend")
        self._states: Dict[str, _QtMacState] = {}

        # 延迟导入：避免模块未启用时 import 失败
        from PySide6.QtBluetooth import QLowEnergyController  # noqa: F401

    def open(
        self,
        mac: str,
        *,
        on_connected: Callable[[], None],
        on_disconnected: Callable[[str], None],
        on_ready: Callable[[str], None],
        on_data: Callable[[str, bytes], None],
        on_error: Callable[[str, str], None],
    ) -> None:
        from PySide6.QtBluetooth import (
            QBluetoothAddress,
            QBluetoothDeviceDiscoveryAgent,
            QBluetoothDeviceInfo,
            QLowEnergyController,
            QLowEnergyService,
            QLowEnergyCharacteristic,
        )
        from PySide6.QtCore import QTimer, QUuid

        mac_n = normalize_mac(mac)
        if not mac_n:
            on_error("invalid_args", "mac 不能为空")
            return

        # 清理旧 state
        if mac_n in self._states:
            try:
                self.close(mac_n)
            except Exception:
                pass

        # Windows 默认先 LE 扫描再建连；其它平台默认直连。可用 BLE_CONNECT_USE_DISCOVERED 覆盖。
        if _use_discovered_connect():
            self._logger.info(
                "BLE connect: scan-then-connect (platform=%s, override with BLE_CONNECT_USE_DISCOVERED=0/1)",
                sys.platform,
            )
            self._open_via_le_scan(
                mac_n,
                on_connected=on_connected,
                on_disconnected=on_disconnected,
                on_ready=on_ready,
                on_data=on_data,
                on_error=on_error,
            )
            return

        address = QBluetoothAddress(mac_n)
        remote_info = QBluetoothDeviceInfo(address, "watch", 0)
        controller = QLowEnergyController.createCentral(remote_info)
        if controller is None:
            on_error("create_controller_failed", "QLowEnergyController.createCentral 返回 None")
            return

        state = _QtMacState(controller=controller)
        self._states[mac_n] = state

        if _env_bool("BLE_DEBUG_SCAN"):
            try:
                agent = QBluetoothDeviceDiscoveryAgent()

                def _device_found(info: QBluetoothDeviceInfo) -> None:
                    try:
                        addr = info.address().toString()
                        name = info.name() or "<unknown>"
                        rssi = info.rssi()
                        self._logger.info("[SCAN] found device addr=%s name=%s rssi=%s", addr, name, rssi)
                    except Exception:
                        pass

                def _scan_finished() -> None:
                    self._logger.info("[SCAN] discovery finished")

                def _scan_error(err) -> None:
                    self._logger.error("[SCAN] discovery error: %s", err)

                agent.deviceDiscovered.connect(_device_found)  # type: ignore[attr-defined]
                agent.finished.connect(_scan_finished)  # type: ignore[attr-defined]
                agent.canceled.connect(_scan_finished)  # type: ignore[attr-defined]
                agent.errorOccurred.connect(_scan_error)  # type: ignore[attr-defined]
                agent.start(QBluetoothDeviceDiscoveryAgent.DiscoveryMethod.LowEnergyMethod)
                QTimer.singleShot(2000, getattr(agent, "stop", agent.cancel))
            except Exception as exc:
                self._logger.error("start discovery agent failed: %s", exc)

        self._attach_controller_and_signals(
            mac_n,
            controller,
            state,
            on_connected,
            on_disconnected,
            on_ready,
            on_data,
            on_error,
        )

    def _attach_controller_and_signals(
        self,
        mac_n: str,
        controller: object,
        state: _QtMacState,
        on_connected: Callable[[], None],
        on_disconnected: Callable[[str], None],
        on_ready: Callable[[str], None],
        on_data: Callable[[str], bytes],
        on_error: Callable[[str, str], None],
    ) -> None:
        from PySide6.QtBluetooth import QLowEnergyService, QLowEnergyCharacteristic
        from PySide6.QtCore import QTimer, QUuid

        def _safe_get_state() -> Optional[_QtMacState]:
            return self._states.get(mac_n)

        def _connected() -> None:
            st = _safe_get_state()
            if not st or st.closing:
                return
            try:
                self._logger.info("QtBluetoothBackend controller connected mac=%s", mac_n)
                controller.discoverServices()
                on_connected()
            except Exception as exc:
                on_error("discover_services_failed", str(exc))

        def _disconnected(reason: str) -> None:
            st = _safe_get_state()
            if not st:
                return
            if st.disconnected_called:
                return
            st.disconnected_called = True

            self._logger.warning("QtBluetoothBackend controller disconnected mac=%s reason=%s", mac_n, reason)
            try:
                on_disconnected(reason)
            finally:
                # 直到 disconnected 才清理，避免异步回调访问已删除对象
                try:
                    if hasattr(controller, "deleteLater"):
                        controller.deleteLater()  # type: ignore[attr-defined]
                except Exception:
                    pass
                self._states.pop(mac_n, None)

        def _error_occurred(err) -> None:
            hint = _qt_ble_error_hints(err)
            if hint:
                self._logger.error("Qt BLE error mac=%s err=%s | %s", mac_n, err, hint)
            else:
                self._logger.error("Qt BLE error mac=%s err=%s", mac_n, err)
            try:
                on_error("controller_error", str(err))
            except Exception:
                pass

        def _service_discovered(service_uuid) -> None:
            st = _safe_get_state()
            if not st or st.closing:
                return

            if hasattr(service_uuid, "toString"):
                sid = str(service_uuid.toString())
            else:
                sid = str(service_uuid)
            sid = sid.strip()
            if not sid:
                return
            self._logger.info("QtBluetoothBackend serviceDiscovered mac=%s service_uuid=%s", mac_n, sid)
            st.service_uuids.add(sid)

        def _char_changed(c: QLowEnergyCharacteristic, value: object) -> None:
            st = _safe_get_state()
            if not st or st.closing:
                return
            try:
                char_uuid = normalize_uuid(str(c.uuid().toString()))
                try:
                    data = bytes(value)
                except Exception:
                    data = b""
                # 这里不做过多日志：避免数据量太大淹没文件；必要时再加开关
                on_data(char_uuid, data)
            except Exception as exc:
                self._logger.error("characteristicChanged parse error: %s", exc)

        def _write_cccd(mac_n_inner: str, char_uuid_n: str, value_bytes: bytes) -> bool:
            st = self._states.get(mac_n_inner)
            if not st or st.closing:
                return False
            if char_uuid_n not in st.char_map or char_uuid_n not in st.char_service_map:
                return False

            ch = st.char_map[char_uuid_n]
            svc = st.char_service_map[char_uuid_n]

            # 2902: Client Characteristic Configuration Descriptor
            cccd = None
            try:
                if hasattr(ch, "descriptors"):
                    descriptors = list(ch.descriptors())  # type: ignore[attr-defined]
                else:
                    descriptors = []
            except Exception:
                descriptors = []

            for d in descriptors:
                try:
                    du = normalize_uuid(str(d.uuid().toString()))
                    if du == CCCD_UUID_N:
                        cccd = d
                        break
                except Exception:
                    continue

            if cccd is None:
                self._logger.info("CCCD not found mac=%s char=%s", mac_n_inner, char_uuid_n)
                return False

            try:
                from PySide6.QtCore import QByteArray

                # CCCD: 0x0001 notify, 0x0002 indicate
                svc.writeDescriptor(cccd, QByteArray(value_bytes))  # type: ignore[attr-defined]
                return True
            except Exception as exc:
                self._logger.error("writeDescriptor CCCD failed mac=%s char=%s err=%s", mac_n_inner, char_uuid_n, exc)
                return False

        def _apply_pending_cccds() -> None:
            st = _safe_get_state()
            if not st:
                return
            if not st.pending_cccd:
                return

            # 复制一份避免迭代中删除
            for char_uuid_n, val in list(st.pending_cccd.items()):
                if char_uuid_n not in st.char_map or char_uuid_n not in st.char_service_map:
                    continue
                ok = _write_cccd(mac_n, char_uuid_n, val)
                if ok:
                    # 写入成功后从 pending 移除
                    try:
                        del st.pending_cccd[char_uuid_n]
                    except KeyError:
                        pass

        def _populate_characteristics(service_obj) -> None:
            st = _safe_get_state()
            if not st or st.closing:
                return

            try:
                # service.uuid() -> service_uuid 字符串
                # 这里不强制依赖 service stateChanged：RemoteServiceDiscovered 时 characteristics() 才稳定可用
                chars = list(service_obj.characteristics())  # type: ignore[attr-defined]
            except Exception:
                chars = []

            discovered: list[str] = []
            new_chars: list[str] = []
            for ch in chars:
                try:
                    cu = normalize_uuid(str(ch.uuid().toString()))
                    discovered.append(cu)
                    is_new = cu not in st.char_map
                    st.char_map[cu] = ch
                    st.char_service_map[cu] = service_obj
                    if is_new:
                        new_chars.append(cu)
                except Exception:
                    continue

            try:
                service_uuid = normalize_uuid(str(service_obj.uuid().toString()))  # type: ignore[attr-defined]
            except Exception:
                service_uuid = "unknown"
            self._logger.info(
                "QtBluetoothBackend serviceDetails mac=%s service_uuid=%s char_count=%d chars=%s",
                mac_n,
                service_uuid,
                len(discovered),
                discovered,
            )

            self._logger.info(
                "QtBluetoothBackend char_map keys mac=%s count=%d keys=%s",
                mac_n,
                len(st.char_map),
                sorted(st.char_map.keys()),
            )

            _apply_pending_cccds()

            # ready：此时特征已进入 char_map；并且 CCCD 写入已尝试发起
            for cu in new_chars:
                try:
                    on_ready(cu)
                except Exception:
                    pass

        def _service_error(err) -> None:
            try:
                on_error("service_error", str(err))
            except Exception:
                pass

        def _discovery_finished() -> None:
            st = _safe_get_state()
            if not st or st.closing:
                return

            uuids = sorted(st.service_uuids)
            if not uuids:
                # 无 service 也不是致命：上层会 enable_notify 时失败或超时
                return
            self._logger.info("QtBluetoothBackend discoveryFinished mac=%s service_total=%d", mac_n, len(uuids))

            # 统一创建 services，并 discoverDetails 一次
            for sid in uuids:
                if sid in st.created_services:
                    continue
                try:
                    svc = controller.createServiceObject(QUuid(sid))
                except Exception:
                    svc = None
                if svc is None:
                    continue

                st.created_services.add(sid)
                st.services.append(svc)
                st.service_by_uuid[sid] = svc
                self._logger.info("QtBluetoothBackend createServiceObject mac=%s service_uuid=%s", mac_n, sid)

                # 只绑定一次：每个 service 的 characteristicChanged / stateChanged / errorOccurred
                try:
                    svc.characteristicChanged.connect(_char_changed)  # type: ignore[attr-defined]
                except Exception:
                    pass
                try:
                    # stateChanged 触发时，延迟一次 populate，规避时序问题
                    def _on_state_changed(ns, *, _svc=svc) -> None:
                        try:
                            if int(ns) == int(QLowEnergyService.ServiceState.RemoteServiceDiscovered):
                                QTimer.singleShot(0, lambda: _populate_characteristics(_svc))
                        except Exception:
                            pass

                    svc.stateChanged.connect(_on_state_changed)  # type: ignore[attr-defined]
                except Exception:
                    pass
                try:
                    svc.errorOccurred.connect(_service_error)  # type: ignore[attr-defined]
                except Exception:
                    pass

                try:
                    svc.discoverDetails()  # type: ignore[attr-defined]
                except Exception:
                    pass

            # fallback: 如果 pending_cccd 很久仍写不进去，重刷一次 characteristics 映射
            def _fallback() -> None:
                st2 = _safe_get_state()
                if not st2 or st2.closing:
                    return
                if st2.pending_cccd:
                    for svc in list(st2.services):
                        _populate_characteristics(svc)

            QTimer.singleShot(2500, _fallback)

        # -----------------------------
        # connect controller signals
        # -----------------------------
        controller.connected.connect(_connected)  # type: ignore[attr-defined]
        controller.disconnected.connect(lambda: _disconnected("disconnected"))  # type: ignore[attr-defined]
        controller.errorOccurred.connect(_error_occurred)  # type: ignore[attr-defined]
        controller.serviceDiscovered.connect(_service_discovered)  # type: ignore[attr-defined]
        controller.discoveryFinished.connect(_discovery_finished)  # type: ignore[attr-defined]

        # -----------------------------
        # connect to device
        # -----------------------------
        try:
            self._logger.info("Connecting to device mac=%s via QtBluetooth", mac_n)
            controller.connectToDevice()  # type: ignore[attr-defined]
        except Exception as exc:
            on_error("connect_to_device_failed", str(exc))

    def _open_via_le_scan(
        self,
        mac_n: str,
        *,
        on_connected: Callable[[], None],
        on_disconnected: Callable[[str], None],
        on_ready: Callable[[str], None],
        on_data: Callable[[str], bytes],
        on_error: Callable[[str, str], None],
    ) -> None:
        from PySide6.QtBluetooth import (
            QBluetoothDeviceDiscoveryAgent,
            QBluetoothDeviceInfo,
            QLowEnergyController,
        )
        from PySide6.QtCore import QTimer

        agent = QBluetoothDeviceDiscoveryAgent()
        found: list[QBluetoothDeviceInfo | None] = [None]
        scan_finished = {"done": False}

        def finish(info: QBluetoothDeviceInfo | None) -> None:
            if scan_finished["done"]:
                return
            scan_finished["done"] = True
            if info is None:
                self._logger.error(
                    "[SCAN→CONNECT] 未发现 %s。核对 MAC、设备已开机且在范围内；或设 BLE_DEBUG_SCAN=1 查看扫描列表。",
                    mac_n,
                )
                on_error("scan_no_device", f"BLE 扫描未发现设备 {mac_n}")
                return
            controller = QLowEnergyController.createCentral(info)
            if controller is None:
                on_error("create_controller_failed", "QLowEnergyController.createCentral 返回 None")
                return
            state = _QtMacState(controller=controller)
            self._states[mac_n] = state
            self._logger.info(
                "[SCAN→CONNECT] 使用扫描到的设备信息建连 addr=%s name=%s",
                info.address().toString(),
                info.name() or "",
            )
            self._attach_controller_and_signals(
                mac_n,
                controller,
                state,
                on_connected,
                on_disconnected,
                on_ready,
                on_data,
                on_error,
            )

        def on_device(info: QBluetoothDeviceInfo) -> None:
            try:
                if normalize_mac(info.address().toString()) != mac_n:
                    return
                found[0] = info
                try:
                    rssi = info.rssi()
                except Exception:
                    rssi = 0
                self._logger.info(
                    "[SCAN→CONNECT] matched addr=%s name=%s rssi=%s",
                    info.address().toString(),
                    info.name() or "",
                    rssi,
                )
            except Exception:
                pass
            try:
                agent.stop()
            except Exception:
                pass

        def on_scan_finished() -> None:
            if scan_finished["done"]:
                return
            finish(found[0])

        agent.deviceDiscovered.connect(on_device)  # type: ignore[attr-defined]
        agent.finished.connect(on_scan_finished)  # type: ignore[attr-defined]
        agent.canceled.connect(on_scan_finished)  # type: ignore[attr-defined]

        def on_scan_error(err) -> None:
            self._logger.error("[SCAN→CONNECT] discovery error: %s", err)

        agent.errorOccurred.connect(on_scan_error)  # type: ignore[attr-defined]

        timeout_ms = int(os.environ.get("BLE_SCAN_TIMEOUT_MS", "12000"))
        self._logger.info("[SCAN→CONNECT] start LE scan for %s (timeout %sms)", mac_n, timeout_ms)
        try:
            agent.start(QBluetoothDeviceDiscoveryAgent.DiscoveryMethod.LowEnergyMethod)
        except Exception as exc:
            on_error("scan_start_failed", str(exc))
            return

        def on_timeout() -> None:
            if scan_finished["done"]:
                return
            try:
                self._logger.warning("[SCAN→CONNECT] timeout %sms, stopping scan", timeout_ms)
                agent.stop()
            except Exception:
                pass

        QTimer.singleShot(timeout_ms, on_timeout)


    def close(self, mac: str) -> None:
        mac_n = normalize_mac(mac)
        st = self._states.get(mac_n)
        if not st:
            return
        if st.closing:
            return
        st.closing = True

        controller = st.controller
        try:
            if hasattr(controller, "disconnectFromDevice"):
                controller.disconnectFromDevice()  # type: ignore[attr-defined]
        except Exception:
            pass

        # safety: 如果 disconnected 回调丢失，延迟强制清理
        def _force_cleanup() -> None:
            st2 = self._states.get(mac_n)
            if not st2 or not st2.closing:
                return
            try:
                if hasattr(controller, "deleteLater"):
                    controller.deleteLater()  # type: ignore[attr-defined]
            except Exception:
                pass
            self._states.pop(mac_n, None)

        try:
            from PySide6.QtCore import QTimer

            QTimer.singleShot(3000, _force_cleanup)
        except Exception:
            _force_cleanup()

    def enable_notify(self, mac: str, characteristic_uuid: str) -> None:
        mac_n = normalize_mac(mac)
        char_uuid_n = normalize_uuid(characteristic_uuid)
        st = self._states.get(mac_n)
        if not st:
            return
        if not char_uuid_n:
            return

        # notify enable: 0x0001 -> little endian 0x01 0x00
        st.pending_cccd[char_uuid_n] = bytes([0x01, 0x00])
        self._logger.info("enable_notify pending mac=%s char=%s", mac_n, char_uuid_n)

        # 如果已经有映射，就立即尝试写 CCCD
        self._try_apply_cccd_now(mac_n)

    def disable_notify(self, mac: str, characteristic_uuid: str) -> None:
        mac_n = normalize_mac(mac)
        char_uuid_n = normalize_uuid(characteristic_uuid)
        st = self._states.get(mac_n)
        if not st:
            return
        if not char_uuid_n:
            return

        # disable: 0x0000
        st.pending_cccd[char_uuid_n] = bytes([0x00, 0x00])
        self._logger.info("disable_notify pending mac=%s char=%s", mac_n, char_uuid_n)
        self._try_apply_cccd_now(mac_n)

    def _try_apply_cccd_now(self, mac_n: str) -> None:
        st = self._states.get(mac_n)
        if not st:
            return
        if st.closing or not st.pending_cccd:
            return

        # 如果 mappings 已经可用，尝试立即写入 CCCD。
        # 若 descriptors / CCCD 尚未 discover 到，写入会失败并保持 pending，等待后续 service discover 完成后重试。
        for svc in list(st.services):
            try:
                chars = list(svc.characteristics())  # type: ignore[attr-defined]
                for ch in chars:
                    cu = normalize_uuid(str(ch.uuid().toString()))
                    is_new = cu not in st.char_map
                    st.char_map[cu] = ch
                    st.char_service_map[cu] = svc
                    # ready 事件由 _populate_characteristics() 触发
            except Exception:
                pass

        # 再尝试写入所有 pending
        try:
            from PySide6.QtCore import QByteArray
        except Exception:
            return

        for char_uuid_n, val in list(st.pending_cccd.items()):
            if char_uuid_n not in st.char_map or char_uuid_n not in st.char_service_map:
                continue

            ch = st.char_map[char_uuid_n]
            svc = st.char_service_map[char_uuid_n]

            cccd = None
            try:
                descriptors = list(ch.descriptors())  # type: ignore[attr-defined]
            except Exception:
                descriptors = []

            for d in descriptors:
                try:
                    du = normalize_uuid(str(d.uuid().toString()))
                    if du == CCCD_UUID_N:
                        cccd = d
                        break
                except Exception:
                    continue

            if cccd is None:
                # 保持 pending，等待 discoverDetails 完成后重试
                continue

            try:
                svc.writeDescriptor(cccd, QByteArray(val))  # type: ignore[attr-defined]
                del st.pending_cccd[char_uuid_n]
                # 不要在这里 break：可能一次性有多个 char 可以写入
            except Exception as exc:
                self._logger.error(
                    "writeDescriptor CCCD failed mac=%s char=%s err=%s",
                    mac_n,
                    char_uuid_n,
                    exc,
                )

    def write(self, mac: str, characteristic_uuid: str, data: bytes) -> None:
        mac_n = normalize_mac(mac)
        char_uuid_n = normalize_uuid(characteristic_uuid)

        st = self._states.get(mac_n)
        if not st:
            raise RuntimeError("device not open")
        if st.closing:
            raise RuntimeError("device closing")
        if char_uuid_n not in st.char_map or char_uuid_n not in st.char_service_map:
            raise RuntimeError("characteristic not ready for write")

        ch = st.char_map[char_uuid_n]
        svc = st.char_service_map[char_uuid_n]

        from PySide6.QtBluetooth import QLowEnergyService
        from PySide6.QtCore import QByteArray

        try:
            self._logger.info(
                "QtBluetoothBackend writeCharacteristic (WNR) mac=%s char=%s len=%s",
                mac_n,
                char_uuid_n,
                len(data),
            )
            svc.writeCharacteristic(ch, QByteArray(data), QLowEnergyService.WriteWithoutResponse)  # type: ignore[attr-defined]
            return
        except Exception as exc:
            # 回退：带响应写
            self._logger.warning(
                "QtBluetoothBackend writeCharacteristic WNR failed mac=%s char=%s err=%s; fallback to WWR",
                mac_n,
                char_uuid_n,
                exc,
            )
            svc.writeCharacteristic(ch, QByteArray(data), QLowEnergyService.WriteWithResponse)  # type: ignore[attr-defined]


__all__ = ["QtBluetoothBackend"]

