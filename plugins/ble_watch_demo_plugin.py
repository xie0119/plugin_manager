"""蓝牙公共服务演示插件（模拟手表 Central 场景）。

使用 ServiceManager 获取 BluetoothService，仅通过 connect/disconnect/send 与 EventBus 事件交互。
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict

from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from PySide6.QtCore import QTimer

from bluetooth.constants import (
    EVT_CONNECTED,
    EVT_READY,
    EVT_DATA_RECEIVED,
    EVT_DISCONNECTED,
    EVT_ERROR,
    KEY_DATA,
    KEY_MAC,
    KEY_CHARACTERISTIC_UUID,
    KEY_MESSAGE,
    KEY_REASON,
    EVT_STATE_CHANGED,
)

# 设备端日志显示 service UUID 为 {178...ae0}，characteristic UUID 为 {178...ae1}
# 这里默认使用发现到的 characteristic UUID（write + notify 同一特征）。
WRITE_UUID = "178458b9-11a1-9427-4657-dfe626078ae1"

if TYPE_CHECKING:
    from core.plugin_api import PluginAPI
    from core.service_manager import ServiceManager


class Plugin:
    name = "蓝牙手表（演示）"
    description = "通过公共服务层 connect / send，订阅 bluetooth.* 事件。"
    version = "1.0.0"
    author = "Toolbox"
    category = "蓝牙 / 手表"
    icon = ""

    def __init__(
        self,
        event_bus,
        config_manager,
        logger,
        plugin_id: str,
        plugin_dir: Path,
        services: "ServiceManager",
        plugin_api: "PluginAPI",
    ) -> None:
        self._bus = event_bus
        self._logger = logger
        self._plugin_id = plugin_id
        self._services = services
        self._api = plugin_api
        self._mac = "41:42:00:00:00:10"
        # 你的固件：只有一个 characteristic（WRITE + NOTIFY），notify 回包从该 characteristic 发出
        self._char_uuid = WRITE_UUID
        self._log_widget: QTextEdit | None = None
        self._auto_send_enabled = True
        self._auto_sent_once = False

    def on_load(self) -> None:
        self._bus.subscribe(
            EVT_DATA_RECEIVED, self._plugin_id, self._on_ble_event
        )
        self._bus.subscribe(EVT_CONNECTED, self._plugin_id, self._on_ble_event)
        self._bus.subscribe(EVT_READY, self._plugin_id, self._on_ble_event)
        self._bus.subscribe(
            EVT_STATE_CHANGED, self._plugin_id, self._on_ble_event
        )
        self._bus.subscribe(EVT_DISCONNECTED, self._plugin_id, self._on_ble_event)
        self._bus.subscribe(EVT_ERROR, self._plugin_id, self._on_ble_event)
        self._logger.info("BLE demo plugin loaded")

    def on_unload(self) -> None:
        try:
            # 先释放 notify，再释放连接引用
            self._services.bluetooth.disable_notify(self._mac, self._char_uuid, self._plugin_id)
            self._services.bluetooth.disconnect(self._mac, self._plugin_id)
        except Exception:
            pass
        self._bus.unsubscribe(EVT_DATA_RECEIVED, self._on_ble_event)
        self._bus.unsubscribe(EVT_CONNECTED, self._on_ble_event)
        self._bus.unsubscribe(EVT_READY, self._on_ble_event)
        self._bus.unsubscribe(EVT_STATE_CHANGED, self._on_ble_event)
        self._bus.unsubscribe(EVT_DISCONNECTED, self._on_ble_event)
        self._bus.unsubscribe(EVT_ERROR, self._on_ble_event)

    def _append(self, text: str) -> None:
        if self._log_widget is not None:
            self._log_widget.append(text)

    def _on_ble_event(self, event: str, data: Dict[str, Any]) -> None:
        # 事件里带 mac，但连接后/断线后不会带 characteristic_uuid；这里只做通用日志
        if event == EVT_CONNECTED:
            if data.get(KEY_MAC) == self._mac:
                self._append(
                    f"[连接] reason={data.get(KEY_REASON)} ref={data.get('ref_count')}"
                )
            return

        if event == EVT_READY:
            mac_ok = data.get(KEY_MAC) == self._mac
            # 后端/服务层会 normalize_uuid；UI 输入/常量可能带大小写/花括号
            ready_char = self._api.normalize_uuid(str(data.get(KEY_CHARACTERISTIC_UUID, "")))
            char_ok = ready_char == self._api.normalize_uuid(self._char_uuid)
            if mac_ok and char_ok:
                self._append(f"[ready] {self._char_uuid} 已可写")
                if self._auto_send_enabled and not self._auto_sent_once:
                    self._auto_sent_once = True
                    payload = b"HELLO_WATCH"
                    try:
                        ok = self._services.bluetooth.send(self._mac, payload)
                    except Exception as exc:
                        ok = False
                        self._append(f"[错误] auto send exception: {exc}")
                    self._append(f"auto send on ready {payload!r} -> {ok}")
            elif mac_ok and ready_char:
                # 若用户填错 UUID，至少把当前 ready 的 uuid 提示出来，便于直接复制到输入框
                self._append(f"[ready] 发现可写特征 uuid={ready_char}（当前输入={self._char_uuid}）")
            return

        if event == EVT_STATE_CHANGED:
            if data.get(KEY_MAC) == self._mac:
                self._append(
                    f"[状态] {data.get('old_state')} -> {data.get('new_state')} reason={data.get(KEY_REASON)}"
                )
            # 继续让后面的分支处理（例如 error/data）
            # 这里不 return，交给下方分支

        if event == EVT_DATA_RECEIVED:
            raw = data.get(KEY_DATA)
            char_uuid = data.get(KEY_CHARACTERISTIC_UUID, "")
            if (
                str(char_uuid).lower().replace("-", "")
                != str(self._char_uuid).lower().replace("-", "")
            ):
                return
            if isinstance(raw, (bytes, bytearray)):
                self._append(f"[RX] {raw.hex()}")
            else:
                self._append(f"[RX] {raw!r}")
        elif event == EVT_DISCONNECTED:
            self._append(f"[断开] {data.get(KEY_MAC)} {data!r}")
        elif event == EVT_ERROR:
            self._append(f"[错误] {data.get(KEY_MESSAGE)}")

    def get_widget(self, parent: QWidget) -> QWidget:
        w = QWidget(parent)
        layout = QVBoxLayout(w)

        row = QHBoxLayout()
        row.addWidget(QLabel("手表 MAC:"))
        mac_edit = QLineEdit(self._mac)
        row.addWidget(mac_edit)
        layout.addLayout(row)

        bt = self._services.bluetooth

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Characteristic UUID:"))
        char_edit = QLineEdit(self._char_uuid)
        row2.addWidget(char_edit)
        layout.addLayout(row2)

        # 调试用：连接建立后自动发送测试数据，便于确认 notify + 协议栈是否通了
        auto_send_cb = QPushButton("连接后自动发送测试：开")
        auto_send_cb.setCheckable(False)
        auto_send_cb.setChecked(False)
        layout.addWidget(auto_send_cb)

        def on_auto_toggle(checked: bool) -> None:
            self._auto_send_enabled = bool(checked)
            auto_send_cb.setText(
                "连接后自动发送测试：开" if checked else "连接后自动发送测试：关"
            )

        auto_send_cb.toggled.connect(on_auto_toggle)

        def do_connect() -> None:
            self._mac = self._api.normalize_mac(mac_edit.text().strip() or self._mac)
            self._char_uuid = char_edit.text().strip() or self._char_uuid
            ok = bt.connect(self._mac, self._plugin_id)
            # 启用 notify（本固件中 write/notify 是同一 characteristic）
            bt.enable_notify(self._mac, self._char_uuid, self._plugin_id)
            self._append(f"connect -> {ok}")
            self._auto_sent_once = False

        def do_disconnect() -> None:
            mac = self._api.normalize_mac(mac_edit.text().strip() or self._mac)
            char_uuid = char_edit.text().strip() or self._char_uuid
            bt.disable_notify(mac, char_uuid, self._plugin_id)
            bt.disconnect(mac, self._plugin_id)
            self._append("disconnect 已请求")

        def do_send() -> None:
            payload = bytes([ 0x55, 0x00, 0xFE, 0x27, 0x01, 0x80, 0x18, 0x00, 0x9A, 0x00, 0x08, 0x00, 0x21, 0x05, 0xCB, 0x06, 0xF9, 0x38, 0x58, 0x01])
            # 防止用户手动过早发送：写入 characteristic 需要等 char_map 生成/CCCD 生效
            max_attempts = 20
            interval_ms = 500
            attempts = {"i": 0}

            def _try_send() -> None:
                attempts["i"] += 1
                try:
                    mac = mac_edit.text().strip() or self._mac
                    char_uuid = char_edit.text().strip() or self._char_uuid
                    ready = bt.is_char_ready(mac, char_uuid)
                    ok = bt.send(mac, payload)
                    self._append(
                        f"send attempt {attempts['i']}/{max_attempts} ready={ready} {payload!r} -> {ok}"
                    )
                except Exception as exc:
                    self._append(f"[错误] send exception: {exc}")
                    ok = False

                # ok=True 可能代表“已排队”（未 ready），因此以 ready 作为真正成功条件
                if (ok and ready) or attempts["i"] >= max_attempts:
                    return
                QTimer.singleShot(interval_ms, _try_send)

            QTimer.singleShot(interval_ms, _try_send)

        b1 = QPushButton("连接（公共服务）")
        b1.clicked.connect(do_connect)
        b2 = QPushButton("断开（释放本插件引用）")
        b2.clicked.connect(do_disconnect)
        b3 = QPushButton("发送测试数据")
        b3.clicked.connect(do_send)
        layout.addWidget(b1)
        layout.addWidget(b2)
        layout.addWidget(b3)

        self._log_widget = QTextEdit()
        self._log_widget.setReadOnly(True)
        self._log_widget.setPlaceholderText("事件日志…")
        layout.addWidget(self._log_widget)

        return w
