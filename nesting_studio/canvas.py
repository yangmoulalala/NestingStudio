from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass

import numpy as np
from shiboken6 import isValid

from PySide6.QtCore import QLineF, QPointF, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QKeySequence,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
from PySide6.QtWidgets import (
    QGraphicsEllipseItem,
    QGraphicsItem,
    QGraphicsLineItem,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsTextItem,
    QGraphicsView,
    QMenu,
)

from shapely import STRtree
from shapely.affinity import translate
from shapely.geometry import Polygon, box
from shapely.ops import unary_union

from nesting.geometry import iter_polygons, polygon_interiors
from nesting.models import NestingConfig, Placement, SheetLayout


logger = logging.getLogger(__name__)


PART_COLORS = [
    "#3b82f6",
    "#10b981",
    "#f59e0b",
    "#a855f7",
    "#ec4899",
    "#06b6d4",
    "#84cc16",
    "#f97316",
    "#8b5cf6",
    "#14b8a6",
]


def _stable_color(key: str, nested: bool = False) -> QColor:
    if nested:
        return QColor("#f59e0b")
    value = sum(ord(character) * (index + 1) for index, character in enumerate(key))
    return QColor(PART_COLORS[value % len(PART_COLORS)])


@dataclass(frozen=True)
class SnapPoint:
    scene_x: float
    scene_y: float
    model_x: float
    model_y: float
    kind: str
    label: str
    radius: float | None = None
    center_model: tuple[float, float] | None = None

    @property
    def scene(self) -> QPointF:
        return QPointF(self.scene_x, self.scene_y)

    @property
    def model(self) -> tuple[float, float]:
        return self.model_x, self.model_y


def _fit_circle(points: list[tuple[float, float]]) -> tuple[tuple[float, float], float] | None:
    if len(points) < 6:
        return None
    array = np.asarray(points, dtype=float)
    if np.allclose(array[0], array[-1]):
        array = array[:-1]
    if len(array) < 6:
        return None
    x = array[:, 0]
    y = array[:, 1]
    matrix = np.column_stack((2.0 * x, 2.0 * y, np.ones_like(x)))
    target = x * x + y * y
    try:
        solution, *_ = np.linalg.lstsq(matrix, target, rcond=None)
    except np.linalg.LinAlgError:
        return None
    center_x, center_y, constant = solution
    radius_squared = constant + center_x * center_x + center_y * center_y
    if radius_squared <= 0.0:
        return None
    radius = math.sqrt(float(radius_squared))
    distances = np.hypot(x - center_x, y - center_y)
    residual = float(np.sqrt(np.mean((distances - radius) ** 2)))
    if radius <= 1.0e-9 or residual / radius > 0.035:
        return None
    return (float(center_x), float(center_y)), float(radius)


@dataclass
class PlacementState:
    x: float
    y: float
    actual: object
    collision: object
    angle_deg: float | None = None
    mirrored: bool | None = None


