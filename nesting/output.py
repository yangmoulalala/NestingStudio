from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from typing import Any

import ezdxf
from shapely.affinity import translate
from shapely.geometry import Point, box
from shapely.geometry.base import BaseGeometry

from .geometry import iter_polygons
from .models import NestingConfig, NestingSolution, Placement, SheetLayout


def _setup_layers(document: ezdxf.document.Drawing) -> None:
    layers = [
        ("SHEET_BORDER", 7),
        ("USABLE_BOUNDARY", 8),
        ("PART_OUTER", 5),
        ("PART_HOLE", 3),
        ("PART_LABEL", 2),
        ("LEAD_IN", 1),
        ("NEST_ORIGIN", 9),
    ]
    for name, color in layers:
        if name not in document.layers:
            document.layers.add(name, color=color)


def _add_geometry(
    modelspace: Any,
    geometry: BaseGeometry,
    *,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
) -> None:
    for polygon in iter_polygons(geometry):
        exterior = [
            (float(x) + offset_x, float(y) + offset_y)
            for x, y, *_ in polygon.exterior.coords
        ]
        modelspace.add_lwpolyline(
            exterior,
            close=True,
            dxfattribs={"layer": "PART_OUTER"},
        )
        for interior in polygon.interiors:
            hole = [
                (float(x) + offset_x, float(y) + offset_y)
                for x, y, *_ in interior.coords
            ]
            modelspace.add_lwpolyline(
                hole,
                close=True,
                dxfattribs={"layer": "PART_HOLE"},
            )


def _lead_anchor(placement: Placement) -> tuple[tuple[float, float], tuple[float, float]]:
    polygons = list(iter_polygons(placement.actual))
    if not polygons:
        return (placement.actual.bounds[0], placement.actual.bounds[1]), (1.0, 0.0)
    polygon = max(polygons, key=lambda item: item.area)
    coordinates = [(float(x), float(y)) for x, y, *_ in polygon.exterior.coords]
    if len(coordinates) > 1 and coordinates[0] == coordinates[-1]:
        coordinates.pop()
    if not coordinates:
        return (polygon.bounds[0], polygon.bounds[1]), (1.0, 0.0)

    index = min(
        range(len(coordinates)),
        key=lambda item: (
            coordinates[item][0] + coordinates[item][1],
            coordinates[item][1],
            coordinates[item][0],
        ),
    )
    entry = coordinates[index]
    previous = coordinates[(index - 1) % len(coordinates)]
    following = coordinates[(index + 1) % len(coordinates)]
    tangent_x = following[0] - previous[0]
    tangent_y = following[1] - previous[1]
    tangent_length = math.hypot(tangent_x, tangent_y)
    if tangent_length <= 1.0e-9:
        return entry, (1.0, 0.0)

    normal_a = (tangent_y / tangent_length, -tangent_x / tangent_length)
    normal_b = (-normal_a[0], -normal_a[1])
    probe_distance = 0.05
    for normal in (normal_a, normal_b):
        probe_point = Point(
            entry[0] + normal[0] * probe_distance,
            entry[1] + normal[1] * probe_distance,
        )
        if not placement.actual.buffer(1.0e-6).contains(probe_point):
            return entry, normal
    return entry, normal_a


def _add_lead_in(modelspace: Any, placement: Placement, config: NestingConfig) -> None:
    if config.lead_length <= 0.0:
        return
    entry, direction = _lead_anchor(placement)
    start = (
        entry[0] + direction[0] * config.kerf / 2.0,
        entry[1] + direction[1] * config.kerf / 2.0,
    )
    end = (
        entry[0] + direction[0] * config.lead_length,
        entry[1] + direction[1] * config.lead_length,
    )
    modelspace.add_line(start, end, dxfattribs={"layer": "LEAD_IN"})


def _add_sheet(
    modelspace: Any,
    sheet: SheetLayout,
    config: NestingConfig,
    offset_x: float = 0.0,
    offset_y: float = 0.0,
) -> None:
    modelspace.add_lwpolyline(
        [
            (offset_x, offset_y),
            (offset_x + sheet.width, offset_y),
            (offset_x + sheet.width, offset_y + sheet.height),
            (offset_x, offset_y + sheet.height),
        ],
        close=True,
        dxfattribs={"layer": "SHEET_BORDER"},
    )
    margin = sheet.margin
    modelspace.add_lwpolyline(
        [
            (offset_x + margin, offset_y + margin),
            (offset_x + sheet.width - margin, offset_y + margin),
            (offset_x + sheet.width - margin, offset_y + sheet.height - margin),
            (offset_x + margin, offset_y + sheet.height - margin),
        ],
        close=True,
        dxfattribs={"layer": "USABLE_BOUNDARY"},
    )
    modelspace.add_text(
        sheet.name,
        height=12.0,
        dxfattribs={"layer": "PART_LABEL"},
    ).set_placement((offset_x + 5.0, offset_y + 5.0))

    for placement in sheet.placements:
        moved = translate(placement.actual, xoff=offset_x, yoff=offset_y)
        _add_geometry(modelspace, moved)
        centroid = moved.representative_point()
        modelspace.add_text(
            placement.label,
            height=5.0,
            dxfattribs={"layer": "PART_LABEL"},
        ).set_placement((float(centroid.x), float(centroid.y)))
        _add_lead_in(modelspace, placement, config)


