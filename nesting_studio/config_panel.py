from __future__ import annotations

import json

from PySide6.QtCore import QSettings, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QInputDialog,
    QMessageBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QToolBox,
    QVBoxLayout,
    QWidget,
)

from nesting.models import NestingConfig
from nesting.optimize import OptimizerSettings


MATERIAL_PRESETS = {
    "自定义": None,
    "2440 × 1220 mm": (2440.0, 1220.0),
    "3000 × 1500 mm": (3000.0, 1500.0),
    "1220 × 2440 mm": (1220.0, 2440.0),
    "2500 × 1250 mm": (2500.0, 1250.0),
}


def _double_spin(
    minimum: float,
    maximum: float,
    value: float,
    *,
    decimals: int = 2,
    step: float = 1.0,
    suffix: str = " mm",
) -> QDoubleSpinBox:
    widget = QDoubleSpinBox()
    widget.setRange(minimum, maximum)
    widget.setDecimals(decimals)
    widget.setSingleStep(step)
    widget.setValue(value)
    widget.setSuffix(suffix)
    return widget


class ConfigPanel(QWidget):
    changed = Signal()
    runRequested = Signal()
    cancelRequested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._running = False
        self._settings = QSettings("NestingStudio", "NestingStudio")
        self._custom_presets: dict[str, dict] = self._load_custom_presets()
        self._build_ui()
        self._connect_signals()
        self._update_material_area()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        root.addWidget(scroll, 1)

        content = QWidget()
        scroll.setWidget(content)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(8, 8, 8, 8)

        toolbox = QToolBox()
        content_layout.addWidget(toolbox)

        material_page = QWidget()
        material_form = QFormLayout(material_page)
        self.material_preset = QComboBox()
        self.material_preset.addItems(MATERIAL_PRESETS.keys())
        self.material_preset.addItems(self._custom_presets.keys())
        preset_buttons = QHBoxLayout()
        self.save_preset_button = QPushButton("\u4fdd\u5b58\u539f\u6599\u9884\u8bbe")
        self.delete_preset_button = QPushButton("\u5220\u9664\u9884\u8bbe")
        preset_buttons.addWidget(self.save_preset_button)
        preset_buttons.addWidget(self.delete_preset_button)
        self.sheet_width = _double_spin(10.0, 100000.0, 2440.0, step=10.0)
        self.sheet_height = _double_spin(10.0, 100000.0, 1220.0, step=10.0)
        self.material_area_label = QLabel("2.977 m²")
        self.margin = _double_spin(0.0, 1000.0, 10.0, step=1.0)
        self.clearance = _double_spin(0.0, 1000.0, 5.0, step=1.0)
        self.kerf = _double_spin(0.0, 100.0, 0.0, decimals=3, step=0.1)
        self.lead_length = _double_spin(0.0, 1000.0, 0.0, step=1.0)
        material_form.addRow("板材原料预设", self.material_preset)
        material_form.addRow("", preset_buttons)
        material_form.addRow("板材宽度 W", self.sheet_width)
        material_form.addRow("板材高度 H", self.sheet_height)
        material_form.addRow("原料面积", self.material_area_label)
        material_form.addRow("板材边距", self.margin)
        material_form.addRow("零件间隙", self.clearance)
        material_form.addRow("刀缝宽度", self.kerf)
        material_form.addRow("引线长度", self.lead_length)
        toolbox.addItem(material_page, "板材与间距")

        thickness_page = QWidget()
        thickness_form = QFormLayout(thickness_page)
        self.default_2d_thickness = _double_spin(
            0.0,
            200.0,
            0.0,
            decimals=3,
            step=0.1,
            suffix=" mm",
        )
        self.default_2d_thickness.setSpecialValueText("未指定")
        self.thickness_tolerance = _double_spin(
            0.0,
            10.0,
            0.05,
            decimals=3,
            step=0.01,
        )
        self.round_thickness = _double_spin(
            0.0,
            10.0,
            0.05,
            decimals=3,
            step=0.01,
        )
        self.allow_unknown_thickness = QCheckBox("允许未指定厚度单独成组")
        thickness_form.addRow("2D 默认厚度", self.default_2d_thickness)
        thickness_form.addRow("厚度分组容差", self.thickness_tolerance)
        thickness_form.addRow("厚度标签步长", self.round_thickness)
        thickness_form.addRow("", self.allow_unknown_thickness)
        toolbox.addItem(thickness_page, "厚度分组")

        optimization_page = QWidget()
        optimization_form = QFormLayout(optimization_page)
        self.optimizer = QComboBox()
        self.optimizer.addItem("Greedy（快速）", "greedy")
        self.optimizer.addItem("Simulated Annealing（SA）", "sa")
        self.optimizer.addItem("Genetic Algorithm（GA）", "ga")
        self.iterations = QSpinBox()
        self.iterations.setRange(1, 100000)
        self.iterations.setValue(80)
        self.population = QSpinBox()
        self.population.setRange(4, 1000)
        self.population.setValue(12)
        self.generations = QSpinBox()
        self.generations.setRange(1, 10000)
        self.generations.setValue(20)
        self.seed = QSpinBox()
        self.seed.setRange(0, 2_147_483_647)
        self.seed.setValue(2026)
        self.time_limit = _double_spin(1.0, 86400.0, 60.0, decimals=1, step=10.0, suffix=" s")
        self.max_sheets = QSpinBox()
        self.max_sheets.setRange(0, 100000)
        self.max_sheets.setSpecialValueText("不限")
        optimization_form.addRow("优化器", self.optimizer)
        optimization_form.addRow("SA 迭代次数", self.iterations)
        optimization_form.addRow("GA 种群规模", self.population)
        optimization_form.addRow("GA 代数", self.generations)
        optimization_form.addRow("随机种子", self.seed)
        optimization_form.addRow("每组时间上限", self.time_limit)
        optimization_form.addRow("每组最大板材数", self.max_sheets)
        toolbox.addItem(optimization_page, "排版优化")

        process_page = QWidget()
        process_form = QFormLayout(process_page)
        self.hole_nesting = QCheckBox("允许零件嵌套到大孔中")
        self.hole_nesting.setChecked(True)
        self.thermal_radius = _double_spin(1.0, 10000.0, 80.0, step=10.0)
        self.thermal_density = _double_spin(
            0.01,
            1.0,
            0.90,
            decimals=2,
            step=0.05,
            suffix="",
        )
        self.flatten_tolerance = _double_spin(
            0.01,
            10.0,
            0.5,
            decimals=3,
            step=0.05,
        )
        self.common_line = QComboBox()
        self.common_line.addItem("关闭（保留普通间距）", "disabled")
        process_form.addRow("内孔二次嵌套", self.hole_nesting)
        process_form.addRow("热影响半径", self.thermal_radius)
        process_form.addRow("热密度上限", self.thermal_density)
        process_form.addRow("曲线离散容差", self.flatten_tolerance)
        process_form.addRow("共边切割", self.common_line)
        toolbox.addItem(process_page, "工艺约束")

        consent = QLabel(
            "说明：STEP 厚度始终由几何自动计算；DXF/SVG/坐标零件必须在表格中填写厚度。自动套裁后可关闭测量模式，直接拖动零件修正位置。"
        )
        consent.setWordWrap(True)
        consent.setStyleSheet("color:#aeb4bb; padding:6px;")
        content_layout.addWidget(consent)

        self.result_box = QGroupBox("\u6392\u7248\u7ed3\u679c")
        result_form = QFormLayout(self.result_box)
        self.result_sheets = QLabel("-")
        self.result_parts = QLabel("-")
        self.result_utilization = QLabel("-")
        self.result_clearance = QLabel("-")
        self.result_holes = QLabel("-")
        self.result_invalid = QLabel("-")
        result_form.addRow("\u677f\u6750\u6570\u91cf", self.result_sheets)
        result_form.addRow("\u5df2\u653e\u7f6e\u96f6\u4ef6", self.result_parts)
        result_form.addRow("\u603b\u5229\u7528\u7387", self.result_utilization)
        result_form.addRow("\u6700\u5c0f\u95f4\u8ddd", self.result_clearance)
        result_form.addRow("\u5b54\u5185\u5d4c\u5957", self.result_holes)
        result_form.addRow("\u975e\u6cd5\u653e\u7f6e", self.result_invalid)
        content_layout.addWidget(self.result_box)

        buttons = QHBoxLayout()
        self.run_button = QPushButton("开始自动套裁")
        self.run_button.setObjectName("primary")
        self.cancel_button = QPushButton("停止")
        self.cancel_button.setEnabled(False)
        buttons.addWidget(self.run_button, 1)
        buttons.addWidget(self.cancel_button)
        root.addLayout(buttons)

    def _connect_signals(self) -> None:
        self.material_preset.currentTextChanged.connect(self._on_preset_changed)
        self.sheet_width.valueChanged.connect(self._update_material_area)
        self.sheet_height.valueChanged.connect(self._update_material_area)
        for widget in (
            self.sheet_width,
            self.sheet_height,
            self.margin,
            self.clearance,
            self.kerf,
            self.lead_length,
            self.default_2d_thickness,
            self.thickness_tolerance,
            self.round_thickness,
            self.thermal_radius,
            self.thermal_density,
            self.flatten_tolerance,
        ):
            widget.valueChanged.connect(lambda *_: self.changed.emit())
        self.allow_unknown_thickness.toggled.connect(lambda *_: self.changed.emit())
        self.hole_nesting.toggled.connect(lambda *_: self.changed.emit())
        self.optimizer.currentIndexChanged.connect(lambda *_: self.changed.emit())
        self.iterations.valueChanged.connect(lambda *_: self.changed.emit())
        self.population.valueChanged.connect(lambda *_: self.changed.emit())
        self.generations.valueChanged.connect(lambda *_: self.changed.emit())
        self.seed.valueChanged.connect(lambda *_: self.changed.emit())
        self.time_limit.valueChanged.connect(lambda *_: self.changed.emit())
        self.max_sheets.valueChanged.connect(lambda *_: self.changed.emit())
        self.save_preset_button.clicked.connect(self._save_material_preset)
        self.delete_preset_button.clicked.connect(self._delete_material_preset)
        self.run_button.clicked.connect(self.runRequested.emit)
        self.cancel_button.clicked.connect(self.cancelRequested.emit)

    def _load_custom_presets(self) -> dict[str, dict]:
        raw = self._settings.value("material_presets", "{}")
        if isinstance(raw, dict):
            return {str(key): dict(value) for key, value in raw.items()}
        try:
            data = json.loads(str(raw or "{}"))
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        return {
            str(key): dict(value)
            for key, value in data.items()
            if isinstance(value, dict)
        }

    def _persist_custom_presets(self) -> None:
        self._settings.setValue(
            "material_presets",
            json.dumps(self._custom_presets, ensure_ascii=False),
        )
        self._settings.sync()

    def _save_material_preset(self) -> None:
        name, accepted = QInputDialog.getText(
            self,
            "\u4fdd\u5b58\u539f\u6599\u9884\u8bbe",
            "\u9884\u8bbe\u540d\u79f0\uff1a",
        )
        name = name.strip()
        if not accepted or not name:
            return
        if name in MATERIAL_PRESETS:
            QMessageBox.warning(self, "\u540d\u79f0\u51b2\u7a81", "\u8be5\u540d\u79f0\u5df2\u88ab\u5185\u7f6e\u9884\u8bbe\u5360\u7528\u3002")
            return
        values = self.to_dict()
        values.pop("material_preset", None)
        self._custom_presets[name] = values
        self._persist_custom_presets()
        if self.material_preset.findText(name) < 0:
            self.material_preset.addItem(name)
        self.material_preset.setCurrentText(name)
        self.status_message = f"\u539f\u6599\u9884\u8bbe\u5df2\u4fdd\u5b58\uff1a{name}"

    def _delete_material_preset(self) -> None:
        name = self.material_preset.currentText()
        if name not in self._custom_presets:
            QMessageBox.information(self, "\u5220\u9664\u9884\u8bbe", "\u5f53\u524d\u4e0d\u662f\u81ea\u5b9a\u4e49\u9884\u8bbe\u3002")
            return
        answer = QMessageBox.question(
            self,
            "\u5220\u9664\u9884\u8bbe",
            f"\u786e\u5b9a\u5220\u9664\u539f\u6599\u9884\u8bbe\u201c{name}\u201d\u5417\uff1f",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._custom_presets.pop(name, None)
        self._persist_custom_presets()
        index = self.material_preset.findText(name)
        if index >= 0:
            self.material_preset.removeItem(index)
        self.material_preset.setCurrentText("\u81ea\u5b9a\u4e49")

    def _on_preset_changed(self, name: str) -> None:
        preset = MATERIAL_PRESETS.get(name)
        if preset is not None:
            self.sheet_width.setValue(preset[0])
            self.sheet_height.setValue(preset[1])
            self._update_material_area()
            self.changed.emit()
            return
        custom = self._custom_presets.get(name)
        if custom:
            self.from_dict(custom)
            self.changed.emit()

    def _update_material_area(self) -> None:
        area_mm2 = self.sheet_width.value() * self.sheet_height.value()
        self.material_area_label.setText(f"{area_mm2 / 1_000_000.0:.3f} m²")

    def set_running(self, running: bool) -> None:
        self._running = running
        self.run_button.setEnabled(not running)
        self.cancel_button.setEnabled(running)

    def nesting_config(self) -> NestingConfig:
        return NestingConfig(
            sheet_width=self.sheet_width.value(),
            sheet_height=self.sheet_height.value(),
            margin=self.margin.value(),
            part_clearance=self.clearance.value(),
            kerf=self.kerf.value(),
            lead_length=self.lead_length.value(),
            flatten_tolerance=self.flatten_tolerance.value(),
            allow_hole_nesting=self.hole_nesting.isChecked(),
            thermal_radius=self.thermal_radius.value(),
            thermal_density_limit=self.thermal_density.value(),
            common_line_policy=str(self.common_line.currentData()),
            max_sheets=self.max_sheets.value(),
        )

    def optimizer_settings(self) -> OptimizerSettings:
        return OptimizerSettings(
            optimizer=str(self.optimizer.currentData()),
            iterations=self.iterations.value(),
            population_size=self.population.value(),
            generations=self.generations.value(),
            seed=self.seed.value(),
            time_limit_seconds=self.time_limit.value(),
        )

    def default_thickness(self) -> float | None:
        value = self.default_2d_thickness.value()
        return None if value <= 0.0 else float(value)

    def update_result(self, result) -> None:
        self.result_sheets.setText(str(result.sheet_count))
        self.result_parts.setText(
            f"{result.placed_count}/{result.requested_count}"
        )
        self.result_utilization.setText(f"{result.utilization_percent:.3f}%")
        minimum = min(
            (
                item.validation.get("minimum_pair_distance_mm", 0.0)
                for item in result.groups
                if item.solution.placed_count > 1
            ),
            default=0.0,
        )
        self.result_clearance.setText(f"{minimum:.3f} mm")
        self.result_holes.setText(str(result.hole_nested_count))
        invalid_count = sum(
            1
            for group_result in result.groups
            for sheet in group_result.solution.sheets
            for placement in sheet.placements
            if placement.manual_error
        )
        self.result_invalid.setText(str(invalid_count))

    def to_dict(self) -> dict:
        return {
            "material_preset": self.material_preset.currentText(),
            "sheet_width": self.sheet_width.value(),
            "sheet_height": self.sheet_height.value(),
            "margin": self.margin.value(),
            "clearance": self.clearance.value(),
            "kerf": self.kerf.value(),
            "lead_length": self.lead_length.value(),
            "default_2d_thickness": self.default_2d_thickness.value(),
            "thickness_tolerance": self.thickness_tolerance.value(),
            "round_thickness": self.round_thickness.value(),
            "allow_unknown_thickness": self.allow_unknown_thickness.isChecked(),
            "optimizer": self.optimizer.currentData(),
            "iterations": self.iterations.value(),
            "population": self.population.value(),
            "generations": self.generations.value(),
            "seed": self.seed.value(),
            "time_limit": self.time_limit.value(),
            "max_sheets": self.max_sheets.value(),
            "hole_nesting": self.hole_nesting.isChecked(),
            "thermal_radius": self.thermal_radius.value(),
            "thermal_density": self.thermal_density.value(),
            "flatten_tolerance": self.flatten_tolerance.value(),
            "common_line": self.common_line.currentData(),
        }

    def from_dict(self, data: dict) -> None:
        if "material_preset" in data:
            index = self.material_preset.findText(str(data["material_preset"]))
            if index >= 0:
                self.material_preset.setCurrentIndex(index)
        mapping = {
            "sheet_width": self.sheet_width,
            "sheet_height": self.sheet_height,
            "margin": self.margin,
            "clearance": self.clearance,
            "kerf": self.kerf,
            "lead_length": self.lead_length,
            "default_2d_thickness": self.default_2d_thickness,
            "thickness_tolerance": self.thickness_tolerance,
            "round_thickness": self.round_thickness,
            "iterations": self.iterations,
            "population": self.population,
            "generations": self.generations,
            "seed": self.seed,
            "time_limit": self.time_limit,
            "max_sheets": self.max_sheets,
            "thermal_radius": self.thermal_radius,
            "thermal_density": self.thermal_density,
            "flatten_tolerance": self.flatten_tolerance,
        }
        for key, widget in mapping.items():
            if key in data:
                widget.setValue(data[key])
        if "allow_unknown_thickness" in data:
            self.allow_unknown_thickness.setChecked(bool(data["allow_unknown_thickness"]))
        if "hole_nesting" in data:
            self.hole_nesting.setChecked(bool(data["hole_nesting"]))
        if "optimizer" in data:
            index = self.optimizer.findData(data["optimizer"])
            if index >= 0:
                self.optimizer.setCurrentIndex(index)
        if "common_line" in data:
            index = self.common_line.findData(data["common_line"])
            if index >= 0:
                self.common_line.setCurrentIndex(index)
