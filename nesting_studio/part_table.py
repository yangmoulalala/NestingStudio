from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .models import PartEntry


class PartTablePanel(QWidget):
    addFilesRequested = Signal()
    addFolderRequested = Signal()
    loadManifestRequested = Signal()
    clearRequested = Signal()
    removeSelectedRequested = Signal()
    cloneRequested = Signal()
    copyRequested = Signal()
    pasteRequested = Signal()
    selectionChanged = Signal(object)
    message = Signal(str)

    HEADERS = ["零件名称", "类型", "数量", "厚度", "允许角度", "纹理锁定", "面积", "状态"]

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.entries: list[PartEntry] = []
        self._filtered_entries: list[PartEntry] = []
        self._updating = False
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        buttons = QHBoxLayout()
        self.add_files_button = QPushButton("\u6dfb\u52a0\u6587\u4ef6")
        self.add_folder_button = QPushButton("\u6dfb\u52a0\u76ee\u5f55")
        self.manifest_button = QPushButton("\u8bfb\u53d6\u6e05\u5355")
        self.remove_button = QPushButton("\u79fb\u9664\u9009\u4e2d")
        self.clone_button = QPushButton("\u514b\u9686")
        self.copy_button = QPushButton("\u590d\u5236")
        self.paste_button = QPushButton("\u7c98\u8d34")
        self.clear_button = QPushButton("\u6e05\u7a7a")
        for button in (
            self.add_files_button,
            self.add_folder_button,
            self.manifest_button,
            self.remove_button,
            self.clone_button,
            self.copy_button,
            self.paste_button,
            self.clear_button,
        ):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("\u641c\u7d22\u540d\u79f0"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("\u8f93\u5165\u96f6\u4ef6\u540d\u79f0\u8fdb\u884c\u8fc7\u6ee4...")
        self.search_edit.setClearButtonEnabled(True)
        search_row.addWidget(self.search_edit, 1)
        self.resize_name_button = QPushButton("\u540d\u79f0\u5217\u81ea\u9002\u5e94")
        search_row.addWidget(self.resize_name_button)
        layout.addLayout(search_row)

        self.table = QTableWidget(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.setWordWrap(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setMinimumSectionSize(70)
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.table, 1)
        self.table.setColumnWidth(0, 320)

        self.summary_label = QLabel("零件类型 0，总数量 0")
        self.summary_label.setStyleSheet("color:#aeb4bb;")
        layout.addWidget(self.summary_label)

        self.add_files_button.clicked.connect(self.addFilesRequested.emit)
        self.add_folder_button.clicked.connect(self.addFolderRequested.emit)
        self.manifest_button.clicked.connect(self.loadManifestRequested.emit)
        self.remove_button.clicked.connect(self.removeSelectedRequested.emit)
        self.clone_button.clicked.connect(self.cloneRequested.emit)
        self.copy_button.clicked.connect(self.copyRequested.emit)
        self.paste_button.clicked.connect(self.pasteRequested.emit)
        self.clear_button.clicked.connect(self.clearRequested.emit)
        self.table.customContextMenuRequested.connect(self._show_context_menu)
        self.table.itemSelectionChanged.connect(self._emit_selection)
        self.table.itemChanged.connect(self._on_item_changed)
        self.search_edit.textChanged.connect(self.refresh)
        self.resize_name_button.clicked.connect(self.resize_name_column)

    def resize_name_column(self) -> None:
        width = max(180, min(650, self.table.sizeHintForColumn(0) + 24))
        self.table.setColumnWidth(0, width)

    def _show_context_menu(self, position) -> None:
        menu = QMenu(self)
        clone_action = menu.addAction("\u514b\u9686")
        copy_action = menu.addAction("\u590d\u5236")
        paste_action = menu.addAction("\u7c98\u8d34")
        menu.addSeparator()
        remove_action = menu.addAction("\u79fb\u9664")
        selected_action = menu.exec(self.table.viewport().mapToGlobal(position))
        if selected_action == clone_action:
            self.cloneRequested.emit()
        elif selected_action == copy_action:
            self.copyRequested.emit()
        elif selected_action == paste_action:
            self.pasteRequested.emit()
        elif selected_action == remove_action:
            self.removeSelectedRequested.emit()

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copyRequested.emit()
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Paste):
            self.pasteRequested.emit()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Delete:
            self.removeSelectedRequested.emit()
            event.accept()
            return
        super().keyPressEvent(event)

    def set_entries(self, entries: list[PartEntry]) -> None:
        self.entries = entries
        self.refresh()

    def refresh(self) -> None:
        self._updating = True
        query = self.search_edit.text().strip().casefold()
        if query:
            self._filtered_entries = [
                entry
                for entry in self.entries
                if query in entry.display_name.casefold()
                or query in entry.source_text.casefold()
                or query in entry.source_type.casefold()
            ]
        else:
            self._filtered_entries = list(self.entries)
        self.table.setRowCount(len(self._filtered_entries))
        for row, entry in enumerate(self._filtered_entries):
            name_item = QTableWidgetItem(entry.display_name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            name_item.setToolTip(
                entry.source_text
                + (
                    "\n\u6b63\u9762\u671d\u4e0a\u5df2\u9501\u5b9a"
                    if entry.definition is not None and entry.definition.face_up_locked
                    else "\n\u5141\u8bb8\u7ffb\u9762"
                )
            )
            self.table.setItem(row, 0, name_item)

            type_item = QTableWidgetItem(entry.source_type)
            type_item.setFlags(type_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row, 1, type_item)

            quantity_item = QTableWidgetItem(str(entry.request.quantity))
            quantity_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 2, quantity_item)

            thickness_text = (
                "" if entry.thickness_mm is None else f"{entry.thickness_mm:.4f}".rstrip("0").rstrip(".")
            )
            thickness_item = QTableWidgetItem(thickness_text)
            thickness_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            if entry.is_step:
                thickness_item.setFlags(thickness_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                thickness_item.setToolTip("STEP 厚度由几何自动计算，不能手工修改")
            else:
                thickness_item.setToolTip("请输入可信厚度；程序不会从名称推断")
            if entry.thickness_mm is None and not entry.is_step:
                thickness_item.setBackground(QColor("#6b3d16"))
            self.table.setItem(row, 3, thickness_item)

            rotations_item = QTableWidgetItem(entry.rotations_text)
            rotations_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 4, rotations_item)

            grain_item = QTableWidgetItem()
            grain_item.setFlags(
                (grain_item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                & ~Qt.ItemFlag.ItemIsEditable
            )
            grain_item.setCheckState(
                Qt.CheckState.Checked
                if entry.request.grain_locked
                else Qt.CheckState.Unchecked
            )
            grain_item.setText("锁定" if entry.request.grain_locked else "自由")
            grain_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 5, grain_item)

            area_item = QTableWidgetItem(f"{entry.area_mm2:.3f}")
            area_item.setFlags(area_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            area_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            self.table.setItem(row, 6, area_item)

            status = entry.error or ("就绪" if entry.definition else "待解析")
            status_item = QTableWidgetItem(status)
            status_item.setFlags(status_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if entry.error:
                status_item.setForeground(QColor("#ff8a80"))
            self.table.setItem(row, 7, status_item)

        self.table.resizeRowsToContents()
        self._updating = False
        total_quantity = sum(entry.request.quantity for entry in self.entries)
        self.summary_label.setText(
            f"\u663e\u793a {len(self._filtered_entries)}/{len(self.entries)} \u79cd\uff0c"
            f"\u603b\u6570\u91cf {total_quantity}"
        )

    def _emit_selection(self) -> None:
        if self._updating:
            return
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        entry = (
            self._filtered_entries[rows[0]]
            if rows and rows[0] < len(self._filtered_entries)
            else None
        )
        self.selectionChanged.emit(entry)

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating or item.row() >= len(self._filtered_entries):
            return
        entry = self._filtered_entries[item.row()]
        try:
            if item.column() == 2:
                entry.set_quantity(int(item.text()))
            elif item.column() == 3:
                text = item.text().strip()
                entry.set_thickness(None if not text else float(text))
            elif item.column() == 4:
                entry.set_rotations_text(item.text())
            elif item.column() == 5:
                checked = item.checkState() == Qt.CheckState.Checked
                entry.request.grain_locked = checked
                if entry.definition is not None:
                    entry.definition.grain_locked = checked
                item.setText("锁定" if checked else "自由")
        except Exception as exc:
            self.message.emit(f"参数无效：{exc}")
            QMessageBox.warning(self, "参数无效", str(exc))
        finally:
            self._updating = True
            self.refresh()
            self._updating = False

    def apply_default_thickness(self, thickness: float | None) -> int:
        if thickness is None:
            return 0
        changed = 0
        for entry in self.entries:
            if not entry.is_step and entry.thickness_mm is None:
                entry.set_thickness(thickness)
                changed += 1
        if changed:
            self.refresh()
        return changed

    def selected_entries(self) -> list[PartEntry]:
        rows = sorted({index.row() for index in self.table.selectedIndexes()})
        return [
            self._filtered_entries[row]
            for row in rows
            if 0 <= row < len(self._filtered_entries)
        ]

    def remove_selected(self) -> None:
        selected = set(self.selected_entries())
        if not selected:
            return
        self.set_entries([entry for entry in self.entries if entry not in selected])
        self.selectionChanged.emit(None)
