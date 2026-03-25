from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional
import xml.etree.ElementTree as ET

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


@dataclass
class PartitionRecord:
    element: ET.Element
    index: int
    name: str
    file_id: Optional[int]
    storage_id: int
    address: int
    size: int
    file_address: Optional[int]


def _parse_int(text: str, field_name: str) -> int:
    raw = (text or "").strip()
    if not raw:
        raise ValueError(f"{field_name} 为空")
    try:
        if raw.lower().startswith("0x"):
            return int(raw, 16)
        return int(raw, 10)
    except ValueError as e:
        raise ValueError(f"{field_name} 不是合法数字: {raw!r}") from e


def _fmt_hex(value: int) -> str:
    if value < 0:
        raise ValueError(f"地址/大小不能为负数: {value}")
    return f"0x{value:X}"


def _get_child_text(el: ET.Element, tag: str) -> str:
    child = el.find(tag)
    return (child.text or "").strip() if child is not None else ""


def _set_child_text(el: ET.Element, tag: str, value: str) -> None:
    child = el.find(tag)
    if child is None:
        child = ET.SubElement(el, tag)
    child.text = value


def _load_partitions(root: ET.Element) -> list[PartitionRecord]:
    parts = root.find("partitions")
    if parts is None:
        raise ValueError("XML 缺少 <partitions> 节点")

    out: list[PartitionRecord] = []
    for idx, p in enumerate(parts.findall("partition")):
        name = _get_child_text(p, "name")
        addr = _parse_int(_get_child_text(p, "address"), f"partition[{idx}].address")
        size = _parse_int(_get_child_text(p, "size"), f"partition[{idx}].size")
        storage_raw = _get_child_text(p, "storage_id") or "0"
        storage_id = _parse_int(storage_raw, f"partition[{idx}].storage_id")

        file_addr_text = _get_child_text(p, "file_address")
        file_addr = (
            _parse_int(file_addr_text, f"partition[{idx}].file_address")
            if file_addr_text
            else None
        )
        file_id_raw = _get_child_text(p, "file_id")
        file_id: Optional[int]
        if file_id_raw:
            file_id = _parse_int(file_id_raw, f"partition[{idx}].file_id")
        else:
            file_id = None

        out.append(
            PartitionRecord(
                element=p,
                index=idx,
                name=name,
                file_id=file_id,
                storage_id=storage_id,
                address=addr,
                size=size,
                file_address=file_addr,
            )
        )
    return out


def _validate_no_overlap(parts: list[PartitionRecord], storage_id: int) -> None:
    items = [p for p in parts if p.storage_id == storage_id]
    items.sort(key=lambda x: x.address)
    for prev, curr in zip(items, items[1:]):
        prev_end = prev.address + prev.size
        if curr.address < prev_end:
            raise ValueError(
                f"storage_id={storage_id} 分区重叠: {prev.name} (end={_fmt_hex(prev_end)}) "
                f"与 {curr.name} (addr={_fmt_hex(curr.address)})"
            )


def resize_partitions_bulk(
    xml_path: Path,
    output_path: Path,
    new_size_by_index: dict[int, int],
) -> tuple[int, int]:
    """批量修改分区大小，并按 storage_id 分组顺延后续 address/file_address。

    规则：
    - 同一个 storage_id 内，按当前 address 升序视为地址分配顺序；
    - 第一项保持 address 不变；
    - 后续分区地址 = 前一分区地址 + 前一分区新 size；
    - file_address 若存在，则与 address 施加同一 delta。
    """
    tree = ET.parse(xml_path)
    root = tree.getroot()
    records = _load_partitions(root)

    # 1) 更新 size（未填写的新值沿用原值）
    for rec in records:
        if rec.index in new_size_by_index:
            val = new_size_by_index[rec.index]
            if val <= 0:
                raise ValueError(f"分区 {rec.name} 新大小必须大于 0")
            rec.size = val
            _set_child_text(rec.element, "size", _fmt_hex(val))

    # 2) 按 storage_id 分组重排地址
    moved_count = 0
    total_delta_sum = 0
    storage_ids = sorted({r.storage_id for r in records})
    for sid in storage_ids:
        group = [r for r in records if r.storage_id == sid]
        group.sort(key=lambda x: x.address)
        if not group:
            continue
        prev_end = group[0].address + group[0].size
        for rec in group[1:]:
            old_addr = rec.address
            new_addr = prev_end
            delta = new_addr - old_addr
            if delta != 0:
                rec.address = new_addr
                _set_child_text(rec.element, "address", _fmt_hex(new_addr))
                if rec.file_address is not None:
                    rec.file_address += delta
                    if rec.file_address < 0:
                        raise ValueError(f"file_address 调整后为负数: {rec.name}")
                    _set_child_text(rec.element, "file_address", _fmt_hex(rec.file_address))
                moved_count += 1
                total_delta_sum += abs(delta)
            prev_end = rec.address + rec.size
        _validate_no_overlap(records, sid)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)
    return moved_count, total_delta_sum


