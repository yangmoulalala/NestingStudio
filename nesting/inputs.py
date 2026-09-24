from __future__ import annotations

import json
import math
from dataclasses import dataclass
from statistics import median
from pathlib import Path
from typing import Any, Iterable, Sequence

import ezdxf
import svgelements as se
from ezdxf.path import make_path
from shapely.geometry.base import BaseGeometry

from .geometry import geometry_from_rings, stitch_point_sequences
from .models import PartDefinition


SUPPORTED_2D_SUFFIXES = {".dxf", ".svg"}
SUPPORTED_STEP_SUFFIXES = {".step", ".stp"}
SUPPORTED_INPUT_SUFFIXES = SUPPORTED_2D_SUFFIXES | SUPPORTED_STEP_SUFFIXES | {".json"}


@dataclass
class PartRequest:
    name: str
    path: Path | None = None
    polygons: list[list[list[float]]] | None = None
    quantity: int = 1
    rotations_deg: tuple[float, ...] = (0.0, 90.0, 180.0, 270.0)
    grain_locked: bool = False
    priority: int = 0
    thickness_mm: float | None = None
    face_up_locked: bool | None = None

    def load_geometry_and_thickness(
        self, flatten_tolerance: float
    ) -> tuple[BaseGeometry, float | None]:
        if self.polygons is not None:
            rings = [
                [(float(point[0]), float(point[1])) for point in ring]
                for ring in self.polygons
            ]
            return geometry_from_rings(rings), self.thickness_mm
        if self.path is None:
            raise ValueError(f"零件 {self.name} 没有提供轮廓文件或坐标")
        suffix = self.path.suffix.lower()
        if suffix == ".dxf":
            geometry = load_dxf_geometry(self.path, flatten_tolerance)
            return geometry, self.thickness_mm
        if suffix == ".svg":
            geometry = load_svg_geometry(self.path, flatten_tolerance)
            return geometry, self.thickness_mm
        if suffix in SUPPORTED_STEP_SUFFIXES:
            from .step_input import load_step_geometry_details

            geometry, inferred_thickness = load_step_geometry_details(
                self.path, flatten_tolerance
            )
            # Geometry is authoritative for STEP; metadata and names never override it.
            return geometry, inferred_thickness
        raise ValueError(f"不支持的输入格式：{self.path}")


def parse_rotations(value: str | Sequence[float]) -> tuple[float, ...]:
    if isinstance(value, str):
        raw_values = [item.strip() for item in value.split(",") if item.strip()]
        rotations = [float(item) % 360.0 for item in raw_values]
    else:
        rotations = [float(item) % 360.0 for item in value]
    rotations = sorted(set(round(item, 8) for item in rotations))
    if not rotations:
        raise ValueError("至少需要一个允许的旋转角度")
    return tuple(rotations)


def _load_dxf_flattened_path(
    entity: Any,
    tolerance: float,
) -> tuple[list[list[list[float]]], list[list[list[float]]]]:
    path = make_path(entity, level=4)
    closed_rings: list[list[list[float]]] = []
    open_fragments: list[list[list[float]]] = []
    for subpath in list(path.sub_paths()):
        points = [(float(point.x), float(point.y)) for point in subpath.flattening(tolerance)]
        if len(points) < 2:
            continue
        if subpath.is_closed or math.dist(points[0], points[-1]) <= tolerance:
            closed_rings.append(points)
        else:
            open_fragments.append(points)
    return closed_rings, open_fragments


def load_dxf_geometry(path: Path, flatten_tolerance: float) -> BaseGeometry:
    document = ezdxf.readfile(str(path))
    modelspace = document.modelspace()
    rings: list[list[list[float]]] = []
    fragments: list[list[list[float]]] = []

    for entity in modelspace:
        dxf_type = entity.dxftype()
        if dxf_type in {"TEXT", "MTEXT", "DIMENSION", "LEADER", "HATCH"}:
            continue
        try:
            entity_rings, entity_fragments = _load_dxf_flattened_path(
                entity, flatten_tolerance
            )
        except (TypeError, ValueError, NotImplementedError):
            continue
        rings.extend(entity_rings)
        fragments.extend(entity_fragments)

    rings.extend(stitch_point_sequences(fragments, tolerance=flatten_tolerance * 2.0))
    if not rings:
        raise ValueError(f"DXF 中没有找到封闭轮廓：{path}")
    return geometry_from_rings(rings)


def _segment_points(segment: Any, tolerance: float) -> list[tuple[float, float]]:
    try:
        length = float(segment.length())
    except Exception:
        length = 0.0
    count = max(1, int(math.ceil(length / max(tolerance, 1.0e-6))))
    points: list[tuple[float, float]] = []
    for index in range(count + 1):
        point = segment.point(index / count)
        points.append((float(point.x), -float(point.y)))  # SVG Y axis points down.
    return points


