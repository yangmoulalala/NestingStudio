from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from PySide6.QtWidgets import QApplication
from shapely.affinity import rotate, scale, translate
from shapely.ops import unary_union
from shapely import wkt

from PySide6.QtCore import QCoreApplication, QSettings, QTimer, Qt, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QUndoCommand, QUndoStack
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QSplitter,
    QStyle,
    QToolBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from nesting.engine import NestingEngine
from nesting.geometry import polygon_interiors
from nesting.inputs import (
    PartRequest,
    group_definitions_by_thickness,
    load_manifest,
    load_requests,
    request_from_mapping,
)
from nesting.models import NestingSolution, PartDefinition, PartInstance, Placement, SheetLayout
from nesting.output import (
    export_sheet_dxf,
    solution_to_dict,
    validate_solution,
    write_csv_report,
    write_json_report,
)
from nesting.pipeline import GroupNestingResult, GroupedNestingResult

from .canvas import LayoutCanvas, PlacementState
from .config_panel import ConfigPanel
from .logging_config import get_log_path
from .models import PartEntry
from .part_table import PartTablePanel
from .project_io import import_global_summary, is_global_summary
from .version import __version__
from .workers import LoadPartsWorker, NestingWorker


logger = logging.getLogger(__name__)


SUPPORTED_FILTER = (
    "CAD 轮廓 (*.step *.stp *.dxf *.svg *.json);;"
    "STEP (*.step *.stp);;DXF (*.dxf);;SVG (*.svg);;JSON (*.json);;所有文件 (*)"
)


class MovePlacementCommand(QUndoCommand):
    def __init__(
        self,
        canvas: LayoutCanvas,
        placement,
        old_state: PlacementState,
        new_state: PlacementState,
    ) -> None:
        super().__init__(f"\u79fb\u52a8 {placement.label}")
        self.canvas = canvas
        self.sheet = canvas._sheet
        self.placement = placement
        self.old_state = old_state
        self.new_state = new_state

    def undo(self) -> None:
        self.canvas.apply_placement_state(
            self.placement,
            self.old_state,
            sheet=self.sheet,
        )

    def redo(self) -> None:
        self.canvas.apply_placement_state(
            self.placement,
            self.new_state,
            sheet=self.sheet,
        )


class TransformPlacementCommand(QUndoCommand):
    def __init__(self, canvas, placement, old_state, new_state, text: str) -> None:
        super().__init__(text)
        self.canvas = canvas
        self.placement = placement
        self.old_state = old_state
        self.new_state = new_state

    def undo(self) -> None:
        self.canvas.apply_placement_state(self.placement, self.old_state)

    def redo(self) -> None:
        self.canvas.apply_placement_state(self.placement, self.new_state)


class AddPlacementCommand(QUndoCommand):
    def __init__(self, canvas, sheet, placement, text: str = "\u6dfb\u52a0\u96f6\u4ef6") -> None:
        super().__init__(text)
        self.canvas = canvas
        self.sheet = sheet
        self.placement = placement

    def undo(self) -> None:
        self.canvas.remove_manual_placement(self.sheet, self.placement)
        definition = self.placement.instance.definition
        definition.quantity = max(0, definition.quantity - 1)

    def redo(self) -> None:
        self.canvas.add_manual_placement(self.sheet, self.placement)
        self.placement.instance.definition.quantity += 1


class AddPlacementsCommand(QUndoCommand):
    def __init__(self, canvas, sheet, placements, text: str = "\u6279\u91cf\u514b\u9686") -> None:
        super().__init__(text)
        self.canvas = canvas
        self.sheet = sheet
        self.placements = list(placements)

    def undo(self) -> None:
        for placement in reversed(self.placements):
            self.canvas.remove_manual_placement(self.sheet, placement, refresh=False)
            definition = placement.instance.definition
            definition.quantity = max(0, definition.quantity - 1)
        if self.sheet is self.canvas._sheet:
            self.canvas.refresh_all_placements()

    def redo(self) -> None:
        for placement in self.placements:
            self.canvas.add_manual_placement(self.sheet, placement, refresh=False)
            placement.instance.definition.quantity += 1
        if self.sheet is self.canvas._sheet:
            self.canvas.refresh_all_placements()