def export_sheet_dxf(
    solution: NestingSolution,
    config: NestingConfig,
    output_dir: Path,
    prefix: str = "nesting",
) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []
    for sheet in solution.sheets:
        document = ezdxf.new("R2010", setup=True)
        document.units = ezdxf.units.MM
        _setup_layers(document)
        _add_sheet(document.modelspace(), sheet, config)
        output_path = output_dir / f"{prefix}_sheet_{sheet.index + 1:02d}.dxf"
        document.saveas(output_path)
        files.append(output_path)

    if solution.sheets:
        gap = 100.0
        document = ezdxf.new("R2010", setup=True)
        document.units = ezdxf.units.MM
        _setup_layers(document)
        for sheet in solution.sheets:
            offset_x = sheet.index * (config.sheet_width + gap)
            _add_sheet(document.modelspace(), sheet, config, offset_x=offset_x)
        combined = output_dir / f"{prefix}_all_sheets.dxf"
        document.saveas(combined)
        files.append(combined)
    return files


def validate_solution(solution: NestingSolution, config: NestingConfig) -> dict[str, Any]:
    minimum_pair_distance = math.inf
    minimum_edge_distance = math.inf
    overlap_count = 0

    for sheet in solution.sheets:
        sheet_box = box(0.0, 0.0, config.sheet_width, config.sheet_height)
        for placement in sheet.placements:
            edge_distance = float(placement.actual.distance(sheet_box.boundary))
            minimum_edge_distance = min(minimum_edge_distance, edge_distance)
        for left_index, left in enumerate(sheet.placements):
            for right in sheet.placements[left_index + 1 :]:
                try:
                    distance = float(left.actual.distance(right.actual))
                    minimum_pair_distance = min(minimum_pair_distance, distance)
                    if left.actual.intersection(right.actual).area > config.overlap_epsilon:
                        overlap_count += 1
                except Exception:
                    overlap_count += 1

    if math.isinf(minimum_pair_distance):
        minimum_pair_distance = 0.0
    if math.isinf(minimum_edge_distance):
        minimum_edge_distance = 0.0

    return {
        "minimum_pair_distance_mm": minimum_pair_distance,
        "minimum_edge_distance_mm": minimum_edge_distance,
        "required_part_clearance_mm": config.effective_clearance,
        "required_edge_margin_mm": config.effective_edge_margin,
        "manual_invalid_count": sum(
            1
            for sheet in solution.sheets
            for placement in sheet.placements
            if placement.manual_error
        ),
        "part_clearance_ok": minimum_pair_distance + config.overlap_epsilon
        >= config.effective_clearance,
        "edge_margin_ok": minimum_edge_distance + config.overlap_epsilon
        >= config.effective_edge_margin,
        "overlap_count": overlap_count,
    }


def solution_to_dict(solution: NestingSolution, config: NestingConfig) -> dict[str, Any]:
    validation = validate_solution(solution, config)
    return {
        "summary": {
            "requested_parts": solution.requested_count,
            "placed_parts": solution.placed_count,
            "unplaced_parts": len(solution.unplaced),
            "sheet_count": len(solution.sheets),
            "part_area_mm2": round(solution.part_area, 6),
            "sheet_area_mm2": round(solution.sheet_area, 6),
            "total_utilization_percent": round(solution.total_utilization_percent, 6),
            "hole_nested_parts": solution.hole_nested_count,
            "optimizer": solution.optimizer,
            "evaluations": solution.evaluations,
            **config.__dict__,
            "effective_clearance_mm": config.effective_clearance,
            "effective_edge_margin_mm": config.effective_edge_margin,
        },
        "validation": validation,
        "sheets": [
            {
                "sheet": sheet.name,
                "width_mm": sheet.width,
                "height_mm": sheet.height,
                "part_area_mm2": round(sheet.part_area, 6),
                "utilization_percent": round(sheet.utilization_percent, 6),
                "used_bounds_mm": [
                    round(float(value), 6) for value in (sheet.used_bounds or (0.0, 0.0, 0.0, 0.0))
                ],
                "placements": [
                    placement.to_dict(sheet.name) for placement in sheet.placements
                ],
            }
            for sheet in solution.sheets
        ],
        "unplaced": [
            {
                "part_key": instance.definition.key,
                "part_name": instance.definition.name,
                "instance_index": instance.index,
                "source": instance.definition.source,
            }
            for instance in solution.unplaced
        ],
    }


def write_json_report(
    solution: NestingSolution,
    config: NestingConfig,
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(solution_to_dict(solution, config), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_csv_report(
    solution: NestingSolution,
    output_path: Path,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "sheet",
        "part_name",
        "part_key",
        "instance_index",
        "placement_order",
        "x",
        "y",
        "anchor_x",
        "anchor_y",
        "rotation_deg",
        "width_mm",
        "height_mm",
        "area_mm2",
        "nested_in_hole",
        "manual_error",
        "mirrored",
        "thickness_mm",
        "source",
    ]
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for sheet in solution.sheets:
            for placement in sheet.placements:
                row = placement.to_dict(sheet.name)
                writer.writerow({name: row.get(name, "") for name in fieldnames})
