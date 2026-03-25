"""蓝牙公共服务层：连接池、多 owner 引用计数、统一事件通知。

插件仅允许调用本模块暴露的 BluetoothService API，不得持有底层 Backend 实例。
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import Dict, Optional, Set

from core.event_bus import EventBus
from core.logging_config import get_plugin_logger

from bluetooth.backends import BluetoothBackend, SimulatedBluetoothBackend
from bluetooth.constants import (
    EVT_CONNECTED,
    EVT_DATA_RECEIVED,
    EVT_DISCONNECTED,
    EVT_READY,
    EVT_ERROR,
    EVT_STATE_CHANGED,
    KEY_CODE,
    KEY_DATA,
    KEY_MAC,
    KEY_CHARACTERISTIC_UUID,
    KEY_MESSAGE,
    KEY_NEW_STATE,
    KEY_OLD_STATE,
    KEY_OWNER,
    KEY_REASON,
    KEY_REF_COUNT,
    KEY_SOURCE,
)


def normalize_mac(mac: str) -> str:
    """统一为大写十六进制对格式 AA:BB:CC:DD:EE:FF。"""
    s = mac.strip().upper().replace("-", ":").replace(" ", "")
    parts = re.split(r"[:]", s)
    parts = [p for p in parts if p]
    if len(parts) == 6 and all(len(p) == 2 for p in parts):
        return ":".join(parts)
    # 无冒号连续 12 hex
    if len(s) == 12 and all(c in "0123456789ABCDEF" for c in s):
        return ":".join(s[i : i + 2] for i in range(0, 12, 2))
    return s


def normalize_uuid(uuid: str) -> str:
    """统一 UUID 的字符串展示与比较（小写），兼容 Qt 输出的 {uuid} 格式。"""
    s = (uuid or "").strip().lower()
    # PySide6/Qt 经常把 uuid 输出成带花括号 "{...}"
    if s.startswith("{") and s.endswith("}"):
        s = s[1:-1]
    # 进一步兜底：如果中间也夹杂花括号则清理
    s = s.replace("{", "").replace("}", "")
    return s


@dataclass
class _PooledEntry:
    mac: str
    owners: Dict[str, int] = field(default_factory=dict)  # owner_id -> 引用次数
    state: str = "disconnected"  # disconnected | connecting | connected | error
    backend_open: bool = False


class BluetoothService:
    """Central 侧统一托管：connect(mac,owner) / disconnect(mac,owner) / send(mac,data)。

    - 同一 MAC 多次 connect（不同或相同 owner）按引用计数复用物理连接
    - 仅当某 MAC 的总引用降为 0 时才真正 close 底层连接
    - 通过 EventBus 发布 bluetooth.* 事件
    """

    def __init__(
        self,
        event_bus: EventBus,
        *,
        max_connections: int = 8,
        backend: Optional[BluetoothBackend] = None,
    ) -> None:
        self._bus = event_bus
        self._max = max(1, max_connections)
        self._backend = backend or SimulatedBluetoothBackend()
        self._lock = threading.RLock()
        self._pool: Dict[str, _PooledEntry] = {}
        # (mac, characteristic_uuid, owner) -> ref_count
        self._notify_ref: Dict[tuple[str, str, str], int] = {}
        # mac -> write characteristic_uuid（本工程当前假设 write/notify 同一 uuid）
        self._write_char_by_mac: Dict[str, str] = {}
        # (mac, characteristic_uuid) -> ready
        self._ready_chars: Dict[tuple[str, str], bool] = {}
        # (mac, characteristic_uuid) -> pending write queue
        self._pending_writes: Dict[tuple[str, str], list[bytes]] = {}
        self._logger = get_plugin_logger("BluetoothService")

    def is_char_ready(self, mac: str, characteristic_uuid: str) -> bool:
        mac_n = normalize_mac(mac)
        char_n = normalize_uuid(characteristic_uuid)
        if not mac_n or not char_n:
            return False
        return bool(self._ready_chars.get((mac_n, char_n), False))

    def _publish(self, event: str, data: dict, source: str = "bluetooth_service") -> None:
        payload = dict(data)
        payload.setdefault(KEY_SOURCE, source)
        self._bus.publish(event, payload, source_plugin=None)

    def _set_state(self, mac: str, entry: _PooledEntry, new_state: str, reason: str = "") -> None:
        old = entry.state
        if old == new_state:
            return
        entry.state = new_state
        self._publish(
            EVT_STATE_CHANGED,
            {
                KEY_MAC: mac,
                KEY_OLD_STATE: old,
                KEY_NEW_STATE: new_state,
                KEY_REASON: reason,
            },
        )

    def connect(self, mac: str, owner: str) -> bool:
        """插件发起连接意图；成功返回 True（含复用已连接）。"""
        mac_n = normalize_mac(mac)
        if not mac_n or not owner:
            self._publish(
                EVT_ERROR,
                {
                    KEY_MAC: mac_n or mac,
                    KEY_CODE: "invalid_args",
                    KEY_MESSAGE: "mac 与 owner 不能为空",
                    KEY_OWNER: owner,
                },
            )
            return False

        with self._lock:
            entry = self._pool.get(mac_n)
            if entry is None:
                # 新建物理连接槽位前检查上限（connecting/connected 均占用槽位）
                slots_used = sum(
                    1 for e in self._pool.values() if e.state in ("connecting", "connected")
                )
                if slots_used >= self._max:
                    self._publish(
                        EVT_ERROR,
                        {
                            KEY_MAC: mac_n,
                            KEY_CODE: "max_connections",
                            KEY_MESSAGE: f"已达最大连接数 {self._max}",
                            KEY_OWNER: owner,
                        },
                    )
                    return False
                entry = _PooledEntry(mac=mac_n)
                self._pool[mac_n] = entry

            entry.owners[owner] = entry.owners.get(owner, 0) + 1
            ref_total = sum(entry.owners.values())
            self._logger.debug(
                "connect %s owner=%s ref=%s state=%s",
                mac_n,
                owner,
                ref_total,
                entry.state,
            )

            if entry.state == "connected" and entry.backend_open:
                self._publish(
                    EVT_CONNECTED,
                    {
                        KEY_MAC: mac_n,
                        KEY_OWNER: owner,
                        KEY_REF_COUNT: ref_total,
                        KEY_REASON: "reused",
                    },
                )
                return True

            if entry.state == "connecting":
                self._publish(
                    EVT_CONNECTED,
                    {
                        KEY_MAC: mac_n,
                        KEY_OWNER: owner,
                        KEY_REF_COUNT: ref_total,
                        KEY_REASON: "pending_share",
                    },
                )
                return True

            # 新建底层连接
            self._set_state(mac_n, entry, "connecting", "plugin_connect")
            entry.backend_open = True

            def on_connected() -> None:
                with self._lock:
                    e = self._pool.get(mac_n)
                    if not e or not e.backend_open:
                        return
                    self._set_state(mac_n, e, "connected", "backend_connected")
                    total = sum(e.owners.values())
                    desired_chars = {
                        char_uuid
                        for (m, char_uuid, _owner), cnt in self._notify_ref.items()
                        if m == mac_n and cnt > 0
                    }
                self._publish(
                    EVT_CONNECTED,
                    {
                        KEY_MAC: mac_n,
                        KEY_OWNER: owner,
                        KEY_REF_COUNT: total,
                        KEY_REASON: "established",
                    },
                )

                # 连接成功后，对已订阅的 characteristic 重新 enable notify（支持断线重连）
                for char_uuid in desired_chars:
                    try:
                        self._backend.enable_notify(mac_n, char_uuid)
                    except Exception as exc:
                        self._logger.error(
                            "enable_notify failed mac=%s char=%s: %s",
                            mac_n,
                            char_uuid,
                            exc,
                        )
                        self._publish(
                            EVT_ERROR,
                            {
                                KEY_MAC: mac_n,
                                KEY_CHARACTERISTIC_UUID: char_uuid,
                                KEY_CODE: "enable_notify_failed",
                                KEY_MESSAGE: str(exc),
                                KEY_OWNER: owner,
                            },
                        )

            def on_disconnected(reason: str) -> None:
                with self._lock:
                    e = self._pool.get(mac_n)
                    if not e:
                        return
                    e.backend_open = False
                    self._set_state(mac_n, e, "disconnected", reason)
                self._publish(
                    EVT_DISCONNECTED,
                    {KEY_MAC: mac_n, KEY_REASON: reason},
                )

            def on_data(characteristic_uuid: str, data: bytes) -> None:
                char_uuid_n = normalize_uuid(characteristic_uuid)
                self._publish(
                    EVT_DATA_RECEIVED,
                    {
                        KEY_MAC: mac_n,
                        KEY_CHARACTERISTIC_UUID: char_uuid_n,
                        KEY_DATA: data,
                    },
                )

            def on_ready(characteristic_uuid: str) -> None:
                char_uuid_n = normalize_uuid(characteristic_uuid)
                with self._lock:
                    e = self._pool.get(mac_n)
                    if not e or not e.backend_open:
                        return
                    self._ready_chars[(mac_n, char_uuid_n)] = True

                    # 准备 flush 待发送队列
                    pending_key = (mac_n, char_uuid_n)
                    to_flush = self._pending_writes.pop(pending_key, [])
                    write_target = self._write_char_by_mac.get(mac_n) == char_uuid_n
                    if write_target:
                        self._logger.info(
                            "[ready] mac=%s char=%s write ready",
                            mac_n,
                            char_uuid_n,
                        )

                # 触发 ready 事件：供插件侧等待
                self._publish(
                    EVT_READY,
                    {
                        KEY_MAC: mac_n,
                        KEY_CHARACTERISTIC_UUID: char_uuid_n,
                        KEY_REASON: "gatt_ready",
                    },
                )

                # flush：避免在锁内 write，减少死锁风险
                for payload in to_flush:
                    try:
                        self._backend.write(mac_n, char_uuid_n, payload)
                    except Exception as exc:
                        self._logger.error(
                            "pending write flush failed mac=%s char=%s err=%s",
                            mac_n,
                            char_uuid_n,
                            exc,
                        )
                        self._publish(
                            EVT_ERROR,
                            {
                                KEY_MAC: mac_n,
                                KEY_CHARACTERISTIC_UUID: char_uuid_n,
                                KEY_CODE: "send_failed",
                                KEY_MESSAGE: str(exc),
                                KEY_OWNER: owner,
                            },
                        )

            def on_error(code: str, message: str) -> None:
                with self._lock:
                    e = self._pool.get(mac_n)
                    if e:
                        self._set_state(mac_n, e, "error", message)
                self._publish(
                    EVT_ERROR,
                    {
                        KEY_MAC: mac_n,
                        KEY_CODE: code,
                        KEY_MESSAGE: message,
                        KEY_OWNER: owner,
                    },
                )

            try:
                self._backend.open(
                    mac_n,
                    on_connected=on_connected,
                    on_disconnected=on_disconnected,
                    on_ready=on_ready,
                    on_data=on_data,
                    on_error=on_error,
                )
            except Exception as exc:
                entry.backend_open = False
                self._set_state(mac_n, entry, "error", str(exc))
                # 回滚本次 owner 引用
                entry.owners[owner] = entry.owners.get(owner, 1) - 1
                if entry.owners[owner] <= 0:
                    del entry.owners[owner]
                if not entry.owners:
                    del self._pool[mac_n]
                self._publish(
                    EVT_ERROR,
                    {
                        KEY_MAC: mac_n,
                        KEY_CODE: "open_failed",
                        KEY_MESSAGE: str(exc),
                        KEY_OWNER: owner,
                    },
                )
                return False

            self._publish(
                EVT_CONNECTED,
                {
                    KEY_MAC: mac_n,
                    KEY_OWNER: owner,
                    KEY_REF_COUNT: ref_total,
                    KEY_REASON: "connecting",
                },
            )
            return True

    def disconnect(self, mac: str, owner: str) -> None:
        """释放该 owner 对设备的引用；仅当引用为 0 时断开底层连接。"""
        mac_n = normalize_mac(mac)
        with self._lock:
            entry = self._pool.get(mac_n)
            if not entry:
                return
            if owner in entry.owners:
                entry.owners[owner] -= 1
                if entry.owners[owner] <= 0:
                    del entry.owners[owner]
            ref_total = sum(entry.owners.values())
            self._logger.debug(
                "disconnect %s owner=%s remaining_refs=%s",
                mac_n,
                owner,
                ref_total,
            )

            if ref_total > 0:
                self._publish(
                    EVT_STATE_CHANGED,
                    {
                        KEY_MAC: mac_n,
                        KEY_OLD_STATE: entry.state,
                        KEY_NEW_STATE: entry.state,
                        KEY_REASON: f"owner_released:{owner}",
                        KEY_REF_COUNT: ref_total,
                    },
                )
                return

            # 最后一个 owner 释放
            if entry.backend_open:
                try:
                    self._backend.close(mac_n)
                except Exception as exc:
                    self._logger.error("backend.close failed %s: %s", mac_n, exc)
                entry.backend_open = False
            self._set_state(mac_n, entry, "disconnected", "all_owners_released")
            del self._pool[mac_n]
            # 清理该 MAC 的 notify 配置与写入特征（避免重复 enable 导致状态错乱）
            self._write_char_by_mac.pop(mac_n, None)
            for key in list(self._notify_ref.keys()):
                if key[0] == mac_n:
                    del self._notify_ref[key]
            # 清理 ready 与 pending write
            for (m, c) in list(self._ready_chars.keys()):
                if m == mac_n:
                    del self._ready_chars[(m, c)]
            for (m, c) in list(self._pending_writes.keys()):
                if m == mac_n:
                    del self._pending_writes[(m, c)]

        self._publish(
            EVT_DISCONNECTED,
            {KEY_MAC: mac_n, KEY_REASON: "all_owners_released", KEY_OWNER: owner},
        )

    def send(self, mac: str, data: bytes) -> bool:
        """向已连接设备发送数据；插件不得直接操作 socket/BLE 对象。"""
        mac_n = normalize_mac(mac)
        with self._lock:
            entry = self._pool.get(mac_n)
            if not entry or entry.state != "connected" or not entry.backend_open:
                self._publish(
                    EVT_ERROR,
                    {
                        KEY_MAC: mac_n,
                        KEY_CODE: "not_connected",
                        KEY_MESSAGE: "设备未连接或正在连接中",
                    },
                )
                return False

            char_uuid = self._write_char_by_mac.get(mac_n)
            if not char_uuid:
                self._publish(
                    EVT_ERROR,
                    {
                        KEY_MAC: mac_n,
                        KEY_CODE: "write_char_not_configured",
                        KEY_MESSAGE: "未启用 notify/未配置写入 characteristic_uuid",
                    },
                )
                return False

            # 核心：write 只允许在目标 characteristic ready 后执行
            if not self.is_char_ready(mac_n, char_uuid):
                key = (mac_n, char_uuid)
                self._pending_writes.setdefault(key, []).append(data)
                self._logger.info(
                    "[send] queued (not ready yet) mac=%s char=%s len=%s queue=%s",
                    mac_n,
                    char_uuid,
                    len(data),
                    len(self._pending_writes.get(key, [])),
                )
                # 标记已接受（排队），上层不要用它作为失败重试依据
                return True
        try:
            self._logger.info(
                "[send] write now mac=%s char=%s len=%s",
                mac_n,
                char_uuid,
                len(data),
            )
            self._backend.write(mac_n, char_uuid, data)
            return True
        except Exception as exc:
            self._publish(
                EVT_ERROR,
                {
                    KEY_MAC: mac_n,
                    KEY_CHARACTERISTIC_UUID: char_uuid,
                    KEY_CODE: "send_failed",
                    KEY_MESSAGE: str(exc),
                },
            )
            return False

    def enable_notify(self, mac: str, characteristic_uuid: str, owner: str = "__default__") -> bool:
        """启用指定 characteristic 的 notify（通过 CCCD）。"""
        mac_n = normalize_mac(mac)
        char_uuid_n = normalize_uuid(characteristic_uuid)
        if not mac_n or not char_uuid_n:
            self._publish(
                EVT_ERROR,
                {
                    KEY_MAC: mac_n or mac,
                    KEY_CHARACTERISTIC_UUID: char_uuid_n,
                    KEY_CODE: "invalid_args",
                    KEY_MESSAGE: "mac 与 characteristic_uuid 必须有效",
                    KEY_OWNER: owner,
                },
            )
            return False

        with self._lock:
            key = (mac_n, char_uuid_n, owner or "__default__")
            self._notify_ref[key] = self._notify_ref.get(key, 0) + 1
            # 约定：本协议 write/notify 同一 uuid
            self._write_char_by_mac[mac_n] = char_uuid_n

            entry = self._pool.get(mac_n)
            if not entry or not entry.backend_open or entry.state != "connected":
                # 连接还没建立：先缓存订阅，连接完成后在 on_connected 中启用
                return True

        try:
            self._backend.enable_notify(mac_n, char_uuid_n)
            return True
        except Exception as exc:
            self._logger.error(
                "enable_notify failed mac=%s char=%s: %s", mac_n, char_uuid_n, exc
            )
            self._publish(
                EVT_ERROR,
                {
                    KEY_MAC: mac_n,
                    KEY_CHARACTERISTIC_UUID: char_uuid_n,
                    KEY_CODE: "enable_notify_failed",
                    KEY_MESSAGE: str(exc),
                    KEY_OWNER: owner,
                },
            )
            return False

    def disable_notify(
        self, mac: str, characteristic_uuid: str, owner: str = "__default__"
    ) -> None:
        """释放 owner 对 (mac, characteristic_uuid) 的 notify 引用。"""
        mac_n = normalize_mac(mac)
        char_uuid_n = normalize_uuid(characteristic_uuid)
        key = (mac_n, char_uuid_n, owner or "__default__")
        with self._lock:
            if key not in self._notify_ref:
                return
            self._notify_ref[key] -= 1
            if self._notify_ref[key] <= 0:
                del self._notify_ref[key]

            # 该 (mac, char) 是否还有其它 owner 引用
            still_used = any(
                (m == mac_n and c == char_uuid_n and cnt > 0)
                for (m, c, _o), cnt in self._notify_ref.items()
            )
            entry = self._pool.get(mac_n)
            backend_open = bool(entry and entry.backend_open and entry.state == "connected")

            if not still_used:
                # 若没有其它 owner 订阅该特征，则禁用 notify
                if backend_open:
                    try:
                        self._backend.disable_notify(mac_n, char_uuid_n)
                    except Exception as exc:
                        self._logger.error(
                            "disable_notify failed mac=%s char=%s: %s",
                            mac_n,
                            char_uuid_n,
                            exc,
                        )
                        self._publish(
                            EVT_ERROR,
                            {
                                KEY_MAC: mac_n,
                                KEY_CHARACTERISTIC_UUID: char_uuid_n,
                                KEY_CODE: "disable_notify_failed",
                                KEY_MESSAGE: str(exc),
                                KEY_OWNER: owner,
                            },
                        )

                # 清理写入特征：如果当前写入 char 还指向它
                if self._write_char_by_mac.get(mac_n) == char_uuid_n:
                    self._write_char_by_mac.pop(mac_n, None)

    def is_connected(self, mac: str) -> bool:
        mac_n = normalize_mac(mac)
        with self._lock:
            e = self._pool.get(mac_n)
            return bool(e and e.state == "connected" and e.backend_open)

    def get_ref_count(self, mac: str) -> int:
        mac_n = normalize_mac(mac)
        with self._lock:
            e = self._pool.get(mac_n)
            return sum(e.owners.values()) if e else 0

    def list_tracked_macs(self) -> list[str]:
        with self._lock:
            return list(self._pool.keys())


__all__ = ["BluetoothService", "normalize_mac"]