class Plugin:
    name = "XML分区大小调整"
    description = "加载全部分区列表，批量改 size 后按 storage_id 一次性顺延地址。"
    version = "1.2.0"
    author = "Toolbox Framework"
    category = "工具 / 固件布局"
    icon = ""

    def __init__(self, event_bus, config_manager, logger, plugin_id: str, plugin_dir) -> None:
        self._event_bus = event_bus
        self._config_manager = config_manager
        self._logger = logger
        self._plugin_id = plugin_id
        self._plugin_dir = Path(plugin_dir)
        self._cfg = self._config_manager.load(plugin_id)

    def on_load(self) -> None:
        self._logger.info("XmlPartitionResize Plugin loaded")

    def on_unload(self) -> None:
        self._logger.info("XmlPartitionResize Plugin unloaded")

    def _browse_xml(self, input_edit: QLineEdit) -> None:
        start = input_edit.text().strip() or str(self._plugin_dir)
        selected, _ = QFileDialog.getOpenFileName(
            None,
            "选择固件分区 XML",
            start,
            "XML Files (*.xml);;All Files (*)",
        )
        if selected:
            input_edit.setText(selected)

    def get_widget(self, parent: QWidget) -> QWidget:
        widget = QWidget(parent)
        root_layout = QVBoxLayout()
        root_layout.setContentsMargins(20, 20, 20, 20)
        root_layout.setSpacing(12)

        title = QLabel("加载分区列表后直接修改“新大小”，一键按 storage_id 顺延后续地址")

        xml_row = QHBoxLayout()
        xml_label = QLabel("XML路径：")
        xml_edit = QLineEdit()
        xml_edit.setText(self._cfg.get("xml_path", ""))
        xml_btn = QPushButton("浏览...")
        xml_btn.clicked.connect(lambda: self._browse_xml(xml_edit))
        xml_row.addWidget(xml_label)
        xml_row.addWidget(xml_edit)
        xml_row.addWidget(xml_btn)

        load_btn = QPushButton("加载分区列表")

        table = QTableWidget()
        table.setColumnCount(7)
        table.setHorizontalHeaderLabels(
            ["序号", "file_id", "storage_id", "name", "address", "当前size", "新大小"]
        )
        table.verticalHeader().setVisible(False)
        table.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        table.setMinimumHeight(220)
        table.setWordWrap(False)
        hdr = table.horizontalHeader()
        hdr.setDefaultSectionSize(96)
        for col in range(7):
            hdr.setSectionResizeMode(col, QHeaderView.ResizeMode.Stretch)

        output_row = QHBoxLayout()
        output_label = QLabel("输出XML：")
        output_edit = QLineEdit()
        output_edit.setPlaceholderText("为空时默认 *_resized.xml")
        output_edit.setText(self._cfg.get("output_path", ""))
        output_row.addWidget(output_label)
        output_row.addWidget(output_edit)

        result_label = QLabel("状态：先选择 XML 并点击“加载分区列表”")
        run_btn = QPushButton("一键顺延并导出")

        loaded_xml_path: dict[str, Path] = {}
        loaded_records: dict[int, PartitionRecord] = {}

        def fill_table(xml_path: Path) -> None:
            tree = ET.parse(xml_path)
            root = tree.getroot()
            records = _load_partitions(root)
            records.sort(key=lambda r: (r.storage_id, r.address, r.index))
            table.setRowCount(len(records))
            loaded_records.clear()
            for row, rec in enumerate(records):
                loaded_records[row] = rec
                # 序号从 1 开始（行号 + 1）
                table.setItem(row, 0, QTableWidgetItem(str(row + 1)))
                table.setItem(
                    row,
                    1,
                    QTableWidgetItem(
                        str(rec.file_id) if rec.file_id is not None else "-"
                    ),
                )
                table.setItem(row, 2, QTableWidgetItem(str(rec.storage_id)))
                table.setItem(row, 3, QTableWidgetItem(rec.name))
                table.setItem(row, 4, QTableWidgetItem(_fmt_hex(rec.address)))
                table.setItem(row, 5, QTableWidgetItem(_fmt_hex(rec.size)))
                edit_item = QTableWidgetItem(_fmt_hex(rec.size))
                table.setItem(row, 6, edit_item)

                # 除「新大小」外只读
                for col in range(6):
                    item = table.item(row, col)
                    if item is not None:
                        item.setFlags(item.flags() & ~Qt.ItemIsEditable)

            loaded_xml_path["path"] = str(xml_path)
            result_label.setText(f"状态：已加载 {len(records)} 个分区，可直接修改“新大小”列")

        def on_load_table() -> None:
            try:
                xml_path = Path(xml_edit.text().strip())
                if not xml_path.is_file():
                    raise ValueError(f"XML 文件不存在: {xml_path}")
                fill_table(xml_path)
            except Exception as e:
                self._logger.error("Load XML failed: %s", e)
                result_label.setText(f"状态：加载失败 - {e}")
                QMessageBox.critical(widget, "加载失败", str(e))

        load_btn.clicked.connect(on_load_table)

        def on_run() -> None:
            try:
                if "path" not in loaded_xml_path:
                    raise ValueError("请先加载分区列表")
                xml_path = Path(loaded_xml_path["path"])

                output_text = output_edit.text().strip()
                if output_text:
                    output_path = Path(output_text)
                else:
                    output_path = xml_path.with_name(f"{xml_path.stem}_resized{xml_path.suffix}")

                new_size_by_index: dict[int, int] = {}
                for row, rec in loaded_records.items():
                    item = table.item(row, 6)
                    if item is None:
                        continue
                    txt = item.text().strip()
                    val = _parse_int(txt, f"{rec.name}.new_size")
                    if val <= 0:
                        raise ValueError(f"{rec.name} 新大小必须大于 0")
                    if val != rec.size:
                        new_size_by_index[rec.index] = val

                moved, delta_sum = resize_partitions_bulk(
                    xml_path=xml_path,
                    output_path=output_path,
                    new_size_by_index=new_size_by_index,
                )
                result_label.setText(
                    f"状态：完成，改动size分区={len(new_size_by_index)}，顺延分区数={moved}"
                )
                self._logger.info(
                    "Resize XML done: changed=%s moved=%s total_delta_abs=%s output=%s",
                    len(new_size_by_index),
                    moved,
                    delta_sum,
                    output_path,
                )
                self._cfg.update(
                    {
                        "xml_path": str(xml_path),
                        "output_path": str(output_path),
                    }
                )
                self._config_manager.save(self._plugin_id, self._cfg)
                self._event_bus.publish(
                    "xml_partition_resize.done",
                    {
                        "xml_path": str(xml_path),
                        "output_path": str(output_path),
                        "changed_count": len(new_size_by_index),
                        "moved_count": moved,
                        "total_delta_abs": delta_sum,
                    },
                    self._plugin_id,
                )
            except Exception as e:
                self._logger.error("Resize XML failed: %s", e)
                result_label.setText(f"状态：失败 - {e}")
                QMessageBox.critical(widget, "执行失败", str(e))

        run_btn.clicked.connect(on_run)

        root_layout.addWidget(title)
        root_layout.addLayout(xml_row)
        root_layout.addWidget(load_btn)
        root_layout.addWidget(table, stretch=1)
        root_layout.addLayout(output_row)
        root_layout.addWidget(run_btn)
        root_layout.addWidget(result_label)
        widget.setLayout(root_layout)
        return widget