def load_svg_geometry(path: Path, flatten_tolerance: float) -> BaseGeometry:
    svg = se.SVG.parse(str(path))
    rings: list[list[list[float]]] = []
    fragments: list[list[list[float]]] = []

    for element in svg.elements():
        if isinstance(element, se.SVG):
            continue
        try:
            d = element.d()
            if not d:
                continue
            path_element = se.Path(d)
            for subpath in path_element.as_subpaths():
                segments = subpath.segments()
                has_close = any(isinstance(segment, se.Close) for segment in segments)
                points: list[tuple[float, float]] = []
                for segment in segments:
                    if isinstance(segment, se.Move):
                        points.append((float(segment.end.x), -float(segment.end.y)))
                        continue
                    segment_points = _segment_points(segment, flatten_tolerance)
                    if points and math.dist(points[-1], segment_points[0]) > flatten_tolerance * 3.0:
                        points.extend(segment_points)
                    else:
                        points.extend(segment_points[1:] if points else segment_points)
                if len(points) < 2:
                    continue
                if has_close or math.dist(points[0], points[-1]) <= flatten_tolerance * 3.0:
                    rings.append([[point[0], point[1]] for point in points])
                else:
                    fragments.append([[point[0], point[1]] for point in points])
        except (TypeError, ValueError, AttributeError):
            continue

    rings.extend(stitch_point_sequences(fragments, tolerance=flatten_tolerance * 3.0))
    if not rings:
        raise ValueError(f"SVG 中没有找到封闭轮廓：{path}")
    return geometry_from_rings(rings)


def _request_from_mapping(
    mapping: dict[str, Any],
    base_dir: Path,
    default_quantity: int,
    default_rotations: tuple[float, ...],
) -> PartRequest:
    name = str(mapping.get("name") or mapping.get("id") or "part")
    quantity = int(mapping.get("quantity", default_quantity))
    grain_locked = bool(mapping.get("grain_locked", False))
    raw_rotations = mapping.get("rotations")
    if raw_rotations is None and grain_locked:
        raw_rotations = (0.0, 180.0)
    rotations = parse_rotations(
        raw_rotations if raw_rotations is not None else default_rotations
    )
    polygons = mapping.get("polygons")
    if polygons is not None and not isinstance(polygons, list):
        raise ValueError(f"零件 {name} 的 polygons 必须是坐标环列表")

    raw_path = mapping.get("path")
    part_path = None
    raw_thickness = mapping.get("thickness_mm")
    thickness_mm = None if raw_thickness is None else float(raw_thickness)
    raw_face_up_locked = mapping.get("face_up_locked")
    face_up_locked = (
        None if raw_face_up_locked is None else bool(raw_face_up_locked)
    )

    if raw_path:
        part_path = Path(str(raw_path)).expanduser()
        if not part_path.is_absolute():
            part_path = base_dir / part_path
        part_path = part_path.resolve()

    if part_path is None and polygons is None:
        raise ValueError(f"零件 {name} 必须提供 path 或 polygons")

    return PartRequest(
        name=name,
        path=part_path,
        polygons=polygons,
        quantity=quantity,
        rotations_deg=rotations,
        grain_locked=grain_locked,
        priority=int(mapping.get("priority", 0)),
        thickness_mm=thickness_mm,
        face_up_locked=face_up_locked,
    )


def request_from_mapping(
    mapping: dict[str, Any],
    base_dir: Path,
    default_quantity: int = 1,
    default_rotations: tuple[float, ...] = (0.0, 90.0, 180.0, 270.0),
) -> PartRequest:
    """Public wrapper used by project-file loading in the desktop application."""

    return _request_from_mapping(
        mapping,
        base_dir,
        default_quantity,
        default_rotations,
    )


def load_manifest(
    path: Path,
    default_quantity: int,
    default_rotations: tuple[float, ...],
) -> list[PartRequest]:
    path = path.expanduser().resolve()
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    mappings = data.get("parts") if isinstance(data, dict) else data
    if not isinstance(mappings, list):
        raise ValueError("JSON 清单格式应为包含 parts 数组的对象")
    return [
        _request_from_mapping(item, path.parent, default_quantity, default_rotations)
        for item in mappings
    ]


def _is_part_json(path: Path) -> bool:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return False
    if isinstance(data, list):
        return True
    if not isinstance(data, dict):
        return False
    return "parts" in data or "polygons" in data or "path" in data