class LayoutCanvas(QGraphicsView):
    placementSelected = Signal(object)
    measurementChanged = Signal(str)
    placementMoveCommitted = Signal(object, object, object)
    clonePlacementRequested = Signal(object)
    copyPlacementRequested = Signal(object)
    deletePlacementRequested = Signal(object)
    pastePlacementRequested = Signal()
    rotatePlacementRequested = Signal(object, float)
    flipPlacementRequested = Signal(object, str)
    toggleFaceLockRequested = Signal(object)
    customRotatePlacementRequested = Signal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self.setRenderHints(
            QPainter.RenderHint.Antialiasing
            | QPainter.RenderHint.TextAntialiasing
            | QPainter.RenderHint.SmoothPixmapTransform
        )
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.ViewportAnchor.AnchorViewCenter)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.SmartViewportUpdate)
        self.setBackgroundBrush(QBrush(QColor("#18191b")))
        self.setDragMode(QGraphicsView.DragMode.NoDrag)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self._panning = False
        self._pan_start = QPointF()
        self._sheet_height = 0.0
        self._outer_items: list[QGraphicsItem] = []
        self._hole_items: list[QGraphicsItem] = []
        self._label_items: list[QGraphicsItem] = []
        self._lead_items: list[QGraphicsItem] = []
        self._margin_items: list[QGraphicsItem] = []
        self._sheet_items: list[QGraphicsItem] = []
        self._measure_mode = False
        self._measure_points: list[tuple[QPointF, tuple[float, float]]] = []
        self._measurement_items: list[QGraphicsItem] = []
        self._measurement_mode = "distance"
        self._snap_mode = "auto"
        self._snap_points: list[SnapPoint] = []
        self._snap_index: dict[tuple[int, int], list[int]] = {}
        self._snap_cell_size = 10.0
        self._collision_tree: STRtree | None = None
        self._collision_tree_items: list[Placement] = []
        self._last_drag_validation = 0.0
        self._last_drag_valid = True
        self._last_drag_message = ""
        self._last_edited_sheet: SheetLayout | None = None
        self._deferred_geometry_refresh = QTimer(self)
        self._deferred_geometry_refresh.setSingleShot(True)
        self._deferred_geometry_refresh.timeout.connect(self._refresh_after_edit)
        self._config: NestingConfig | None = None
        self._sheet: SheetLayout | None = None
        self._placement_items: dict[int, list[QGraphicsItem]] = {}
        self._grid_enabled = True
        self._grid_size = 1.0
        self._drag_placement: Placement | None = None
        self._drag_start_scene = QPointF()
        self._drag_start_state: PlacementState | None = None
        self._drag_items: list[QGraphicsItem] = []
        self._drag_start_positions: list[QPointF] = []
        self._drag_invalid_item: QGraphicsPathItem | None = None
        self._selected_placement: Placement | None = None
        self._config: NestingConfig | None = None
        self._sheet: SheetLayout | None = None
        self._placement_items: dict[int, list[QGraphicsItem]] = {}
        self._grid_enabled = True
        self._grid_size = 1.0
        self._drag_placement: Placement | None = None
        self._drag_start_scene = QPointF()
        self._drag_start_state: PlacementState | None = None
        self._drag_items: list[QGraphicsItem] = []
        self._drag_start_positions: list[QPointF] = []
        self._drag_invalid_item: QGraphicsPathItem | None = None

    def clear_layout(self) -> None:
        self._scene.clear()
        self._outer_items.clear()
        self._hole_items.clear()
        self._label_items.clear()
        self._lead_items.clear()
        self._margin_items.clear()
        self._sheet_items.clear()
        self._measure_points.clear()
        self._measurement_items.clear()
        self._snap_points.clear()
        self._snap_index.clear()
        self._placement_items.clear()
        self._placement_items.clear()

    def set_layers(
        self,
        *,
        show_sheet: bool = True,
        show_margin: bool = True,
        show_holes: bool = True,
        show_labels: bool = True,
        show_leads: bool = True,
    ) -> None:
        for item in self._sheet_items:
            item.setVisible(show_sheet)
        for item in self._margin_items:
            item.setVisible(show_margin)
        for item in self._hole_items:
            item.setVisible(show_holes)
        for item in self._label_items:
            item.setVisible(show_labels)
        for item in self._lead_items:
            item.setVisible(show_leads)

    def show_sheet(
        self,
        sheet: SheetLayout,
        config: NestingConfig,
        title: str,
    ) -> None:
        self.clear_layout()
        self._sheet_height = config.sheet_height
        self._config = config
        self._sheet = sheet
        self._config = config
        self._sheet = sheet
        self._scene.setSceneRect(
            QRectF(
                -0.08 * config.sheet_width,
                -0.10 * config.sheet_height,
                config.sheet_width * 1.16,
                config.sheet_height * 1.22,
            )
        )

        self._add_grid(config.sheet_width, config.sheet_height)
        sheet_path = self._rect_path(
            0.0, 0.0, config.sheet_width, config.sheet_height
        )
        sheet_item = QGraphicsPathItem(sheet_path)
        sheet_item.setPen(QPen(QColor("#d7dce2"), 2.0))
        sheet_item.setBrush(QBrush(QColor("#25272a")))
        sheet_item.setZValue(-20)
        self._scene.addItem(sheet_item)
        self._sheet_items.append(sheet_item)

        margin = config.effective_edge_margin
        margin_path = self._rect_path(
            margin,
            margin,
            config.sheet_width - margin,
            config.sheet_height - margin,
        )
        margin_pen = QPen(QColor("#75808d"), 1.2)
        margin_pen.setStyle(Qt.PenStyle.DashLine)
        margin_item = QGraphicsPathItem(margin_path)
        margin_item.setPen(margin_pen)
        margin_item.setBrush(Qt.BrushStyle.NoBrush)
        margin_item.setZValue(-10)
        self._scene.addItem(margin_item)
        self._margin_items.append(margin_item)

        title_item = QGraphicsTextItem(title)
        title_item.setDefaultTextColor(QColor("#f1f3f4"))
        font = QFont("Segoe UI", 13)
        font.setBold(True)
        title_item.setFont(font)
        title_item.setPos(8.0, -config.sheet_height * 0.085)
        title_item.setZValue(100)
        self._scene.addItem(title_item)
        self._label_items.append(title_item)

        for placement in sheet.placements:
            self._add_placement(placement, config)
        self._build_snap_index(sheet.placements)

        self.fit_layout()

    def _flip(self, x: float, y: float) -> QPointF:
        return QPointF(float(x), float(self._sheet_height - y))

    def _add_grid(self, width: float, height: float) -> None:
        grid_pen = QPen(QColor("#303338"), 0.7)
        for x in range(0, int(width) + 1, 100):
            line = QGraphicsLineItem(QLineF(self._flip(x, 0), self._flip(x, height)))
            line.setPen(grid_pen)
            line.setZValue(-30)
            self._scene.addItem(line)
        for y in range(0, int(height) + 1, 100):
            line = QGraphicsLineItem(QLineF(self._flip(0, y), self._flip(width, y)))
            line.setPen(grid_pen)
            line.setZValue(-30)
            self._scene.addItem(line)

    def _rect_path(self, x1: float, y1: float, x2: float, y2: float) -> QPainterPath:
        path = QPainterPath()
        path.moveTo(self._flip(x1, y1))
        path.lineTo(self._flip(x2, y1))
        path.lineTo(self._flip(x2, y2))
        path.lineTo(self._flip(x1, y2))
        path.closeSubpath()
        return path

    def _geometry_path(self, placement: Placement) -> QPainterPath:
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.OddEvenFill)
        for polygon in iter_polygons(placement.actual):
            exterior = QPolygonF(
                [self._flip(x, y) for x, y, *_ in polygon.exterior.coords]
            )
            path.addPolygon(exterior)
            for interior in polygon.interiors:
                hole = QPolygonF(
                    [self._flip(x, y) for x, y, *_ in interior.coords]
                )
                path.addPolygon(hole)
        return path

    def _hole_paths(self, placement: Placement) -> list[QPainterPath]:
        paths: list[QPainterPath] = []
        for polygon in iter_polygons(placement.actual):
            for interior in polygon.interiors:
                path = QPainterPath()
                path.addPolygon(
                    QPolygonF([self._flip(x, y) for x, y, *_ in interior.coords])
                )
                paths.append(path)
        return paths

    def _lead_points(self, placement: Placement, config: NestingConfig):
        if config.lead_length <= 0.0:
            return None
        polygons = list(iter_polygons(placement.actual))
        if not polygons:
            return None
        polygon = max(polygons, key=lambda item: item.area)
        points = [(float(x), float(y)) for x, y, *_ in polygon.exterior.coords]
        if len(points) < 2:
            return None
        entry = min(points, key=lambda item: (item[0] + item[1], item[1], item[0]))
        representative = placement.actual.representative_point()
        dx = entry[0] - representative.x
        dy = entry[1] - representative.y
        length = math.hypot(dx, dy)
        if length <= 1e-9:
            dx, dy, length = 1.0, 0.0, 1.0
        start = (entry[0], entry[1])
        end = (
            entry[0] + dx / length * config.lead_length,
            entry[1] + dy / length * config.lead_length,
        )
        return self._flip(*start), self._flip(*end)

    def _add_placement(self, placement: Placement, config: NestingConfig) -> None:
        placement_items: list[QGraphicsItem] = []
        path = self._geometry_path(placement)
        invalid = bool(placement.manual_error)
        color = QColor("#ef4444") if invalid else _stable_color(
            placement.instance.definition.key,
            placement.nested_in_hole,
        )
        item = QGraphicsPathItem(path)
        pen = QPen(color.lighter(130), 2.4 if invalid else 1.4)
        if invalid:
            pen.setStyle(Qt.PenStyle.DashLine)
        item.setPen(pen)
        fill = QColor(color)
        fill.setAlpha(165 if invalid else (125 if placement.nested_in_hole else 105))
        item.setBrush(QBrush(fill))
        item.setData(0, placement)
        item.setToolTip(
            f"{placement.label}\n"
            f"X={placement.actual.bounds[0]:.3f}, Y={placement.actual.bounds[1]:.3f}\n"
            f"旋转={placement.angle_deg:g}°\n"
            f"厚度={placement.instance.definition.thickness_mm or 0:g} mm\n"
            f"孔内嵌套={'是' if placement.nested_in_hole else '否'}"
        )
        item.setZValue(10)
        self._scene.addItem(item)
        self._outer_items.append(item)
        placement_items.append(item)

        for hole_path in self._hole_paths(placement):
            hole_item = QGraphicsPathItem(hole_path)
            hole_item.setPen(QPen(QColor("#e6e8eb"), 1.0))
            hole_item.setBrush(Qt.BrushStyle.NoBrush)
            hole_item.setZValue(12)
            hole_item.setData(0, placement)
            self._scene.addItem(hole_item)
            self._hole_items.append(hole_item)
            placement_items.append(hole_item)

        centroid = placement.actual.representative_point()
        label = QGraphicsTextItem(placement.label)
        label.setDefaultTextColor(QColor("#f8fafc"))
        label.setFont(QFont("Segoe UI", 6))
        label.setPos(self._flip(float(centroid.x), float(centroid.y)))
        label.setZValue(30)
        label.setData(0, placement)
        self._scene.addItem(label)
        self._label_items.append(label)
        placement_items.append(label)

        lead_points = self._lead_points(placement, config)
        if lead_points is not None:
            lead = QGraphicsLineItem(QLineF(lead_points[0], lead_points[1]))
            lead.setPen(QPen(QColor("#ef4444"), 1.3))
            lead.setZValue(25)
            lead.setData(0, placement)
            self._scene.addItem(lead)
            self._lead_items.append(lead)
            placement_items.append(lead)
        self._placement_items[id(placement)] = placement_items

    def remove_placement_items(self, placement: Placement) -> None:
        for item in self._placement_items.pop(id(placement), []):
            if item.scene() is not None:
                self._scene.removeItem(item)
        for collection in (
            self._outer_items,
            self._hole_items,
            self._label_items,
            self._lead_items,
        ):
            collection[:] = [item for item in collection if item.scene() is not None]

    def refresh_placement(self, placement: Placement, config: NestingConfig) -> None:
        self.remove_placement_items(placement)
        self._add_placement(placement, config)
        self._snap_points.clear()
        self._snap_index.clear()

    def update_sheet_obstacles(self) -> None:
        if self._sheet is None:
            return
        collisions = [placement.collision for placement in self._sheet.placements]
        self._sheet.obstacles = (
            unary_union(collisions) if collisions else None
        )
        self._refresh_collision_tree()

    def _refresh_collision_tree(self) -> None:
        if self._sheet is None:
            self._collision_tree = None
            self._collision_tree_items = []
            return
        self._collision_tree_items = list(self._sheet.placements)
        collisions = [placement.collision for placement in self._collision_tree_items]
        self._collision_tree = STRtree(collisions) if collisions else None

    def set_grid(self, enabled: bool, size: float) -> None:
        self._grid_enabled = enabled
        self._grid_size = max(0.01, float(size))

    def _snapped_model_delta(
        self,
        placement: Placement,
        raw_model_delta: tuple[float, float],
        disable_grid: bool,
    ) -> tuple[float, float]:
        dx, dy = raw_model_delta
        if self._grid_enabled and not disable_grid:
            min_x, min_y, _, _ = placement.actual.bounds
            target_x = round((min_x + dx) / self._grid_size) * self._grid_size
            target_y = round((min_y + dy) / self._grid_size) * self._grid_size
            dx = target_x - min_x
            dy = target_y - min_y
        return float(dx), float(dy)

    def _preview_state(
        self,
        placement: Placement,
        model_delta: tuple[float, float],
    ) -> PlacementState | None:
        if self._drag_start_state is None:
            return None
        dx, dy = model_delta
        return PlacementState(
            x=self._drag_start_state.x + dx,
            y=self._drag_start_state.y + dy,
            actual=translate(self._drag_start_state.actual, xoff=dx, yoff=dy),
            collision=translate(self._drag_start_state.collision, xoff=dx, yoff=dy),
        )

    def _validate_state(self, placement: Placement, state: PlacementState) -> tuple[bool, str]:
        if self._config is None or self._sheet is None:
            return False, "\u5f53\u524d\u677f\u6750\u4e0d\u53ef\u7f16\u8f91"
        margin = self._config.effective_edge_margin
        region = box(
            margin,
            margin,
            self._config.sheet_width - margin,
            self._config.sheet_height - margin,
        )
        if not region.covers(state.actual):
            return False, "\u8d85\u51fa\u6709\u6548\u677f\u6750\u8fb9\u754c"
        if self._collision_tree is not None:
            nearby_indexes = self._collision_tree.query(state.collision)
            nearby = [
                self._collision_tree_items[index]
                for index in nearby_indexes
                if index < len(self._collision_tree_items)
            ]
        else:
            nearby = self._sheet.placements
        for other in nearby:
            if other is placement:
                continue
            try:
                if state.collision.intersection(other.collision).area > self._config.overlap_epsilon:
                    return False, f"\u4e0e {other.label} \u5b89\u5168\u95f4\u8ddd\u4e0d\u8db3"
            except Exception:
                continue
        if self._config.thermal_density_limit < 1.0:
            center = state.actual.centroid
            radius = max(1.0, self._config.thermal_radius)
            window = box(
                center.x - radius,
                center.y - radius,
                center.x + radius,
                center.y + radius,
            )
            occupied = sum(
                float(window.intersection(other.actual).area)
                for other in self._sheet.placements
                if other is not placement
            )
            if window.area > 0.0 and occupied / window.area > self._config.thermal_density_limit:
                return False, "\u5c40\u90e8\u70ed\u5bc6\u5ea6\u8d85\u9650"
        return True, "\u4f4d\u7f6e\u6709\u6548"

    def _item_is_valid(self, item) -> bool:
        if item is None:
            return False
        try:
            return bool(isValid(item))
        except Exception:
            return False

    def _update_invalid_overlay(self, state: PlacementState | None) -> None:
        if state is None:
            return
        path = QPainterPath()
        path.setFillRule(Qt.FillRule.OddEvenFill)
        for polygon in iter_polygons(state.actual):
            path.addPolygon(
                QPolygonF([self._flip(x, y) for x, y, *_ in polygon.exterior.coords])
            )
        if not self._item_is_valid(self._drag_invalid_item):
            self._drag_invalid_item = QGraphicsPathItem()
            self._scene.addItem(self._drag_invalid_item)
        self._drag_invalid_item.setPen(
            QPen(QColor("#ef4444"), 2.0, Qt.PenStyle.DashLine)
        )
        self._drag_invalid_item.setBrush(Qt.BrushStyle.NoBrush)
        self._drag_invalid_item.setPath(path)
        self._drag_invalid_item.setZValue(180)
        self._drag_invalid_item.setVisible(True)

    def _hide_invalid_overlay(self) -> None:
        if not self._item_is_valid(self._drag_invalid_item):
            self._drag_invalid_item = None
            return
        try:
            self._drag_invalid_item.setVisible(False)
        except RuntimeError:
            self._drag_invalid_item = None

    def _begin_placement_drag(self, placement: Placement, viewport_position: QPointF) -> bool:
        logger.debug(
            "Start drag attempt: placement=%s sheet=%s viewport=(%.2f, %.2f)",
            placement.label,
            self._sheet.name if self._sheet is not None else None,
            viewport_position.x(),
            viewport_position.y(),
        )
        if self._config is None or self._sheet is None or self._measure_mode:
            return False
        self._drag_placement = placement
        self._drag_start_scene = viewport_position
        self._drag_start_state = PlacementState(
            x=placement.x,
            y=placement.y,
            actual=placement.actual,
            collision=placement.collision,
            angle_deg=placement.angle_deg,
            mirrored=placement.mirrored,
        )
        self._drag_items = list(self._placement_items.get(id(placement), []))
        self._drag_start_positions = [item.pos() for item in self._drag_items]
        self._last_drag_validation = 0.0
        self._last_drag_valid = True
        self._last_drag_message = ""
        logger.debug(
            "Drag state initialized: placement=%s item_count=%d start=(%.3f, %.3f)",
            placement.label,
            len(self._drag_items),
            placement.x,
            placement.y,
        )
        for item in self._drag_items:
            item.setOpacity(0.72)
        return bool(self._drag_items)

    def _update_placement_drag(self, event) -> None:
        if self._drag_placement is None or self._drag_start_state is None:
            return
        scene_delta = event.position() - self._drag_start_scene
        raw_model_delta = (scene_delta.x(), -scene_delta.y())
        model_delta = self._snapped_model_delta(
            self._drag_placement,
            raw_model_delta,
            bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier),
        )
        scene_delta = QPointF(model_delta[0], -model_delta[1])
        for item, start_position in zip(self._drag_items, self._drag_start_positions):
            item.setPos(start_position + scene_delta)
        state = self._preview_state(self._drag_placement, model_delta)
        now = time.monotonic()
        if now - self._last_drag_validation >= 0.06:
            valid, message = (
                self._validate_state(self._drag_placement, state)
                if state
                else (False, "")
            )
            self._last_drag_validation = now
            self._last_drag_valid = valid
            self._last_drag_message = message
        else:
            valid = self._last_drag_valid
            message = self._last_drag_message
        if valid:
            self._hide_invalid_overlay()
        else:
            self._update_invalid_overlay(state)
        placement_status = "\u53ef\u653e\u7f6e" if valid else "\u4e0d\u53ef\u653e\u7f6e"
        logger.debug(
            "Drag move: placement=%s valid=%s delta=(%.3f, %.3f) reason=%s",
            self._drag_placement.label,
            valid,
            model_delta[0],
            model_delta[1],
            message,
        )
        self.measurementChanged.emit(
            f"{placement_status}\uff1a{message}"
            f"  X={state.x if state else 0:.3f}, Y={state.y if state else 0:.3f}"
        )

    def _clear_drag_state(self) -> None:
        if self._drag_placement is not None:
            logger.debug("Release event received: placement=%s", self._drag_placement.label)
        self._drag_placement = None
        self._drag_start_state = None
        self._drag_items = []
        self._drag_start_positions = []

    def _finish_placement_drag(self, event) -> None:
        if self._drag_placement is None or self._drag_start_state is None:
            self._clear_drag_state()
            return
        started = time.monotonic()
        placement_label = self._drag_placement.label
        try:
            self._finish_placement_drag_impl(event)
        except Exception as exc:
            logger.exception("Drag handler exception, cancelled safely: placement=%s", self._drag_placement)
            self._hide_invalid_overlay()
            self.refresh_all_placements()
            self.measurementChanged.emit(f"\u79fb\u52a8\u5931\u8d25\u5df2\u53d6\u6d88\uff1a{exc}")
        finally:
            self._clear_drag_state()
            logger.info(
                "Drag release processing finished: placement=%s elapsed_ms=%.1f",
                placement_label,
                (time.monotonic() - started) * 1000.0,
            )

    def _finish_placement_drag_impl(self, event) -> None:
        placement = self._drag_placement
        if placement is None or self._drag_start_state is None:
            return
        scene_delta = event.position() - self._drag_start_scene
        if math.hypot(scene_delta.x(), scene_delta.y()) < 1.5:
            for item, start_position in zip(self._drag_items, self._drag_start_positions):
                item.setPos(start_position)
                item.setOpacity(1.0)
            self._hide_invalid_overlay()
            self._drag_placement = None
            self._drag_start_state = None
            self._drag_items = []
            self._drag_start_positions = []
            return
        raw_model_delta = (scene_delta.x(), -scene_delta.y())
        model_delta = self._snapped_model_delta(
            placement,
            raw_model_delta,
            bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier),
        )
        state = self._preview_state(placement, model_delta)
        valid, message = self._validate_state(placement, state) if state else (False, "")
        for item, start_position in zip(self._drag_items, self._drag_start_positions):
            item.setPos(start_position)
            item.setOpacity(1.0)
        self._hide_invalid_overlay()
        if state is not None:
            old_state = self._drag_start_state
            logger.info(
                "Drag completed: placement=%s old=(%.3f, %.3f) new=(%.3f, %.3f) valid=%s reason=%s",
                placement.label,
                old_state.x,
                old_state.y,
                state.x,
                state.y,
                valid,
                message,
            )
            self.apply_placement_state(placement, state)
            self.placementMoveCommitted.emit(placement, old_state, state)
            if valid:
                self.measurementChanged.emit(
                    f"\u5df2\u624b\u52a8\u79fb\u52a8 {placement.label}"
                    f"\uff1aX={state.x:.3f}, Y={state.y:.3f}"
                )
            else:
                self.measurementChanged.emit(
                    f"\u5df2\u653e\u7f6e\u4f46\u6807\u8bb0\u4e3a\u975e\u6cd5\uff1a{message}"
                )
        self._drag_placement = None
        self._drag_start_state = None
        self._drag_items = []
        self._drag_start_positions = []

    def add_manual_placement(
        self,
        sheet: SheetLayout,
        placement: Placement,
        index: int | None = None,
        refresh: bool = True,
    ) -> None:
        if placement in sheet.placements:
            return
        if index is None:
            sheet.placements.append(placement)
        else:
            sheet.placements.insert(index, placement)
        for sequence, item in enumerate(sheet.placements):
            item.sequence = sequence
        placement.sheet_index = sheet.index
        self._last_edited_sheet = sheet
        if sheet is self._sheet and refresh:
            self.refresh_all_placements()
            self._selected_placement = placement

    def remove_manual_placement(
        self,
        sheet: SheetLayout,
        placement: Placement,
        refresh: bool = True,
    ) -> int | None:
        try:
            index = sheet.placements.index(placement)
        except ValueError:
            return None
        self.remove_placement_items(placement)
        self._last_edited_sheet = sheet
        sheet.placements.pop(index)
        for sequence, item in enumerate(sheet.placements):
            item.sequence = sequence
        if self._selected_placement is placement:
            self._selected_placement = None
        if sheet is self._sheet and refresh:
            self.refresh_all_placements()
        return index

    def apply_placement_state(
        self,
        placement: Placement,
        state: PlacementState,
        sheet: SheetLayout | None = None,
    ) -> None:
        target_sheet = sheet or self._sheet
        self._last_edited_sheet = target_sheet
        placement.x = state.x
        placement.y = state.y
        placement.actual = state.actual
        placement.collision = state.collision
        if state.angle_deg is not None:
            placement.angle_deg = state.angle_deg
        if state.mirrored is not None:
            placement.mirrored = state.mirrored
        self.clear_measurements()
        if target_sheet is self._sheet:
            self._refresh_collision_tree()
            valid, message = self._validate_state(placement, state)
            placement.manual_error = "" if valid else message
            self.refresh_placement(placement, self._config)
            self._deferred_geometry_refresh.start(400)

    def _validate_all_placements(self) -> list[Placement]:
        if self._config is None or self._sheet is None:
            return []
        placements = self._sheet.placements
        previous = {id(placement): placement.manual_error for placement in placements}
        for placement in placements:
            placement.manual_error = ""
        margin = self._config.effective_edge_margin
        region = box(
            margin,
            margin,
            self._config.sheet_width - margin,
            self._config.sheet_height - margin,
        )
        for placement in placements:
            if not region.covers(placement.actual):
                placement.manual_error = "\u8d85\u51fa\u6709\u6548\u677f\u6750\u8fb9\u754c"
        tree = STRtree([placement.collision for placement in placements])
        for index, first in enumerate(placements):
            for other_index in tree.query(first.collision):
                if other_index <= index:
                    continue
                second = placements[other_index]
                try:
                    if first.collision.intersection(second.collision).area > self._config.overlap_epsilon:
                        message = f"\u4e0e {second.label} \u91cd\u53e0\u6216\u95f4\u8ddd\u4e0d\u8db3"
                        first.manual_error = "; ".join(filter(None, [first.manual_error, message]))
                        reverse = f"\u4e0e {first.label} \u91cd\u53e0\u6216\u95f4\u8ddd\u4e0d\u8db3"
                        second.manual_error = "; ".join(filter(None, [second.manual_error, reverse]))
                except Exception:
                    continue
        if self._config.thermal_density_limit < 1.0:
            radius = max(1.0, self._config.thermal_radius)
            for placement in placements:
                center = placement.actual.centroid
                window = box(
                    center.x - radius,
                    center.y - radius,
                    center.x + radius,
                    center.y + radius,
                )
                nearby_indexes = tree.query(window)
                occupied = sum(
                    float(window.intersection(placements[index].actual).area)
                    for index in nearby_indexes
                    if placements[index] is not placement
                )
                if window.area > 0.0 and occupied / window.area > self._config.thermal_density_limit:
                    placement.manual_error = "; ".join(
                        filter(
                            None,
                            [placement.manual_error, "\u5c40\u90e8\u70ed\u5bc6\u5ea6\u8d85\u9650"],
                        )
                    )
        return [
            placement
            for placement in placements
            if previous.get(id(placement), "") != placement.manual_error
        ]

    def refresh_all_placements(self) -> None:
        if self._config is None or self._sheet is None:
            return
        for items in self._placement_items.values():
            for item in items:
                if item.scene() is not None:
                    self._scene.removeItem(item)
        self._placement_items.clear()
        self._outer_items.clear()
        self._hole_items.clear()
        self._label_items.clear()
        self._lead_items.clear()
        self._validate_all_placements()
        for placement in self._sheet.placements:
            self._add_placement(placement, self._config)
        self.update_sheet_obstacles()
        self._build_snap_index(self._sheet.placements)

    def _refresh_after_edit(self) -> None:
        if self._config is None or self._sheet is None:
            return
        started = time.monotonic()
        previous_nested = {
            id(placement): placement.nested_in_hole
            for placement in self._sheet.placements
        }
        changed_errors = self._validate_all_placements()
        self._update_nested_flags(self._sheet)
        changed_nested = [
            placement
            for placement in self._sheet.placements
            if previous_nested.get(id(placement)) != placement.nested_in_hole
        ]
        affected = {id(item): item for item in [*changed_errors, *changed_nested]}
        for item in affected.values():
            self.refresh_placement(item, self._config)
        logger.info(
            "Deferred geometry refresh finished: sheet=%s changed=%d elapsed_ms=%.1f",
            self._sheet.name,
            len(affected),
            (time.monotonic() - started) * 1000.0,
        )

    def _update_nested_flag(
        self,
        placement: Placement,
        sheet: SheetLayout | None = None,
    ) -> None:
        target_sheet = sheet or self._sheet
        if target_sheet is None:
            return
        if self._collision_tree is not None and target_sheet is self._sheet:
            nearby_indexes = self._collision_tree.query(placement.actual)
            nearby = [
                self._collision_tree_items[index]
                for index in nearby_indexes
                if index < len(self._collision_tree_items)
            ]
        else:
            nearby = target_sheet.placements
        placement.nested_in_hole = any(
            hole.covers(placement.actual)
            for other in nearby
            if other is not placement
            for hole in polygon_interiors(other.actual)
        )

    def _update_nested_flags(self, sheet: SheetLayout | None = None) -> None:
        target_sheet = sheet or self._sheet
        if target_sheet is None:
            return
        for placement in target_sheet.placements:
            if self._collision_tree is not None and target_sheet is self._sheet:
                nearby_indexes = self._collision_tree.query(placement.actual)
                nearby = [
                    self._collision_tree_items[index]
                    for index in nearby_indexes
                    if index < len(self._collision_tree_items)
                ]
            else:
                nearby = target_sheet.placements
            placement.nested_in_hole = any(
                hole.covers(placement.actual)
                for other in nearby
                if other is not placement
                for hole in polygon_interiors(other.actual)
            )

    def _show_placement_context_menu(self, placement: Placement, global_position) -> None:
        menu = QMenu(self)
        clone_action = menu.addAction("\u514b\u9686")
        copy_action = menu.addAction("\u590d\u5236")
        paste_action = menu.addAction("\u7c98\u8d34")
        menu.addSeparator()
        rotate_cw = menu.addAction("\u987a\u65f6\u9488\u65cb\u8f6c 90\u00b0")
        rotate_ccw = menu.addAction("\u9006\u65f6\u9488\u65cb\u8f6c 90\u00b0")
        rotate_180 = menu.addAction("\u65cb\u8f6c 180\u00b0")
        rotate_custom = menu.addAction("\u81ea\u5b9a\u4e49\u89d2\u5ea6...")
        flip_h = menu.addAction("\u6c34\u5e73\u7ffb\u9762")
        flip_v = menu.addAction("\u5782\u76f4\u7ffb\u9762")
        lock_text = "\u9501\u5b9a\u6b63\u9762" if not placement.instance.definition.face_up_locked else "\u5141\u8bb8\u7ffb\u9762"
        face_lock = menu.addAction(lock_text)
        menu.addSeparator()
        delete_action = menu.addAction("\u5220\u9664")
        selected = menu.exec(global_position.toPoint())
        if selected == clone_action:
            self.clonePlacementRequested.emit(placement)
        elif selected == copy_action:
            self.copyPlacementRequested.emit(placement)
        elif selected == paste_action:
            self.pastePlacementRequested.emit()
        elif selected == rotate_cw:
            self.rotatePlacementRequested.emit(placement, -90.0)
        elif selected == rotate_ccw:
            self.rotatePlacementRequested.emit(placement, 90.0)
        elif selected == rotate_180:
            self.rotatePlacementRequested.emit(placement, 180.0)
        elif selected == rotate_custom:
            self.customRotatePlacementRequested.emit(placement)
        elif selected == flip_h:
            self.flipPlacementRequested.emit(placement, "horizontal")
        elif selected == flip_v:
            self.flipPlacementRequested.emit(placement, "vertical")
        elif selected == face_lock:
            self.toggleFaceLockRequested.emit(placement)
        elif selected == delete_action:
            self.deletePlacementRequested.emit(placement)

    def keyPressEvent(self, event) -> None:
        if self._selected_placement is None:
            super().keyPressEvent(event)
            return
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copyPlacementRequested.emit(self._selected_placement)
            event.accept()
            return
        if event.matches(QKeySequence.StandardKey.Paste):
            self.pastePlacementRequested.emit()
            event.accept()
            return
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier and event.key() == Qt.Key.Key_D:
            self.clonePlacementRequested.emit(self._selected_placement)
            event.accept()
            return
        if event.key() == Qt.Key.Key_R:
            degrees = 90.0 if event.modifiers() & Qt.KeyboardModifier.ShiftModifier else -90.0
            self.rotatePlacementRequested.emit(self._selected_placement, degrees)
            event.accept()
            return
        if event.key() == Qt.Key.Key_H:
            self.flipPlacementRequested.emit(self._selected_placement, "horizontal")
            event.accept()
            return
        if event.key() == Qt.Key.Key_V:
            self.flipPlacementRequested.emit(self._selected_placement, "vertical")
            event.accept()
            return
        if event.key() == Qt.Key.Key_Delete:
            self.deletePlacementRequested.emit(self._selected_placement)
            event.accept()
            return
        super().keyPressEvent(event)

    def set_measure_mode(self, enabled: bool) -> None:
        self._measure_mode = enabled
        self._measure_points.clear()
        self.setCursor(Qt.CursorShape.CrossCursor if enabled else Qt.CursorShape.ArrowCursor)

    def clear_measurements(self) -> None:
        for item in self._measurement_items:
            if item.scene() is not None:
                self._scene.removeItem(item)
        self._measurement_items.clear()
        self._measure_points.clear()
        self.measurementChanged.emit("")

    def _build_snap_index(self, placements: list[Placement]) -> None:
        self._snap_points.clear()
        self._snap_index.clear()

        for placement in placements:
            for polygon in iter_polygons(placement.actual):
                rings = [(polygon.exterior, False)]
                rings.extend((interior, True) for interior in polygon.interiors)
                for ring, is_hole in rings:
                    raw_points = [(float(x), float(y)) for x, y, *_ in ring.coords]
                    if len(raw_points) > 1 and raw_points[0] == raw_points[-1]:
                        raw_points.pop()
                    if len(raw_points) < 2:
                        continue

                    circle = _fit_circle(raw_points)
                    if circle is not None:
                        center, radius = circle
                        center_label = "\u5706\u5b54\u4e2d\u5fc3" if is_hole else "\u5706\u5f62\u8f6e\u5ed3\u5706\u5fc3"
                        self._append_snap(
                            center[0],
                            center[1],
                            "circle_center",
                            center_label,
                            radius=radius,
                            center_model=center,
                        )
                        step = max(1, len(raw_points) // 24)
                        for index in range(0, len(raw_points), step):
                            x, y = raw_points[index]
                            self._append_snap(
                                x,
                                y,
                                "circle_edge",
                                "\u5706\u5f27",
                                radius=radius,
                                center_model=center,
                            )
                        continue

                    for x, y in raw_points:
                        self._append_snap(x, y, "endpoint", "\u7aef\u70b9")
                    for index, start_point in enumerate(raw_points):
                        end_point = raw_points[(index + 1) % len(raw_points)]
                        midpoint = (
                            (start_point[0] + end_point[0]) / 2.0,
                            (start_point[1] + end_point[1]) / 2.0,
                        )
                        self._append_snap(
                            midpoint[0],
                            midpoint[1],
                            "midpoint",
                            "\u4e2d\u70b9",
                        )
                    try:
                        ring_center = Polygon(raw_points).centroid
                        self._append_snap(
                            float(ring_center.x),
                            float(ring_center.y),
                            "center",
                            "\u5b54\u4e2d\u5fc3" if is_hole else "\u8f6e\u5ed3\u4e2d\u5fc3",
                        )
                    except Exception:
                        pass

                center = polygon.centroid
                self._append_snap(
                    float(center.x),
                    float(center.y),
                    "center",
                    "\u96f6\u4ef6\u4e2d\u5fc3",
                )

        for index, point in enumerate(self._snap_points):
            cell = (
                int(math.floor(point.model_x / self._snap_cell_size)),
                int(math.floor(point.model_y / self._snap_cell_size)),
            )
            self._snap_index.setdefault(cell, []).append(index)

    def _append_snap(
        self,
        model_x: float,
        model_y: float,
        kind: str,
        label: str,
        radius: float | None = None,
        center_model: tuple[float, float] | None = None,
    ) -> None:
        scene = self._flip(model_x, model_y)
        self._snap_points.append(
            SnapPoint(
                scene_x=float(scene.x()),
                scene_y=float(scene.y()),
                model_x=float(model_x),
                model_y=float(model_y),
                kind=kind,
                label=label,
                radius=radius,
                center_model=center_model,
            )
        )

    def _snap_mode_matches(self, point: SnapPoint) -> bool:
        mode = self._snap_mode
        if mode == "auto":
            return True
        if mode == "endpoint":
            return point.kind == "endpoint"
        if mode == "midpoint":
            return point.kind == "midpoint"
        if mode == "center":
            return point.kind in {"center", "circle_center"}
        if mode == "circle":
            return point.kind in {"circle_center", "circle_edge"}
        return True

    def _nearest_snap_point(
        self,
        scene_point: QPointF,
        viewport_point: QPointF,
    ) -> SnapPoint:
        model_x = float(scene_point.x())
        model_y = float(self._sheet_height - scene_point.y())
        fallback = SnapPoint(
            scene_x=float(scene_point.x()),
            scene_y=float(scene_point.y()),
            model_x=model_x,
            model_y=model_y,
            kind="free",
            label="\u5750\u6807",
        )
        if not self._snap_index and self._sheet is not None:
            self._build_snap_index(self._sheet.placements)
        if not self._snap_index:
            return fallback

        scale = max(abs(float(self.transform().m11())), 1.0e-9)
        model_tolerance = 14.0 / scale
        cell_range = max(1, int(math.ceil(model_tolerance / self._snap_cell_size)))
        center_cell = (
            int(math.floor(model_x / self._snap_cell_size)),
            int(math.floor(model_y / self._snap_cell_size)),
        )
        candidates: set[int] = set()
        for dx in range(-cell_range, cell_range + 1):
            for dy in range(-cell_range, cell_range + 1):
                candidates.update(
                    self._snap_index.get((center_cell[0] + dx, center_cell[1] + dy), ())
                )

        best: SnapPoint | None = None
        best_distance = 14.0
        priority = {
            "circle_center": 0.6,
            "circle_edge": 0.0,
            "midpoint": 0.0,
            "endpoint": 0.0,
            "center": 0.4,
            "free": 1.0,
        }
        for index in candidates:
            point = self._snap_points[index]
            if not self._snap_mode_matches(point):
                continue
            view_point = self.mapFromScene(point.scene)
            distance = math.hypot(
                view_point.x() - viewport_point.x(),
                view_point.y() - viewport_point.y(),
            )
            distance += priority.get(point.kind, 0.0)
            if distance < best_distance:
                best_distance = distance
                best = point
        return best or fallback

    def _add_marker(self, point: QPointF, color: str = "#22d3ee") -> None:
        marker = QGraphicsEllipseItem(
            point.x() - 2.5,
            point.y() - 2.5,
            5.0,
            5.0,
        )
        marker.setPen(QPen(QColor(color), 1.0))
        marker.setBrush(QBrush(QColor(color)))
        marker.setZValue(200)
        self._scene.addItem(marker)
        self._measurement_items.append(marker)

    def _add_radius_measurement(self, snap: SnapPoint) -> None:
        if snap.radius is None or snap.center_model is None:
            self.measurementChanged.emit("\u8bf7\u70b9\u51fb\u5706\u5b54\u6216\u5706\u5f62\u8f6e\u5ed3")
            return
        center_model = snap.center_model
        edge_model = (center_model[0] + snap.radius, center_model[1])
        center_scene = self._flip(*center_model)
        edge_scene = self._flip(*edge_model)
        self._add_marker(center_scene)
        line = QGraphicsLineItem(QLineF(center_scene, edge_scene))
        line.setPen(QPen(QColor("#22d3ee"), 1.4, Qt.PenStyle.DashLine))
        line.setZValue(199)
        self._scene.addItem(line)
        self._measurement_items.append(line)
        label = QGraphicsTextItem(
            f"R={snap.radius:.3f} mm\nD={snap.radius * 2.0:.3f} mm"
        )
        label.setDefaultTextColor(QColor("#67e8f9"))
        label.setFont(QFont("Segoe UI", 8))
        label.setPos((center_scene.x() + edge_scene.x()) / 2.0, center_scene.y())
        label.setZValue(201)
        self._scene.addItem(label)
        self._measurement_items.append(label)
        self.measurementChanged.emit(
            f"{snap.label}\uff1a\u534a\u5f84 R={snap.radius:.3f} mm\uff0c"
            f"\u76f4\u5f84 D={snap.radius * 2.0:.3f} mm"
        )

    def _add_measurement_point(self, scene_point: QPointF, viewport_point: QPointF) -> None:
        snap = self._nearest_snap_point(scene_point, viewport_point)
        if self._measurement_mode == "radius":
            self._add_radius_measurement(snap)
            return

        snapped_scene = snap.scene
        model_point = snap.model
        self._add_marker(snapped_scene)
        self._measure_points.append((snapped_scene, model_point))

        if len(self._measure_points) < 2:
            self.measurementChanged.emit(
                f"\u5df2\u6355\u6349{snap.label}\uff1aX={model_point[0]:.3f}, Y={model_point[1]:.3f}"
            )
            return

        first_scene, first_model = self._measure_points[0]
        second_scene, second_model = self._measure_points[1]
        distance = math.dist(first_model, second_model)
        dx = second_model[0] - first_model[0]
        dy = second_model[1] - first_model[1]

        line = QGraphicsLineItem(QLineF(first_scene, second_scene))
        line.setPen(QPen(QColor("#22d3ee"), 1.4, Qt.PenStyle.DashLine))
        line.setZValue(199)
        self._scene.addItem(line)
        self._measurement_items.append(line)

        label = QGraphicsTextItem(f"{distance:.3f} mm")
        label.setDefaultTextColor(QColor("#67e8f9"))
        label.setFont(QFont("Segoe UI", 8))
        label.setPos(
            (first_scene.x() + second_scene.x()) / 2.0,
            (first_scene.y() + second_scene.y()) / 2.0,
        )
        label.setZValue(201)
        self._scene.addItem(label)
        self._measurement_items.append(label)
        self.measurementChanged.emit(
            f"\u6d4b\u91cf\u8ddd\u79bb {distance:.3f} mm"
            f"\uff08\u0394X={dx:.3f}, \u0394Y={dy:.3f}\uff09"
        )
        self._measure_points.clear()

    def set_measurement_mode(self, mode: str) -> None:
        self._measurement_mode = mode
        self._measure_points.clear()
        label = "\u534a\u5f84" if mode == "radius" else "\u8ddd\u79bb"
        self.measurementChanged.emit(f"\u6d4b\u91cf\u6a21\u5f0f\uff1a{label}")

    def set_snap_mode(self, mode: str) -> None:
        self._snap_mode = mode
        labels = {
            "auto": "\u81ea\u52a8",
            "endpoint": "\u7aef\u70b9",
            "midpoint": "\u4e2d\u70b9",
            "center": "\u4e2d\u5fc3",
            "circle": "\u5706\u5fc3/\u5706\u5f27",
        }
        self.measurementChanged.emit(f"\u6355\u6349\u6a21\u5f0f\uff1a{labels.get(mode, mode)}")

    def wheelEvent(self, event) -> None:
        factor = 1.18 if event.angleDelta().y() > 0 else 1.0 / 1.18
        self.scale(factor, factor)

    def mousePressEvent(self, event) -> None:
        if self._measure_mode and event.button() == Qt.MouseButton.LeftButton:
            self._add_measurement_point(
                self.mapToScene(event.position().toPoint()),
                event.position(),
            )
            event.accept()
            return
        if self._measure_mode and event.button() == Qt.MouseButton.RightButton:
            self._measure_points.clear()
            self.measurementChanged.emit("\u5df2\u53d6\u6d88\u5f53\u524d\u6d4b\u91cf")
            event.accept()
            return
        if event.button() == Qt.MouseButton.MiddleButton:
            logger.debug("Middle-button pan started: sheet=%s", self._sheet.name if self._sheet else None)
            self._panning = True
            self._pan_start = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        item = self.itemAt(event.position().toPoint())
        if item is not None:
            placement = item.data(0)
            if isinstance(placement, Placement):
                self._selected_placement = placement
                self.placementSelected.emit(placement)
                self._scene.clearSelection()
                item.setSelected(True)
                if event.button() == Qt.MouseButton.RightButton:
                    self._show_placement_context_menu(placement, event.globalPosition())
                    event.accept()
                    return
                if event.button() == Qt.MouseButton.LeftButton and self._begin_placement_drag(
                    placement, event.position()
                ):
                    self.setCursor(Qt.CursorShape.SizeAllCursor)
                    event.accept()
                    return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_placement is not None:
            self._update_placement_drag(event)
            event.accept()
            return
        if self._measure_mode:
            scene_point = self.mapToScene(event.position().toPoint())
            snap = self._nearest_snap_point(scene_point, event.position())
            self.measurementChanged.emit(
                f"\u6355\u6349 {snap.label}  X={snap.model_x:.3f}, Y={snap.model_y:.3f}"
            )
        if self._panning:
            delta = event.position() - self._pan_start
            self._pan_start = event.position()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - int(delta.x())
            )
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - int(delta.y())
            )
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._drag_placement is not None:
            logger.debug("Mouse release while dragging: placement=%s", self._drag_placement.label)
            self._finish_placement_drag(event)
            self.setCursor(
                Qt.CursorShape.CrossCursor if self._measure_mode else Qt.CursorShape.ArrowCursor
            )
            event.accept()
            return
        if event.button() == Qt.MouseButton.MiddleButton:
            logger.debug("Middle-button pan started: sheet=%s", self._sheet.name if self._sheet else None)
            self._panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def fit_layout(self) -> None:
        bounds = self.scene().itemsBoundingRect()
        if bounds.isValid():
            self.fitInView(bounds.adjusted(-10, -10, 10, 10), Qt.AspectRatioMode.KeepAspectRatio)

    def zoom_in(self) -> None:
        self.scale(1.2, 1.2)

    def zoom_out(self) -> None:
        self.scale(1.0 / 1.2, 1.0 / 1.2)