class DeletePlacementCommand(QUndoCommand):
    def __init__(self, canvas, sheet, placement, index: int) -> None:
        super().__init__(f"\u5220\u9664 {placement.label}")
        self.canvas = canvas
        self.sheet = sheet
        self.placement = placement
        self.index = index

    def undo(self) -> None:
        self.canvas.add_manual_placement(self.sheet, self.placement, self.index)
        self.placement.instance.definition.quantity += 1

    def redo(self) -> None:
        self.canvas.remove_manual_placement(self.sheet, self.placement)
        definition = self.placement.instance.definition
        definition.quantity = max(0, definition.quantity - 1)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("NestingStudio - 板材自动套裁")
        self.resize(1680, 980)
        self.setMinimumSize(1180, 720)

        self._load_worker: LoadPartsWorker | None = None
        self._nest_worker: NestingWorker | None = None
        self._pending_append = False
        self._result: GroupedNestingResult | None = None
        self._project_path: Path | None = None
        self._internal_clipboard: list[dict] = []
        self._pending_layout: list[dict] | None = None
        self._placement_clipboard: dict | None = None
        self._selection_context = "table"
        self._selected_placement = None
        self.undo_stack = QUndoStack(self)
        self._settings = QSettings("NestingStudio", "NestingStudio")
        self._session_save_timer = QTimer(self)
        self._session_save_timer.setSingleShot(True)
        self._session_save_timer.timeout.connect(self._save_last_session)
        self._restoring_session = False
        self._session_path = (
            Path(__file__).resolve().parents[1] / "runtime" / "last_session.json"
        )
        self.setAcceptDrops(True)

        self.part_panel = PartTablePanel()
        self.config_panel = ConfigPanel()
        self.canvas = LayoutCanvas()
        self.sheet_tree = QTreeWidget()
        self.sheet_tree.setHeaderLabel("厚度组 / 板材")
        self.sheet_tree.setMinimumWidth(250)

        self._build_actions()
        self._build_toolbar()
        self._build_docks()
        self._build_menus()
        self._build_central()
        self._build_statusbar()
        self._connect_signals()
        if QCoreApplication.applicationName() == "NestingStudio":
            self._load_last_config()
            QTimer.singleShot(0, self._restore_last_session)
        self._update_actions()

    def _build_actions(self) -> None:
        style = self.style()
        self.action_new = QAction(style.standardIcon(QStyle.StandardPixmap.SP_FileIcon), "新建", self)
        self.action_new.setShortcut(QKeySequence.StandardKey.New)
        self.action_open = QAction(style.standardIcon(QStyle.StandardPixmap.SP_DialogOpenButton), "打开项目", self)
        self.action_open.setShortcut(QKeySequence.StandardKey.Open)
        self.action_save = QAction(style.standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton), "保存项目", self)
        self.action_save.setShortcut(QKeySequence.StandardKey.Save)
        self.action_add_files = QAction(style.standardIcon(QStyle.StandardPixmap.SP_FileDialogNewFolder), "添加文件", self)
        self.action_add_folder = QAction(style.standardIcon(QStyle.StandardPixmap.SP_DirOpenIcon), "添加目录", self)
        self.action_manifest = QAction("读取 JSON 清单", self)
        self.action_run = QAction(style.standardIcon(QStyle.StandardPixmap.SP_MediaPlay), "开始套裁", self)
        self.action_run.setShortcut(QKeySequence("F5"))
        self.action_stop = QAction(style.standardIcon(QStyle.StandardPixmap.SP_MediaStop), "停止", self)
        self.action_fit = QAction(style.standardIcon(QStyle.StandardPixmap.SP_TitleBarMaxButton), "适合窗口", self)
        self.action_zoom_in = QAction("放大", self)
        self.action_zoom_out = QAction("缩小", self)
        self.action_export = QAction(style.standardIcon(QStyle.StandardPixmap.SP_DialogSaveButton), "导出 DXF", self)
        self.action_export.setShortcut(QKeySequence("Ctrl+E"))
        self.action_clone = QAction("\u514b\u9686", self)
        self.action_clone.setShortcut(QKeySequence("Ctrl+D"))
        self.action_copy = QAction("\u590d\u5236", self)
        self.action_copy.setShortcut(QKeySequence.StandardKey.Copy)
        self.action_paste = QAction("\u7c98\u8d34", self)
        self.action_paste.setShortcut(QKeySequence.StandardKey.Paste)
        self.action_delete = QAction("\u5220\u9664", self)
        self.action_delete.setShortcut(QKeySequence.StandardKey.Delete)
        self.action_rotate_ccw = QAction("\u9006\u65f6\u9488 90\u00b0", self)
        self.action_rotate_cw = QAction("\u987a\u65f6\u9488 90\u00b0", self)
        self.action_rotate_180 = QAction("\u65cb\u8f6c 180\u00b0", self)
        self.action_rotate_custom = QAction("\u81ea\u5b9a\u4e49\u89d2\u5ea6...", self)
        self.action_flip_h = QAction("\u6c34\u5e73\u7ffb\u9762", self)
        self.action_flip_v = QAction("\u5782\u76f4\u7ffb\u9762", self)
        self.action_measure = QAction("\u6d4b\u91cf", self)
        self.action_measure.setCheckable(True)
        self.action_measure.setShortcut(QKeySequence("Ctrl+M"))
        self.action_clear_measure = QAction("\u6e05\u9664\u6d4b\u91cf", self)
        self.action_undo = self.undo_stack.createUndoAction(self, "\u64a4\u9500")
        self.action_undo.setShortcut(QKeySequence.StandardKey.Undo)
        self.action_redo = self.undo_stack.createRedoAction(self, "\u91cd\u505a")
        self.action_redo.setShortcut(QKeySequence.StandardKey.Redo)
        self.action_grid = QAction("\u7f51\u683c\u5438\u9644", self)
        self.action_grid.setCheckable(True)
        self.action_grid.setChecked(True)
        self.action_exit = QAction("\u9000\u51fa", self)
        self.action_about = QAction("\u5173\u4e8e", self)
        self.action_logs = QAction("\u6253\u5f00\u65e5\u5fd7", self)
        self.action_documentation = QAction("\u6253\u5f00\u6587\u6863", self)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("主工具栏", self)
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(toolbar)

        for action in (
            self.action_new,
            self.action_open,
            self.action_save,
            None,
            self.action_add_files,
            self.action_add_folder,
            self.action_manifest,
            None,
            self.action_clone,
            self.action_copy,
            self.action_paste,
            self.action_delete,
            self.action_rotate_ccw,
            self.action_rotate_cw,
            self.action_rotate_180,
            self.action_rotate_custom,
            self.action_flip_h,
            self.action_flip_v,
            None,
            self.action_undo,
            self.action_redo,
            None,
            self.action_run,
            self.action_stop,
            None,
            self.action_fit,
            self.action_zoom_in,
            self.action_zoom_out,
            self.action_grid,
            self.action_measure,
            self.action_clear_measure,
            None,
            self.action_export,
        ):
            if action is None:
                toolbar.addSeparator()
            else:
                toolbar.addAction(action)

    def _build_docks(self) -> None:
        part_dock = QDockWidget("零件工作区", self)
        part_dock.setObjectName("partDock")
        part_dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        part_dock.setWidget(self.part_panel)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, part_dock)

        config_dock = QDockWidget("参数与结果", self)
        config_dock.setObjectName("configDock")
        config_dock.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        config_dock.setMinimumWidth(330)
        config_dock.setWidget(self.config_panel)
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, config_dock)

        self.part_dock = part_dock
        self.config_dock = config_dock

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("\u6587\u4ef6")
        file_menu.addAction(self.action_new)
        file_menu.addAction(self.action_open)
        file_menu.addAction(self.action_save)
        file_menu.addSeparator()
        file_menu.addAction(self.action_add_files)
        file_menu.addAction(self.action_add_folder)
        file_menu.addAction(self.action_manifest)
        file_menu.addSeparator()
        file_menu.addAction(self.action_export)
        file_menu.addSeparator()
        file_menu.addAction(self.action_exit)

        edit_menu = self.menuBar().addMenu("\u7f16\u8f91")
        edit_menu.addAction(self.action_undo)
        edit_menu.addAction(self.action_redo)
        edit_menu.addSeparator()
        edit_menu.addAction(self.action_clone)
        edit_menu.addAction(self.action_copy)
        edit_menu.addAction(self.action_paste)
        edit_menu.addAction(self.action_delete)
        edit_menu.addSeparator()
        edit_menu.addAction(self.action_rotate_ccw)
        edit_menu.addAction(self.action_rotate_cw)
        edit_menu.addAction(self.action_rotate_180)
        edit_menu.addAction(self.action_rotate_custom)
        edit_menu.addAction(self.action_flip_h)
        edit_menu.addAction(self.action_flip_v)
        edit_menu.addSeparator()
        edit_menu.addAction(self.action_measure)
        edit_menu.addAction(self.action_clear_measure)

        view_menu = self.menuBar().addMenu("\u89c6\u56fe")
        view_menu.addAction(self.action_fit)
        view_menu.addAction(self.action_zoom_in)
        view_menu.addAction(self.action_zoom_out)
        view_menu.addSeparator()
        view_menu.addAction(self.part_dock.toggleViewAction())
        view_menu.addAction(self.config_dock.toggleViewAction())

        help_menu = self.menuBar().addMenu("\u5e2e\u52a9")
        help_menu.addAction(self.action_logs)
        help_menu.addAction(self.action_documentation)
        help_menu.addAction(self.action_about)

    def _build_central(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        layer_bar = QFrame()
        layer_layout = QHBoxLayout(layer_bar)
        layer_layout.setContentsMargins(6, 4, 6, 4)
        self.layer_sheet = QCheckBox("板材")
        self.layer_margin = QCheckBox("有效边界")
        self.layer_holes = QCheckBox("内孔")
        self.layer_labels = QCheckBox("标签")
        self.layer_leads = QCheckBox("引线")
        for checkbox in (
            self.layer_sheet,
            self.layer_margin,
            self.layer_holes,
            self.layer_labels,
            self.layer_leads,
        ):
            checkbox.setChecked(True)
            layer_layout.addWidget(checkbox)
        layer_layout.addSpacing(12)
        layer_layout.addWidget(QLabel("\u6d4b\u8ddd"))
        self.measurement_mode_combo = QComboBox()
        self.measurement_mode_combo.addItem("\u8ddd\u79bb", "distance")
        self.measurement_mode_combo.addItem("\u534a\u5f84", "radius")
        layer_layout.addWidget(self.measurement_mode_combo)
        layer_layout.addWidget(QLabel("\u6355\u6349"))
        self.snap_mode_combo = QComboBox()
        self.snap_mode_combo.addItem("\u81ea\u52a8", "auto")
        self.snap_mode_combo.addItem("\u7aef\u70b9", "endpoint")
        self.snap_mode_combo.addItem("\u4e2d\u70b9", "midpoint")
        self.snap_mode_combo.addItem("\u5706\u5fc3/\u5706\u5b54", "circle")
        self.snap_mode_combo.addItem("\u4e2d\u5fc3", "center")
        layer_layout.addWidget(self.snap_mode_combo)
        layer_layout.addSpacing(10)
        self.grid_toggle = QCheckBox("\u7f51\u683c")
        self.grid_toggle.setChecked(True)
        self.grid_size = QDoubleSpinBox()
        self.grid_size.setRange(0.1, 100.0)
        self.grid_size.setDecimals(2)
        self.grid_size.setSingleStep(0.5)
        self.grid_size.setValue(1.0)
        self.grid_size.setSuffix(" mm")
        layer_layout.addWidget(self.grid_toggle)
        layer_layout.addWidget(self.grid_size)
        layer_layout.addStretch(1)
        self.sheet_title_label = QLabel("尚未生成排版")
        self.sheet_title_label.setStyleSheet("font-weight:600;")
        layer_layout.addWidget(self.sheet_title_label)
        layout.addWidget(layer_bar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.sheet_tree)
        splitter.addWidget(self.canvas)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([260, 1100])
        layout.addWidget(splitter, 1)
        self.setCentralWidget(central)

    def _build_statusbar(self) -> None:
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.progress.setFixedWidth(280)
        self.status_label = QLabel("就绪")
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.progress)

    def _connect_signals(self) -> None:
        self.action_new.triggered.connect(self.new_project)
        self.action_open.triggered.connect(self.open_project_dialog)
        self.action_save.triggered.connect(self.save_project_dialog)
        self.action_add_files.triggered.connect(self.add_files_dialog)
        self.action_add_folder.triggered.connect(self.add_folder_dialog)
        self.action_manifest.triggered.connect(self.load_manifest_dialog)
        self.action_run.triggered.connect(self.start_nesting)
        self.action_stop.triggered.connect(self.stop_nesting)
        self.action_fit.triggered.connect(self.canvas.fit_layout)
        self.action_zoom_in.triggered.connect(self.canvas.zoom_in)
        self.action_zoom_out.triggered.connect(self.canvas.zoom_out)
        self.action_export.triggered.connect(self.export_results_dialog)
        self.action_clone.triggered.connect(self.clone_selection)
        self.action_copy.triggered.connect(self.copy_selection)
        self.action_paste.triggered.connect(self.paste_selection)
        self.action_delete.triggered.connect(self.delete_selection)
        self.action_rotate_ccw.triggered.connect(
            lambda: self.rotate_selected_placement(90.0)
        )
        self.action_rotate_cw.triggered.connect(
            lambda: self.rotate_selected_placement(-90.0)
        )
        self.action_rotate_180.triggered.connect(
            lambda: self.rotate_selected_placement(180.0)
        )
        self.action_rotate_custom.triggered.connect(self.rotate_selected_custom)
        self.action_flip_h.triggered.connect(
            lambda: self.flip_selected_placement("horizontal")
        )
        self.action_flip_v.triggered.connect(
            lambda: self.flip_selected_placement("vertical")
        )
        self.action_measure.toggled.connect(self._set_measurement_enabled)
        self.action_clear_measure.triggered.connect(self.canvas.clear_measurements)
        self.action_exit.triggered.connect(self.close)
        self.action_about.triggered.connect(self.show_about)
        self.action_documentation.triggered.connect(self.open_documentation)
        self.action_logs.triggered.connect(self.open_logs)

        self.part_panel.addFilesRequested.connect(self.add_files_dialog)
        self.part_panel.addFolderRequested.connect(self.add_folder_dialog)
        self.part_panel.loadManifestRequested.connect(self.load_manifest_dialog)
        self.part_panel.clearRequested.connect(self.clear_parts)
        self.part_panel.removeSelectedRequested.connect(self.remove_selected_parts)
        self.part_panel.cloneRequested.connect(self.clone_selected_parts)
        self.part_panel.copyRequested.connect(self.copy_selected_parts)
        self.part_panel.pasteRequested.connect(self.paste_parts)
        self.part_panel.selectionChanged.connect(self.show_part_details)
        self.part_panel.message.connect(self.show_message)

        self.config_panel.runRequested.connect(self.start_nesting)
        self.config_panel.cancelRequested.connect(self.stop_nesting)
        self.config_panel.changed.connect(self._update_actions)
        self.config_panel.changed.connect(self._schedule_session_save)

        self.sheet_tree.currentItemChanged.connect(self._on_sheet_selected)
        self.canvas.placementSelected.connect(self.show_placement_details)
        self.canvas.clonePlacementRequested.connect(self.clone_placement)
        self.canvas.copyPlacementRequested.connect(self.copy_placement)
        self.canvas.deletePlacementRequested.connect(self.delete_placement)
        self.canvas.pastePlacementRequested.connect(self.paste_selection)
        self.canvas.rotatePlacementRequested.connect(self.rotate_placement)
        self.canvas.flipPlacementRequested.connect(self.flip_placement)
        self.canvas.toggleFaceLockRequested.connect(self.toggle_face_lock)
        self.canvas.customRotatePlacementRequested.connect(self.rotate_placement_custom)
        self.canvas.measurementChanged.connect(self.show_message)
        self.canvas.placementMoveCommitted.connect(self._on_placement_moved)
        self.grid_toggle.toggled.connect(self._apply_grid)
        self.grid_size.valueChanged.connect(self._apply_grid)
        self.grid_toggle.toggled.connect(self.action_grid.setChecked)
        self.action_grid.toggled.connect(self.grid_toggle.setChecked)
        self.undo_stack.indexChanged.connect(self._refresh_validation_summary)
        self.undo_stack.indexChanged.connect(self._schedule_session_save)
        self.measurement_mode_combo.currentIndexChanged.connect(
            lambda _: self.canvas.set_measurement_mode(
                str(self.measurement_mode_combo.currentData())
            )
        )
        self.snap_mode_combo.currentIndexChanged.connect(
            lambda _: self.canvas.set_snap_mode(
                str(self.snap_mode_combo.currentData())
            )
        )

        for checkbox in (
            self.layer_sheet,
            self.layer_margin,
            self.layer_holes,
            self.layer_labels,
            self.layer_leads,
        ):
            checkbox.toggled.connect(self._apply_layers)

    def _update_actions(self) -> None:
        ready = bool(self.part_panel.entries) and self._load_worker is None
        running = self._nest_worker is not None and self._nest_worker.isRunning()
        self.action_run.setEnabled(ready and not running)
        self.action_stop.setEnabled(running)
        self.action_export.setEnabled(self._result is not None and not running)
        table_selected = bool(self.part_panel.selected_entries())
        model_selected = self._selected_placement is not None
        selected = table_selected or model_selected
        self.action_clone.setEnabled(selected and not running)
        self.action_copy.setEnabled(selected and not running)
        self.action_delete.setEnabled(selected and not running)
        for action in (
            self.action_rotate_ccw,
            self.action_rotate_cw,
            self.action_rotate_180,
            self.action_rotate_custom,
        ):
            action.setEnabled(model_selected and not running)
        face_flip_allowed = bool(
            model_selected
            and self._selected_placement is not None
            and not self._selected_placement.instance.definition.face_up_locked
        )
        self.action_flip_h.setEnabled(face_flip_allowed and not running)
        self.action_flip_v.setEnabled(face_flip_allowed and not running)
        self.action_paste.setEnabled(not running and self._load_worker is None)
        self.action_measure.setEnabled(self._result is not None and not running)
        self.action_clear_measure.setEnabled(self._result is not None and not running)
        self.action_save.setEnabled(self._load_worker is None)
        self.action_add_files.setEnabled(not running)
        self.action_add_folder.setEnabled(not running)
        self.action_manifest.setEnabled(not running)
        self.config_panel.set_running(running)

    def _set_measurement_enabled(self, enabled: bool) -> None:
        self.canvas.set_measure_mode(enabled)
        if enabled:
            self.canvas.set_measurement_mode(
                str(self.measurement_mode_combo.currentData())
            )
            self.canvas.set_snap_mode(str(self.snap_mode_combo.currentData()))

    def _apply_grid(self, *_) -> None:
        self.canvas.set_grid(self.grid_toggle.isChecked(), self.grid_size.value())

    def _on_placement_moved(self, placement, old_state, new_state) -> None:
        self.undo_stack.push(
            MovePlacementCommand(self.canvas, placement, old_state, new_state)
        )
        self.status_label.setText(
            f"\u5df2\u624b\u52a8\u8c03\u6574 {placement.label}"
            f"\uff1aX={new_state.x:.3f}, Y={new_state.y:.3f}"
        )

    def _refresh_validation_summary(self, *_) -> None:
        started = time.monotonic()
        if self.canvas._selected_placement is not None:
            self._selected_placement = self.canvas._selected_placement
        for entry in self.part_panel.entries:
            if entry.definition is not None:
                entry.request.quantity = entry.definition.quantity
        self.part_panel.refresh()
        self._update_actions()
        if self._result is None:
            return
        edited_sheet = self.canvas._last_edited_sheet or self.canvas._sheet
        for group_result in self._result.groups:
            if edited_sheet is not None and edited_sheet in group_result.solution.sheets:
                group_result.validation = validate_solution(
                    group_result.solution,
                    self._result.config,
                )
                break
        self.config_panel.update_result(self._result)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        if elapsed_ms >= 20.0:
            logger.info("Validation summary refresh elapsed_ms=%.1f", elapsed_ms)

    def _apply_layers(self) -> None:
        self.canvas.set_layers(
            show_sheet=self.layer_sheet.isChecked(),
            show_margin=self.layer_margin.isChecked(),
            show_holes=self.layer_holes.isChecked(),
            show_labels=self.layer_labels.isChecked(),
            show_leads=self.layer_leads.isChecked(),
        )

    def add_files_dialog(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "添加 CAD 零件", "", SUPPORTED_FILTER)
        if not paths:
            return
        requests = [
            PartRequest(name=Path(path).stem, path=Path(path).resolve())
            for path in paths
        ]
        self._load_requests(requests, append=True)

    def add_folder_dialog(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择零件目录")
        if not folder:
            return
        root = Path(folder).resolve()
        paths = [
            path
            for path in sorted(root.rglob("*"))
            if path.is_file() and path.suffix.lower() in {".step", ".stp", ".dxf", ".svg"}
        ]
        requests = [PartRequest(name=path.stem, path=path.resolve()) for path in paths]
        if not requests:
            QMessageBox.information(self, "没有零件", "目录中没有找到 STEP、DXF 或 SVG 文件。")
            return
        self._load_requests(requests, append=True)

    def load_manifest_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "读取 JSON 清单", "", "JSON (*.json);;所有文件 (*)")
        if not path:
            return
        try:
            requests = load_manifest(
                Path(path),
                default_quantity=1,
                default_rotations=(0.0, 90.0, 180.0, 270.0),
            )
        except Exception as exc:
            QMessageBox.critical(self, "清单读取失败", str(exc))
            return
        self._load_requests(requests, append=True)

    def _load_requests(self, requests: list[PartRequest], append: bool) -> None:
        if not requests:
            return
        default_thickness = self.config_panel.default_thickness()
        if default_thickness is not None:
            for request in requests:
                is_step = request.path is not None and request.path.suffix.lower() in {".step", ".stp"}
                if request.thickness_mm is None and not is_step:
                    request.thickness_mm = default_thickness
        self._pending_append = append
        self._load_worker = LoadPartsWorker(
            requests,
            flatten_tolerance=self.config_panel.flatten_tolerance.value(),
        )
        self._load_worker.progress.connect(self._on_load_progress)
        self._load_worker.finished_successfully.connect(self._on_parts_loaded)
        self._load_worker.failed.connect(self._on_worker_failed)
        self._load_worker.finished.connect(self._on_load_finished)
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        self.status_label.setText(f"正在读取 {len(requests)} 个零件...")
        self._update_actions()
        self._load_worker.start()

    def _on_load_progress(self, current: int, total: int, name: str) -> None:
        self.progress.setValue(int(1000 * current / max(total, 1)))
        self.status_label.setText(f"读取 {current}/{total}：{name}")

    def _on_parts_loaded(self, entries: list[PartEntry]) -> None:
        if self._pending_append:
            self.part_panel.set_entries(self.part_panel.entries + entries)
        else:
            self.part_panel.set_entries(entries)
        failed = [entry.display_name for entry in entries if entry.error]
        self.status_label.setText(
            f"已读取 {len(entries)} 种零件" + (f"，{len(failed)} 个失败" if failed else "")
        )
        self._result = None
        self.sheet_tree.clear()
        self.canvas.clear_layout()
        if not self._pending_append and self._pending_layout:
            layout_records = self._pending_layout
            self._pending_layout = None
            self._restore_layout(layout_records)
        self._restoring_session = False
        self._update_actions()
        self._schedule_session_save()

    def _on_load_finished(self) -> None:
        self._load_worker = None
        self.progress.setValue(0)
        self._update_actions()

    def _on_worker_failed(self, details: str) -> None:
        self.status_label.setText("后台任务失败")
        self.progress.setRange(0, 1000)
        self.progress.setValue(0)
        QMessageBox.critical(self, "任务失败", details)

    def clone_selection(self) -> None:
        if self._selection_context == "canvas" and self._selected_placement is not None:
            self.clone_placement(self._selected_placement)
        else:
            self.clone_selected_parts()

    def copy_selection(self) -> None:
        if self._selection_context == "canvas" and self._selected_placement is not None:
            self.copy_placement(self._selected_placement)
        else:
            self.copy_selected_parts()

    def paste_selection(self) -> None:
        text = QApplication.clipboard().text().strip()
        data = None
        if text:
            try:
                payload = json.loads(text)
                if isinstance(payload, dict) and payload.get("type") == "nesting_placement":
                    data = payload.get("placement")
            except Exception:
                pass
        if data is None:
            data = self._placement_clipboard
        if data is not None:
            self._add_placement_clone(data)
            return
        self.paste_parts()

    def delete_selection(self) -> None:
        if self._selection_context == "canvas" and self._selected_placement is not None:
            self.delete_placement(self._selected_placement)
        else:
            self.remove_selected_parts()

    def _placement_data(self, placement: Placement) -> dict:
        return {
            "type": "nesting_placement",
            "part_key": placement.instance.definition.key,
            "rotation_deg": placement.angle_deg,
            "actual_min_x": placement.actual.bounds[0],
            "actual_min_y": placement.actual.bounds[1],
            "collision_offset_x": placement.collision.bounds[0] - placement.actual.bounds[0],
            "collision_offset_y": placement.collision.bounds[1] - placement.actual.bounds[1],
            "actual_wkt": wkt.dumps(placement.actual),
            "collision_wkt": wkt.dumps(placement.collision),
            "mirrored": placement.mirrored,
        }

    def copy_placement(self, placement: Placement) -> None:
        logger.info("Copy placement: %s", placement.label)
        self._placement_clipboard = self._placement_data(placement)
        QApplication.clipboard().setText(
            json.dumps(self._placement_clipboard, ensure_ascii=False, indent=2)
        )
        self.status_label.setText(f"\u5df2\u590d\u5236\u6a21\u578b {placement.label}")

    def _next_instance_index(self) -> int:
        indexes = [
            item.instance.index
            for group_result in (self._result.groups if self._result is not None else [])
            for sheet in group_result.solution.sheets
            for item in sheet.placements
        ]
        return (max(indexes) + 1) if indexes else 0

    def _add_placement_clone(
        self,
        data: dict,
        count: int = 1,
        offset: float | None = None,
    ) -> None:
        if self._result is None or self.canvas._sheet is None:
            return
        count = max(1, int(count))
        target_key = data.get("part_key")
        definition = next(
            (
                entry.definition
                for entry in self.part_panel.entries
                if entry.definition is not None
                and entry.definition.key == target_key
            ),
            None,
        )
        if definition is None and self._result is not None:
            definition = next(
                (
                    candidate
                    for group_result in self._result.groups
                    for candidate in group_result.group.definitions
                    if candidate.key == target_key
                ),
                None,
            )
        if definition is None:
            self.status_label.setText("\u627e\u4e0d\u5230\u5f85\u514b\u9686\u96f6\u4ef6\u7684\u5b9a\u4e49")
            return
        base_shift = self.grid_size.value() * 5.0 if offset is None else float(offset)
        config = self._result.config
        placements: list[Placement] = []
        base_instance_index = self._next_instance_index()
        for clone_index in range(count):
            shift = base_shift * (clone_index + 1)
            target_min_x = float(data["actual_min_x"]) + shift
            target_min_y = float(data["actual_min_y"]) + shift
            if data.get("actual_wkt") and data.get("collision_wkt"):
                source_actual = wkt.loads(str(data["actual_wkt"]))
                source_collision = wkt.loads(str(data["collision_wkt"]))
                actual = translate(source_actual, xoff=shift, yoff=shift)
                collision = translate(source_collision, xoff=shift, yoff=shift)
                collision_min_x = collision.bounds[0]
                collision_min_y = collision.bounds[1]
            else:
                oriented = NestingEngine([definition], config)._oriented(
                    0,
                    float(data.get("rotation_deg", 0.0)),
                )
                actual = translate(
                    oriented.actual,
                    xoff=target_min_x - oriented.actual.bounds[0],
                    yoff=target_min_y - oriented.actual.bounds[1],
                )
                collision_min_x = target_min_x + float(data.get("collision_offset_x", 0.0))
                collision_min_y = target_min_y + float(data.get("collision_offset_y", 0.0))
                collision = translate(
                    oriented.collision,
                    xoff=collision_min_x - oriented.collision.bounds[0],
                    yoff=collision_min_y - oriented.collision.bounds[1],
                )
            instance = PartInstance(base_instance_index + clone_index, definition)
            placements.append(
                Placement(
                    sequence=len(self.canvas._sheet.placements) + clone_index,
                    instance=instance,
                    sheet_index=self.canvas._sheet.index,
                    x=collision_min_x,
                    y=collision_min_y,
                    angle_deg=float(data.get("rotation_deg", 0.0)),
                    actual=actual,
                    collision=collision,
                    mirrored=bool(data.get("mirrored", False)),
                )
            )
        logger.info(
            "Clone placements: source_key=%s target_sheet=%s count=%d",
            definition.key,
            self.canvas._sheet.name,
            count,
        )
        self.undo_stack.push(
            AddPlacementsCommand(
                self.canvas,
                self.canvas._sheet,
                placements,
                "\u6279\u91cf\u514b\u9686",
            )
        )
        placement = placements[-1]
        self._selected_placement = placement
        self._selection_context = "canvas"
        self.status_label.setText(f"\u5df2\u514b\u9686 {count} \u4e2a\u6a21\u578b")

    def clone_placement(self, placement: Placement) -> None:
        count, accepted = QInputDialog.getInt(
            self,
            "\u514b\u9686\u6a21\u578b",
            "\u514b\u9686\u6570\u91cf\uff1a",
            1,
            1,
            999,
            1,
        )
        if not accepted:
            return
        data = self._placement_data(placement)
        self._add_placement_clone(data, count=count)

    def rotate_selected_custom(self) -> None:
        if self._selected_placement is not None:
            self.rotate_placement_custom(self._selected_placement)

    def rotate_placement_custom(self, placement: Placement) -> None:
        degrees, accepted = QInputDialog.getDouble(
            self,
            "\u81ea\u5b9a\u4e49\u65cb\u8f6c",
            "\u65cb\u8f6c\u89d2\u5ea6\uff08\u6b63\u6570\u4e3a\u9006\u65f6\u9488\uff09\uff1a",
            0.0,
            -360.0,
            360.0,
            1,
        )
        if accepted and abs(degrees) > 1.0e-9:
            self.rotate_placement(placement, float(degrees))

    def rotate_selected_placement(self, degrees: float) -> None:
        if self._selected_placement is not None:
            self.rotate_placement(self._selected_placement, degrees)

    def rotate_placement(self, placement: Placement, degrees: float) -> None:
        logger.info("Rotate placement: %s by %.1f deg", placement.label, degrees)
        old_state = PlacementState(
            x=placement.x,
            y=placement.y,
            actual=placement.actual,
            collision=placement.collision,
            angle_deg=placement.angle_deg,
            mirrored=placement.mirrored,
        )
        pivot = placement.actual.centroid
        actual = rotate(placement.actual, degrees, origin=pivot, use_radians=False)
        collision = rotate(placement.collision, degrees, origin=pivot, use_radians=False)
        new_state = PlacementState(
            x=collision.bounds[0],
            y=collision.bounds[1],
            actual=actual,
            collision=collision,
            angle_deg=(placement.angle_deg + degrees) % 360.0,
            mirrored=placement.mirrored,
        )
        self.undo_stack.push(
            TransformPlacementCommand(
                self.canvas,
                placement,
                old_state,
                new_state,
                f"\u65cb\u8f6c {placement.label}",
            )
        )
        self.status_label.setText(f"\u5df2\u65cb\u8f6c {placement.label}")

    def flip_selected_placement(self, axis: str) -> None:
        if self._selected_placement is not None:
            self.flip_placement(self._selected_placement, axis)

    def flip_placement(self, placement: Placement, axis: str) -> None:
        if placement.instance.definition.face_up_locked:
            QMessageBox.warning(
                self,
                "\u6b63\u9762\u65b9\u5411\u9501\u5b9a",
                "\u8be5\u96f6\u4ef6\u5df2\u9501\u5b9a\u6b63\u9762\u671d\u4e0a\uff0c"
                "\u7ffb\u9762\u4f1a\u6539\u53d8\u6c89\u5b54/\u6316\u69fd\u7684\u671d\u5411\u3002\n"
                "\u5982\u9700\u5141\u8bb8\u7ffb\u9762\uff0c\u8bf7\u5148\u5728\u6a21\u578b\u53f3\u952e\u83dc\u5355\u89e3\u9664\u9501\u5b9a\u3002",
            )
            return
        logger.info("Flip placement: %s axis=%s", placement.label, axis)
        old_state = PlacementState(
            x=placement.x,
            y=placement.y,
            actual=placement.actual,
            collision=placement.collision,
            angle_deg=placement.angle_deg,
            mirrored=placement.mirrored,
        )
        pivot = placement.actual.centroid
        xfact = -1.0 if axis == "horizontal" else 1.0
        yfact = -1.0 if axis == "vertical" else 1.0
        actual = scale(
            placement.actual,
            xfact=xfact,
            yfact=yfact,
            origin=pivot,
        )
        collision = scale(
            placement.collision,
            xfact=xfact,
            yfact=yfact,
            origin=pivot,
        )
        new_state = PlacementState(
            x=collision.bounds[0],
            y=collision.bounds[1],
            actual=actual,
            collision=collision,
            angle_deg=placement.angle_deg,
            mirrored=not placement.mirrored,
        )
        self.undo_stack.push(
            TransformPlacementCommand(
                self.canvas,
                placement,
                old_state,
                new_state,
                f"\u7ffb\u9762 {placement.label}",
            )
        )
        self.status_label.setText(f"\u5df2\u7ffb\u9762 {placement.label}")

    def toggle_face_lock(self, placement: Placement) -> None:
        definition = placement.instance.definition
        definition.face_up_locked = not definition.face_up_locked
        for entry in self.part_panel.entries:
            if entry.definition is definition:
                entry.request.face_up_locked = definition.face_up_locked
                break
        lock_status = "\u5f00" if definition.face_up_locked else "\u5173"
        self.status_label.setText(
            f"{placement.label} \u6b63\u9762\u9501\u5b9a\uff1a{lock_status}"
        )
        self._update_actions()

    def delete_placement(self, placement: Placement) -> None:
        logger.info("Delete placement: %s", placement.label)
        sheet = self.canvas._sheet
        if sheet is None:
            return
        try:
            index = sheet.placements.index(placement)
        except ValueError:
            return
        self.undo_stack.push(DeletePlacementCommand(self.canvas, sheet, placement, index))
        self._selected_placement = None
        self.status_label.setText(f"\u5df2\u5220\u9664\u6a21\u578b {placement.label}")

    def _unique_part_name(self, base_name: str) -> str:
        existing = {entry.request.name for entry in self.part_panel.entries}
        if base_name not in existing:
            return base_name
        for index in range(2, 10000):
            candidate = f"{base_name}_copy{index}"
            if candidate not in existing:
                return candidate
        return f"{base_name}_copy"

    def clone_selected_parts(self) -> None:
        selected = self.part_panel.selected_entries()
        if not selected:
            return
        clones: list[PartEntry] = []
        for entry in selected:
            name = self._unique_part_name(entry.request.name)
            request = PartRequest(
                name=name,
                path=entry.request.path,
                polygons=entry.request.polygons,
                quantity=entry.request.quantity,
                rotations_deg=entry.request.rotations_deg,
                grain_locked=entry.request.grain_locked,
                priority=entry.request.priority,
                thickness_mm=entry.request.thickness_mm,
            )
            definition = None
            if entry.definition is not None:
                definition = PartDefinition(
                    key=name,
                    name=name,
                    geometry=entry.definition.geometry,
                    quantity=entry.definition.quantity,
                    rotations_deg=entry.definition.rotations_deg,
                    source=entry.definition.source,
                    grain_locked=entry.definition.grain_locked,
                    priority=entry.definition.priority,
                    thickness_mm=entry.definition.thickness_mm,
                )
                request.thickness_mm = definition.thickness_mm if not entry.is_step else None
            clones.append(PartEntry(request=request, definition=definition, error=entry.error))
        self.part_panel.set_entries(self.part_panel.entries + clones)
        self.status_label.setText(f"\u5df2\u514b\u9686 {len(clones)} \u79cd\u96f6\u4ef6")
        self._result = None
        self._update_actions()

    def copy_selected_parts(self) -> None:
        selected = self.part_panel.selected_entries()
        if not selected:
            return
        self._internal_clipboard = [entry.to_project_dict() for entry in selected]
        payload = {"parts": self._internal_clipboard}
        QApplication.clipboard().setText(json.dumps(payload, ensure_ascii=False, indent=2))
        self.status_label.setText(f"\u5df2\u590d\u5236 {len(selected)} \u79cd\u96f6\u4ef6")

    def paste_parts(self) -> None:
        mappings = list(self._internal_clipboard)
        text = QApplication.clipboard().text().strip()
        if text:
            try:
                data = json.loads(text)
                if isinstance(data, dict) and isinstance(data.get("parts"), list):
                    mappings = data["parts"]
                elif isinstance(data, list):
                    mappings = data
                elif isinstance(data, dict):
                    mappings = [data]
            except Exception:
                pass
        if not mappings:
            self.status_label.setText("\u526a\u8d34\u677f\u4e2d\u6ca1\u6709\u53ef\u7c98\u8d34\u7684\u96f6\u4ef6")
            return
        requests: list[PartRequest] = []
        for mapping in mappings:
            values = dict(mapping)
            values["name"] = self._unique_part_name(str(values.get("name", "part")))
            try:
                requests.append(
                    request_from_mapping(
                        values,
                        Path.cwd(),
                        default_quantity=1,
                        default_rotations=(0.0, 90.0, 180.0, 270.0),
                    )
                )
            except Exception as exc:
                self.status_label.setText(f"\u7c98\u8d34\u5931\u8d25\uff1a{exc}")
        if requests:
            self._load_requests(requests, append=True)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()]
        if not paths:
            return
        project_candidate = None
        for path in paths:
            if path.suffix.lower() == ".json":
                try:
                    data = json.loads(path.read_text(encoding="utf-8-sig"))
                    if isinstance(data, dict) and ("parts" in data or "thickness_groups" in data):
                        project_candidate = path
                        break
                except Exception:
                    continue
        if project_candidate is not None:
            self._open_project_path(project_candidate)
            event.acceptProposedAction()
            return
        try:
            requests = load_requests(
                paths,
                manifest=None,
                default_quantity=1,
                default_rotations=(0.0, 90.0, 180.0, 270.0),
            )
        except Exception as exc:
            QMessageBox.critical(self, "\u6253\u5f00\u5931\u8d25", str(exc))
            return
        if requests:
            self._load_requests(requests, append=True)
            event.acceptProposedAction()

    def clear_parts(self) -> None:
        if not self.part_panel.entries:
            return
        if QMessageBox.question(self, "清空零件", "确定移除当前全部零件吗？") != QMessageBox.StandardButton.Yes:
            return
        self.part_panel.set_entries([])
        self.sheet_tree.clear()
        self.canvas.clear_layout()
        self._result = None
        self._project_path = None
        self._clear_saved_session()
        self._update_actions()

    def remove_selected_parts(self) -> None:
        self.part_panel.remove_selected()
        self._result = None
        self.sheet_tree.clear()
        self.canvas.clear_layout()
        if not self.part_panel.entries:
            self._clear_saved_session()
        else:
            self._schedule_session_save()
        self._update_actions()

    def _prepare_definitions(self):
        if self.config_panel.default_thickness() is not None:
            self.part_panel.apply_default_thickness(self.config_panel.default_thickness())
        failed = [entry for entry in self.part_panel.entries if entry.definition is None or entry.error]
        if failed:
            QMessageBox.warning(
                self,
                "存在无效零件",
                "以下零件仍未成功解析：\n" + "\n".join(entry.display_name for entry in failed),
            )
            return None
        missing = [
            entry
            for entry in self.part_panel.entries
            if not entry.is_step and entry.thickness_mm is None
        ]
        if missing and not self.config_panel.allow_unknown_thickness.isChecked():
            QMessageBox.warning(
                self,
                "缺少厚度",
                "以下 DXF/SVG/坐标零件没有厚度，程序不会从名称推断：\n"
                + "\n".join(entry.display_name for entry in missing)
                + "\n\n请在零件表中填写厚度，或设置“2D 默认厚度”。",
            )
            return None
        definitions = []
        for entry in self.part_panel.entries:
            if entry.definition is None or entry.definition.quantity <= 0:
                continue
            entry.request.quantity = entry.definition.quantity
            definitions.append(entry.definition)
        return definitions

    def start_nesting(self) -> None:
        if self._nest_worker is not None and self._nest_worker.isRunning():
            return
        definitions = self._prepare_definitions()
        if not definitions:
            return
        try:
            config = self.config_panel.nesting_config()
            config.validate()
            settings = self.config_panel.optimizer_settings()
        except Exception as exc:
            QMessageBox.warning(self, "参数无效", str(exc))
            return
        if (
            self.config_panel.round_thickness.value() > 0.0
            and self.config_panel.round_thickness.value()
            > self.config_panel.thickness_tolerance.value()
        ):
            QMessageBox.warning(
                self,
                "厚度标签冲突",
                "厚度标签步长不能大于厚度分组容差，否则不同厚度组可能写入同一目录。",
            )
            return

        self._result = None
        self.sheet_tree.clear()
        self.canvas.clear_layout()
        self.progress.setRange(0, 0)
        self.progress.setValue(0)
        self.status_label.setText("正在自动套裁...")
        self._nest_worker = NestingWorker(
            definitions,
            config,
            settings,
            thickness_tolerance=self.config_panel.thickness_tolerance.value(),
            round_thickness=self.config_panel.round_thickness.value(),
        )
        self._nest_worker.progress.connect(self._on_nesting_progress)
        self._nest_worker.finished_successfully.connect(self._on_nesting_finished)
        self._nest_worker.cancelled.connect(self._on_nesting_cancelled)
        self._nest_worker.failed.connect(self._on_worker_failed)
        self._nest_worker.finished.connect(self._on_nesting_thread_finished)
        self._update_actions()
        self._nest_worker.start()

    def stop_nesting(self) -> None:
        if self._nest_worker is not None and self._nest_worker.isRunning():
            self.status_label.setText("正在停止...")
            self._nest_worker.cancel()

    def _on_nesting_progress(self, message: str, fraction: float) -> None:
        if self.progress.maximum() == 0:
            self.progress.setRange(0, 1000)
        self.progress.setValue(max(0, min(1000, int(fraction * 1000))))
        self.status_label.setText(message)

    def _on_nesting_finished(self, result: GroupedNestingResult) -> None:
        self._result = result
        self.undo_stack.clear()
        self._populate_sheet_tree()
        self.config_panel.update_result(result)
        self._schedule_session_save()
        self.progress.setRange(0, 1000)
        self.progress.setValue(1000)
        self.status_label.setText(
            f"完成：{result.placed_count}/{result.requested_count} 件，"
            f"{result.sheet_count} 张板，利用率 {result.utilization_percent:.3f}%"
        )
        self._update_actions()

    def _on_nesting_cancelled(self) -> None:
        self.status_label.setText("已取消排版")
        self.progress.setValue(0)

    def _on_nesting_thread_finished(self) -> None:
        self._nest_worker = None
        self._update_actions()

    def _populate_sheet_tree(self) -> None:
        self.sheet_tree.clear()
        if self._result is None:
            return
        first_sheet_item: QTreeWidgetItem | None = None
        for group_result in self._result.groups:
            group = group_result.group
            solution = group_result.solution
            group_item = QTreeWidgetItem(
                [
                    f"{group.folder_name}  |  {solution.placed_count}/{solution.requested_count} 件"
                    f"  |  {len(solution.sheets)} 板"
                ]
            )
            group_item.setData(0, Qt.ItemDataRole.UserRole, ("group", group_result, None))
            self.sheet_tree.addTopLevelItem(group_item)
            for sheet in solution.sheets:
                sheet_item = QTreeWidgetItem(
                    [
                        f"{sheet.name}  |  {len(sheet.placements)} 件  |  "
                        f"{sheet.utilization_percent:.2f}%"
                    ]
                )
                sheet_item.setData(0, Qt.ItemDataRole.UserRole, ("sheet", group_result, sheet))
                group_item.addChild(sheet_item)
                if first_sheet_item is None:
                    first_sheet_item = sheet_item
            group_item.setExpanded(True)
        if first_sheet_item is not None:
            self.sheet_tree.setCurrentItem(first_sheet_item)

    def _on_sheet_selected(self, current: QTreeWidgetItem | None, previous=None) -> None:
        if current is None or self._result is None:
            return
        data = current.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return
        _, group_result, sheet = data
        if sheet is None:
            if group_result.solution.sheets:
                sheet = group_result.solution.sheets[0]
            else:
                return
        self._selected_placement = None
        logger.debug("Switch sheet: %s", sheet.name)
        self._update_actions()
        self.canvas.show_sheet(
            sheet,
            self._result.config,
            f"{group_result.group.folder_name} / {sheet.name}",
        )
        self.sheet_title_label.setText(
            f"{group_result.group.folder_name} / {sheet.name}  |  "
            f"{len(sheet.placements)} 件  |  利用率 {sheet.utilization_percent:.3f}%"
        )
        self._apply_layers()

    def show_part_details(self, entry: PartEntry | None) -> None:
        if entry is not None:
            self._selection_context = "table"
        self._update_actions()
        if entry is None:
            self.sheet_title_label.setText("未选择零件")
            return
        thickness = "未指定" if entry.thickness_mm is None else f"{entry.thickness_mm:.4f} mm"
        self.sheet_title_label.setText(
            f"{entry.display_name} | {entry.source_type} | 厚度 {thickness} | 面积 {entry.area_mm2:.3f} mm²"
        )

    def show_placement_details(self, placement) -> None:
        if placement is None:
            return
        self._selection_context = "canvas"
        self._selected_placement = placement
        self._update_actions()
        bounds = placement.actual.bounds
        self.status_label.setText(
            f"{placement.label} | X={bounds[0]:.3f}, Y={bounds[1]:.3f} | "
            f"旋转={placement.angle_deg:g}° | 厚度={placement.instance.definition.thickness_mm or 0:g} mm | "
            f"孔内嵌套={'是' if placement.nested_in_hole else '否'}"
            " | \u955c\u50cf=" + ("\u662f" if placement.mirrored else "\u5426")
            + (f" | \u975e\u6cd5\uff1a{placement.manual_error}" if placement.manual_error else "")
        )

    def show_message(self, message: str) -> None:
        self.status_label.setText(message)

    def new_project(self) -> None:
        if self.part_panel.entries:
            result = QMessageBox.question(self, "新建项目", "当前项目未保存，是否继续？")
            if result != QMessageBox.StandardButton.Yes:
                return
        self.part_panel.set_entries([])
        self.sheet_tree.clear()
        self.canvas.clear_layout()
        self.config_panel.from_dict({})
        self._result = None
        self._project_path = None
        self._pending_layout = None
        self._clear_saved_session()
        self.undo_stack.clear()
        self._update_actions()

    def _serialize_layout(self) -> list[dict]:
        if self._result is None:
            return []
        records: list[dict] = []
        for group_result in self._result.groups:
            for sheet in group_result.solution.sheets:
                for placement in sheet.placements:
                    records.append(
                        {
                            "thickness_mm": group_result.group.value_mm,
                            "part_key": placement.instance.definition.key,
                            "sheet_index": sheet.index,
                            "instance_index": placement.instance.index,
                            "sequence": placement.sequence,
                            "x": placement.x,
                            "y": placement.y,
                            "rotation_deg": placement.angle_deg,
                            "actual_offset_x": placement.actual.bounds[0] - placement.x,
                            "actual_offset_y": placement.actual.bounds[1] - placement.y,
                            "mirrored": placement.mirrored,
                            "nested_in_hole": placement.nested_in_hole,
                            **(
                                {
                                    "actual_wkt": wkt.dumps(placement.actual),
                                    "collision_wkt": wkt.dumps(placement.collision),
                                }
                                if placement.mirrored
                                else {}
                            ),
                        }
                    )
        return records

    def _restore_layout(self, records: list[dict]) -> None:
        if not records:
            return
        definitions = [
            entry.definition
            for entry in self.part_panel.entries
            if entry.definition is not None
        ]
        if not definitions:
            return
        config = self.config_panel.nesting_config()
        config.validate()
        groups = group_definitions_by_thickness(
            definitions,
            tolerance=self.config_panel.thickness_tolerance.value(),
            round_to=self.config_panel.round_thickness.value(),
        )
        group_results: list[GroupNestingResult] = []
        for group in groups:
            engine = NestingEngine(group.definitions, config)
            selected_records = [
                record
                for record in records
                if (
                    (record.get("thickness_mm") is None and group.value_mm is None)
                    or (
                        record.get("thickness_mm") is not None
                        and group.value_mm is not None
                        and abs(float(record["thickness_mm"]) - float(group.value_mm)) <= 0.051
                    )
                )
            ]
            if not selected_records:
                continue
            sheets: dict[int, SheetLayout] = {}
            schedule: list[tuple[int, float]] = []
            for record in sorted(selected_records, key=lambda item: int(item.get("sequence", 0))):
                instance_index = int(record["instance_index"])
                part_key = record.get("part_key")
                definition = next(
                    (item for item in group.definitions if item.key == part_key),
                    None,
                )
                if definition is None:
                    if instance_index < 0 or instance_index >= len(engine.instances):
                        continue
                    definition = engine.instances[instance_index].definition
                    oriented = engine._oriented(instance_index, float(record["rotation_deg"]))
                    instance = engine.instances[instance_index]
                else:
                    orientation_engine = NestingEngine([definition], config)
                    oriented = orientation_engine._oriented(0, float(record["rotation_deg"]))
                    instance = PartInstance(instance_index, definition)
                angle = float(record["rotation_deg"])
                x = float(record["x"])
                y = float(record["y"])
                if record.get("actual_wkt") and record.get("collision_wkt"):
                    actual = wkt.loads(str(record["actual_wkt"]))
                    collision = wkt.loads(str(record["collision_wkt"]))
                else:
                    actual = translate(
                        oriented.actual,
                        xoff=x - oriented.actual.bounds[0] + float(record.get("actual_offset_x", 0.0)),
                        yoff=y - oriented.actual.bounds[1] + float(record.get("actual_offset_y", 0.0)),
                    )
                    collision = translate(
                        oriented.collision,
                        xoff=x - oriented.collision.bounds[0],
                        yoff=y - oriented.collision.bounds[1],
                    )
                sheet_index = int(record.get("sheet_index", 0))
                sheet = sheets.setdefault(
                    sheet_index,
                    SheetLayout(
                        index=sheet_index,
                        width=config.sheet_width,
                        height=config.sheet_height,
                        margin=config.effective_edge_margin,
                    ),
                )
                sheet.placements.append(
                    Placement(
                        sequence=int(record.get("sequence", len(sheet.placements))),
                        instance=instance,
                        sheet_index=sheet_index,
                        x=x,
                        y=y,
                        angle_deg=angle,
                        actual=actual,
                        collision=collision,
                        nested_in_hole=bool(record.get("nested_in_hole", False)),
                        mirrored=bool(record.get("mirrored", False)),
                    )
                )
                schedule.append((instance_index, angle))
            ordered_sheets = [sheets[index] for index in sorted(sheets)]
            for sheet in ordered_sheets:
                collisions = [placement.collision for placement in sheet.placements]
                sheet.obstacles = unary_union(collisions) if collisions else None
                for placement in sheet.placements:
                    placement.nested_in_hole = any(
                        hole.covers(placement.actual)
                        for other in sheet.placements
                        if other is not placement
                        for hole in polygon_interiors(other.actual)
                    )
                for sequence, placement in enumerate(sheet.placements):
                    placement.sequence = sequence
            solution = NestingSolution(
                sheets=ordered_sheets,
                unplaced=[],
                schedule=schedule,
                optimizer="restored",
                evaluations=0,
                config_summary={},
            )
            group_results.append(
                GroupNestingResult(
                    group=group,
                    solution=solution,
                    validation=validate_solution(solution, config),
                )
            )
        if group_results:
            self._result = GroupedNestingResult(
                groups=group_results,
                config=config,
                settings=self.config_panel.optimizer_settings(),
            )
            self._populate_sheet_tree()
            self.config_panel.update_result(self._result)

    def save_project_dialog(self) -> bool:
        default_path = str(self._project_path or Path.cwd() / "nesting_project.neststudio.json")
        path, _ = QFileDialog.getSaveFileName(
            self,
            "保存套裁项目",
            default_path,
            "NestingStudio 项目 (*.neststudio.json);;JSON (*.json)",
        )
        if not path:
            return False
        project = {
            "version": 1,
            "config": self.config_panel.to_dict(),
            "parts": [entry.to_project_dict() for entry in self.part_panel.entries],
            "layout": self._serialize_layout(),
        }
        try:
            Path(path).write_text(
                json.dumps(project, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            QMessageBox.critical(self, "保存失败", str(exc))
            return False
        self._project_path = Path(path)
        self.status_label.setText(f"项目已保存：{path}")
        return True

    def open_project_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "NestingStudio \u9879\u76ee (*.neststudio.json *.json);;\u6240\u6709\u6587\u4ef6 (*)",
            "",
            "NestingStudio \u9879\u76ee (*.neststudio.json *.json);;\u6240\u6709\u6587\u4ef6 (*)",
        )
        if path:
            self._open_project_path(Path(path))

    def _load_last_config(self) -> None:
        raw = self._settings.value("last_config", "")
        if not raw:
            return
        try:
            config = json.loads(str(raw))
            if isinstance(config, dict):
                self.config_panel.from_dict(config)
        except Exception:
            logger.exception("Failed to restore last configuration")

    def _schedule_session_save(self, *_) -> None:
        if not self._restoring_session:
            self._session_save_timer.start(1500)

    def _clear_saved_session(self) -> None:
        try:
            self._session_path.unlink(missing_ok=True)
        except OSError:
            logger.exception("Failed to remove last session file")
        self._settings.remove("last_session")

    def _save_last_session(self) -> None:
        if self._restoring_session:
            return
        started = time.monotonic()
        try:
            config = self.config_panel.to_dict()
            payload = {
                "version": 1,
                "config": config,
                "parts": [entry.to_project_dict() for entry in self.part_panel.entries],
                "layout": self._serialize_layout(),
            }
            self._settings.setValue(
                "last_config",
                json.dumps(config, ensure_ascii=False, separators=(",", ":")),
            )
            if payload["parts"] or payload["layout"]:
                session_json = json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                self._session_path.parent.mkdir(parents=True, exist_ok=True)
                self._session_path.write_text(session_json, encoding="utf-8")
                logger.info(
                    "Session saved: parts=%d bytes=%d elapsed_ms=%.1f",
                    len(payload["parts"]),
                    len(session_json.encode("utf-8")),
                    (time.monotonic() - started) * 1000.0,
                )
                self._settings.remove("last_session")
            self._settings.sync()
        except Exception:
            logger.exception("Failed to save last session")

    def _project_payload(
        self,
        data: dict,
        base_dir: Path,
    ) -> tuple[list[PartRequest], dict, list[dict] | None, bool]:
        if is_global_summary(data):
            requests, panel_config, layout = import_global_summary(data, base_dir)
            return requests, panel_config, layout, False
        requests: list[PartRequest] = []
        for mapping in data.get("parts", []):
            requests.append(
                request_from_mapping(
                    mapping,
                    base_dir,
                    default_quantity=1,
                    default_rotations=(0.0, 90.0, 180.0, 270.0),
                )
            )
        return requests, data.get("config", {}), data.get("layout") or None, True

    def _restore_last_session(self) -> None:
        raw = ""
        try:
            if self._session_path.exists():
                raw = self._session_path.read_text(encoding="utf-8")
            else:
                raw = str(self._settings.value("last_session", "") or "")
        except OSError:
            logger.exception("Failed to read last session file")
            return
        if not raw:
            return
        try:
            data = json.loads(raw)
            requests, panel_config, layout, _ = self._project_payload(data, Path.cwd())
            if not requests:
                return
            self._restoring_session = True
            self.config_panel.from_dict(panel_config)
            self._pending_layout = layout
            self._project_path = None
            self._load_requests(requests, append=False)
            self.status_label.setText("\u6b63\u5728\u6062\u590d\u4e0a\u6b21\u6392\u7248...")
        except Exception:
            self._restoring_session = False
            logger.exception("Failed to restore last session")

    def _open_project_path(self, path: Path) -> None:
        try:
            project_path = path.expanduser().resolve()
            data = json.loads(project_path.read_text(encoding="utf-8-sig"))
            requests, panel_config, layout, is_project = self._project_payload(
                data,
                project_path.parent,
            )
            self.config_panel.from_dict(panel_config)
            self._project_path = project_path if is_project else None
            self._pending_layout = layout
            self._load_requests(requests, append=False)
        except Exception as exc:
            QMessageBox.critical(self, "\u6253\u5f00\u5931\u8d25", str(exc))

    def export_results_dialog(self) -> None:
        if self._result is None:
            return
        folder = QFileDialog.getExistingDirectory(self, "选择 DXF 输出目录")
        if not folder:
            return
        output_dir = Path(folder).resolve()
        invalid_count = sum(
            1
            for group_result in self._result.groups
            for sheet in group_result.solution.sheets
            for placement in sheet.placements
            if placement.manual_error
        )
        if invalid_count:
            answer = QMessageBox.question(
                self,
                "\u5b58\u5728\u975e\u6cd5\u653e\u7f6e",
                f"\u5f53\u524d\u6709 {invalid_count} \u4e2a\u7ea2\u8272\u975e\u6cd5\u653e\u7f6e\uff0c"
                "\u662f\u5426\u4ecd\u7136\u7ee7\u7eed\u5bfc\u51fa\uff1f",
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            global_groups = []
            for group_result in self._result.groups:
                group_result.validation = validate_solution(
                    group_result.solution,
                    self._result.config,
                )
                group_dir = output_dir / group_result.group.folder_name
                group_dir.mkdir(parents=True, exist_ok=True)
                write_json_report(
                    group_result.solution,
                    self._result.config,
                    group_dir / "nesting_layout.json",
                )
                write_csv_report(
                    group_result.solution,
                    group_dir / "nesting_placements.csv",
                )
                export_sheet_dxf(
                    group_result.solution,
                    self._result.config,
                    group_dir,
                    prefix="nesting",
                )
                global_groups.append(
                    {
                        "folder": group_result.group.folder_name,
                        "thickness_mm": group_result.group.value_mm,
                        "solution": solution_to_dict(
                            group_result.solution,
                            self._result.config,
                        ),
                    }
                )
            global_report = {
                "summary": {
                    "requested_parts": self._result.requested_count,
                    "placed_parts": self._result.placed_count,
                    "unplaced_parts": self._result.unplaced_count,
                    "sheet_count": self._result.sheet_count,
                    "part_area_mm2": self._result.part_area,
                    "sheet_area_mm2": self._result.sheet_area,
                    "utilization_percent": self._result.utilization_percent,
                    "hole_nested_parts": self._result.hole_nested_count,
                },
                "config": self._result.config.__dict__,
                "thickness_groups": global_groups,
            }
            (output_dir / "nesting_global_summary.json").write_text(
                json.dumps(global_report, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:
            QMessageBox.critical(self, "导出失败", str(exc))
            return
        QMessageBox.information(self, "导出完成", f"结果已导出到：\n{output_dir}")
        self.status_label.setText(f"结果已导出：{output_dir}")

    def open_logs(self) -> None:
        path = get_log_path()
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))

    def show_about(self) -> None:
        QMessageBox.about(
            self,
            "\u5173\u4e8e NestingStudio",
            f"<b>NestingStudio {__version__}</b><br>"
            "\u677f\u6750\u96f6\u4ef6\u81ea\u52a8\u5957\u88c1\u4e0e\u5d4c\u5957\u6392\u7248\u5de5\u5177<br><br>"
            "\u5f3a\u5236\u6309\u51e0\u4f55\u539a\u5ea6\u5206\u7ec4\uff0c\u652f\u6301\u5b89\u5168\u95f4\u8ddd\u3001"
            "\u5f15\u7ebf\u907f\u8ba9\u3001\u5b54\u5185\u5d4c\u5957\u3001\u7eb9\u7406\u65b9\u5411\u548c\u70ed\u53d8\u5f62\u63a7\u5236\u3002",
        )

    def open_documentation(self) -> None:
        document = Path(__file__).resolve().parents[1] / "README.md"
        if document.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(document)))
        else:
            QMessageBox.information(self, "\u6587\u6863", f"\u672a\u627e\u5230\u6587\u6863\uff1a{document}")

    def closeEvent(self, event) -> None:
        if self._nest_worker is not None and self._nest_worker.isRunning():
            result = QMessageBox.question(self, "退出", "套裁仍在运行，是否停止并退出？")
            if result != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._nest_worker.cancel()
            self._nest_worker.wait(2000)
        self._save_last_session()
        event.accept()