def expand_input_paths(paths: Iterable[Path]) -> list[Path]:
    files: list[Path] = []
    for raw_path in paths:
        path = raw_path.expanduser().resolve()
        if path.is_dir():
            for candidate in sorted(path.rglob("*"), key=lambda item: str(item).casefold()):
                if not candidate.is_file():
                    continue
                suffix = candidate.suffix.lower()
                if suffix not in SUPPORTED_INPUT_SUFFIXES:
                    continue
                if suffix == ".json" and not _is_part_json(candidate):
                    continue
                files.append(candidate)
        elif path.is_file():
            files.append(path)
        else:
            raise FileNotFoundError(f"输入路径不存在：{path}")
    return files


def load_requests(
    paths: Iterable[Path],
    manifest: Path | None,
    default_quantity: int,
    default_rotations: tuple[float, ...],
) -> list[PartRequest]:
    requests: list[PartRequest] = []
    if manifest is not None:
        requests.extend(load_manifest(manifest, default_quantity, default_rotations))

    for path in expand_input_paths(paths):
        suffix = path.suffix.lower()
        if suffix == ".json":
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict) and "parts" in data:
                requests.extend(load_manifest(path, default_quantity, default_rotations))
            elif isinstance(data, dict):
                requests.append(
                    _request_from_mapping(
                        data, path.parent, default_quantity, default_rotations
                    )
                )
            else:
                raise ValueError(f"JSON 输入格式无效：{path}")
        else:
            requests.append(
                PartRequest(
                    name=path.stem,
                    path=path,
                    quantity=default_quantity,
                    rotations_deg=default_rotations,
                )
            )
    return requests


@dataclass(frozen=True)
class ThicknessGroup:
    value_mm: float | None
    definitions: list[PartDefinition]

    @property
    def folder_name(self) -> str:
        if self.value_mm is None:
            return "\u672a\u6307\u5b9a\u539a\u5ea6"
        return f"\u539a\u5ea6_{compact_thickness(self.value_mm)}mm"


def compact_thickness(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text if text and text != "-0" else "0"


def group_definitions_by_thickness(
    definitions: Sequence[PartDefinition],
    tolerance: float = 0.05,
    round_to: float = 0.05,
) -> list[ThicknessGroup]:
    if tolerance < 0.0:
        raise ValueError("\u539a\u5ea6\u5206\u7ec4\u5bb9\u5dee\u4e0d\u80fd\u5c0f\u4e8e 0")
    numbered = [item for item in definitions if item.thickness_mm is not None]
    unknown = [item for item in definitions if item.thickness_mm is None]
    numbered.sort(key=lambda item: float(item.thickness_mm))

    clusters: list[list[PartDefinition]] = []
    for definition in numbered:
        value = float(definition.thickness_mm)
        best_cluster: list[PartDefinition] | None = None
        best_distance = float("inf")
        for cluster in clusters:
            representative = median(float(item.thickness_mm) for item in cluster)
            distance = abs(value - representative)
            if distance < best_distance:
                best_distance = distance
                best_cluster = cluster
        if best_cluster is not None and best_distance <= tolerance:
            best_cluster.append(definition)
        else:
            clusters.append([definition])

    groups: list[ThicknessGroup] = []
    for cluster in clusters:
        representative = median(float(item.thickness_mm) for item in cluster)
        if round_to > 0.0:
            representative = round(representative / round_to) * round_to
        groups.append(ThicknessGroup(float(representative), cluster))
    groups.sort(key=lambda item: float(item.value_mm or 0.0))
    if unknown:
        groups.append(ThicknessGroup(None, unknown))
    return groups


def definitions_from_requests(
    requests: Sequence[PartRequest],
    flatten_tolerance: float,
) -> list[PartDefinition]:
    definitions: list[PartDefinition] = []
    used_keys: dict[str, int] = {}
    for request in requests:
        geometry, thickness_mm = request.load_geometry_and_thickness(flatten_tolerance)
        base_key = request.name or (request.path.stem if request.path else "part")
        suffix = used_keys.get(base_key, 0)
        used_keys[base_key] = suffix + 1
        key = base_key if suffix == 0 else f"{base_key}_{suffix + 1}"
        definitions.append(
            PartDefinition(
                key=key,
                name=request.name,
                geometry=geometry,
                quantity=max(1, int(request.quantity)),
                rotations_deg=request.rotations_deg,
                source=str(request.path or "polygon-list"),
                grain_locked=request.grain_locked,
                priority=request.priority,
                thickness_mm=thickness_mm,
                face_up_locked=(
                    request.face_up_locked
                    if request.face_up_locked is not None
                    else request.path is not None
                    and request.path.suffix.lower() in SUPPORTED_STEP_SUFFIXES
                ),
            )
        )
    if not definitions:
        raise ValueError("没有加载到任何零件")
    return definitions
