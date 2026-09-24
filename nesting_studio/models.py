from __future__ import annotations

from dataclasses import dataclass

from nesting.inputs import PartRequest, parse_rotations
from nesting.models import PartDefinition


@dataclass
class PartEntry:
    request: PartRequest
    definition: PartDefinition | None = None
    error: str = ""

    @property
    def is_step(self) -> bool:
        return bool(
            self.request.path
            and self.request.path.suffix.lower() in {".step", ".stp"}
        )

    @property
    def display_name(self) -> str:
        return self.definition.name if self.definition else self.request.name

    @property
    def source_text(self) -> str:
        return str(self.request.path or "polygon-list")

    @property
    def source_type(self) -> str:
        if self.request.path is None:
            return "Coordinates"
        suffix = self.request.path.suffix.lower()
        return {
            ".step": "STEP",
            ".stp": "STEP",
            ".dxf": "DXF",
            ".svg": "SVG",
            ".json": "JSON",
        }.get(suffix, suffix.upper())

    @property
    def thickness_mm(self) -> float | None:
        if self.definition is not None:
            return self.definition.thickness_mm
        return self.request.thickness_mm

    @property
    def area_mm2(self) -> float:
        if self.definition is not None:
            return float(self.definition.geometry.area)
        return 0.0

    @property
    def rotations_text(self) -> str:
        return ",".join(
            f"{value:g}" for value in self.request.rotations_deg
        )

    def set_quantity(self, quantity: int) -> None:
        self.request.quantity = max(1, int(quantity))
        if self.definition is not None:
            self.definition.quantity = self.request.quantity

    def set_rotations_text(self, value: str) -> None:
        rotations = parse_rotations(value)
        self.request.rotations_deg = rotations
        if self.definition is not None:
            self.definition.rotations_deg = rotations

    def set_thickness(self, value: float | None) -> None:
        self.request.thickness_mm = value
        if self.definition is not None and not self.is_step:
            self.definition.thickness_mm = value

    def to_project_dict(self) -> dict:
        result = {
            "name": self.request.name,
            "quantity": self.request.quantity,
            "rotations": list(self.request.rotations_deg),
            "grain_locked": self.request.grain_locked,
            "priority": self.request.priority,
            "face_up_locked": (
                self.definition.face_up_locked
                if self.definition is not None
                else self.request.face_up_locked
            ),
            "thickness_mm": (
                self.definition.thickness_mm
                if self.definition is not None and self.is_step
                else self.request.thickness_mm
            ),
        }
        if self.request.path is not None:
            result["path"] = str(self.request.path)
        if self.request.polygons is not None:
            result["polygons"] = self.request.polygons
        return result
