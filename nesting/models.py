from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from shapely.geometry.base import BaseGeometry


class NestingCancelled(RuntimeError):
    """Raised when the user cancels an optimization run."""


@dataclass
class PartDefinition:
    """A distinct part type, with all allowed orientations."""

    key: str
    name: str
    geometry: BaseGeometry
    quantity: int = 1
    rotations_deg: tuple[float, ...] = (0.0, 90.0, 180.0, 270.0)
    source: str = ""
    grain_locked: bool = False
    priority: int = 0
    thickness_mm: float | None = None
    face_up_locked: bool = True

    @property
    def area(self) -> float:
        return float(self.geometry.area)

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        return tuple(float(value) for value in self.geometry.bounds)


@dataclass(frozen=True)
class PartInstance:
    """One physical copy of a part."""

    index: int
    definition: PartDefinition

    @property
    def key(self) -> str:
        return self.definition.key


@dataclass
class OrientedShape:
    """A part normalized to the positive quadrant for one rotation."""

    instance: PartInstance
    angle_deg: float
    actual: BaseGeometry
    collision: BaseGeometry
    actual_bounds: tuple[float, float, float, float]
    collision_bounds: tuple[float, float, float, float]


@dataclass
class Placement:
    """One placed part on one sheet."""

    sequence: int
    instance: PartInstance
    sheet_index: int
    x: float
    y: float
    angle_deg: float
    actual: BaseGeometry
    collision: BaseGeometry
    holes: list[BaseGeometry] = field(default_factory=list)
    nested_in_hole: bool = False
    thermal_density: float = 0.0
    manual_error: str = ""
    mirrored: bool = False

    @property
    def label(self) -> str:
        return f"{self.instance.definition.key}#{self.instance.index + 1}"

    def to_dict(self, sheet_name: str) -> dict[str, Any]:
        min_x, min_y, max_x, max_y = self.actual.bounds
        return {
            "sheet": sheet_name,
            "part_name": self.instance.definition.name,
            "part_key": self.instance.definition.key,
            "instance_index": self.instance.index,
            "placement_order": self.sequence,
            "x": round(float(min_x), 6),
            "y": round(float(min_y), 6),
            "anchor_x": round(float(self.x), 6),
            "anchor_y": round(float(self.y), 6),
            "rotation_deg": round(float(self.angle_deg), 6),
            "width_mm": round(float(max_x - min_x), 6),
            "height_mm": round(float(max_y - min_y), 6),
            "area_mm2": round(float(self.actual.area), 6),
            "nested_in_hole": self.nested_in_hole,
            "thermal_density": round(float(self.thermal_density), 6),
            "manual_error": self.manual_error,
            "mirrored": self.mirrored,
            "source": self.instance.definition.source,
            "thickness_mm": self.instance.definition.thickness_mm,
            "face_up_locked": self.instance.definition.face_up_locked,
        }


@dataclass
class SheetLayout:
    """Placements and derived utilization for one physical sheet."""

    index: int
    width: float
    height: float
    margin: float
    placements: list[Placement] = field(default_factory=list)
    obstacles: BaseGeometry | None = None

    @property
    def name(self) -> str:
        return f"S{self.index + 1:02d}"

    @property
    def part_area(self) -> float:
        return sum(float(item.actual.area) for item in self.placements)

    @property
    def sheet_area(self) -> float:
        return float(self.width * self.height)

    @property
    def usable_area(self) -> float:
        usable_w = max(0.0, self.width - 2.0 * self.margin)
        usable_h = max(0.0, self.height - 2.0 * self.margin)
        return float(usable_w * usable_h)

    @property
    def utilization_percent(self) -> float:
        return 100.0 * self.part_area / self.sheet_area if self.sheet_area else 0.0

    @property
    def used_bounds(self) -> tuple[float, float, float, float] | None:
        if not self.placements:
            return None
        min_x = min(item.actual.bounds[0] for item in self.placements)
        min_y = min(item.actual.bounds[1] for item in self.placements)
        max_x = max(item.actual.bounds[2] for item in self.placements)
        max_y = max(item.actual.bounds[3] for item in self.placements)
        return float(min_x), float(min_y), float(max_x), float(max_y)

    @property
    def used_bbox_area(self) -> float:
        bounds = self.used_bounds
        if bounds is None:
            return 0.0
        return float((bounds[2] - bounds[0]) * (bounds[3] - bounds[1]))

    @property
    def hole_nested_count(self) -> int:
        return sum(1 for placement in self.placements if placement.nested_in_hole)


@dataclass
class NestingSolution:
    """Complete result for all requested parts."""

    sheets: list[SheetLayout]
    unplaced: list[PartInstance]
    schedule: list[tuple[int, float]]
    optimizer: str
    evaluations: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)
    config_summary: dict[str, Any] = field(default_factory=dict)

    @property
    def placed_count(self) -> int:
        return sum(len(sheet.placements) for sheet in self.sheets)

    @property
    def requested_count(self) -> int:
        return self.placed_count + len(self.unplaced)

    @property
    def part_area(self) -> float:
        return sum(sheet.part_area for sheet in self.sheets)

    @property
    def sheet_area(self) -> float:
        return sum(sheet.sheet_area for sheet in self.sheets)

    @property
    def total_utilization_percent(self) -> float:
        return 100.0 * self.part_area / self.sheet_area if self.sheet_area else 0.0

    @property
    def hole_nested_count(self) -> int:
        return sum(sheet.hole_nested_count for sheet in self.sheets)

    @property
    def cost(self) -> float:
        # Primary objective: use fewer sheets and place every requested part.
        unplaced_penalty = len(self.unplaced) * 1.0e12
        sheet_penalty = len(self.sheets) * 1.0e9
        compactness = sum(sheet.used_bbox_area for sheet in self.sheets)
        hole_bonus = self.hole_nested_count * 1.0e-3
        return unplaced_penalty + sheet_penalty + compactness - hole_bonus


@dataclass
class NestingConfig:
    sheet_width: float
    sheet_height: float
    margin: float = 10.0
    part_clearance: float = 5.0
    kerf: float = 0.0
    lead_length: float = 0.0
    flatten_tolerance: float = 0.25
    allow_hole_nesting: bool = True
    thermal_radius: float = 80.0
    thermal_density_limit: float = 0.90
    common_line_policy: str = "disabled"
    max_sheets: int = 0
    overlap_epsilon: float = 1.0e-5

    @property
    def effective_clearance(self) -> float:
        pierce_reserve = self.kerf + 2.0 * self.lead_length
        return max(self.part_clearance, pierce_reserve)

    @property
    def effective_edge_margin(self) -> float:
        return self.margin + self.kerf / 2.0 + self.lead_length

    def validate(self) -> None:
        if self.sheet_width <= 0.0 or self.sheet_height <= 0.0:
            raise ValueError("板材宽度和高度必须大于 0")
        if self.margin < 0.0 or self.part_clearance < 0.0:
            raise ValueError("边距和安全间距不能小于 0")
        if self.kerf < 0.0 or self.lead_length < 0.0:
            raise ValueError("刀缝宽度和引线长度不能小于 0")
        if self.flatten_tolerance <= 0.0:
            raise ValueError("轮廓离散容差必须大于 0")
        if not 0.0 < self.thermal_density_limit <= 1.0:
            raise ValueError("热密度上限必须在 (0, 1] 范围内")
        if self.effective_edge_margin * 2.0 >= min(self.sheet_width, self.sheet_height):
            raise ValueError("边距与引线预留过大，板材没有可用区域")
