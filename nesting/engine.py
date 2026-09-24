from __future__ import annotations

from dataclasses import dataclass

from shapely.affinity import translate
from shapely.geometry import Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.prepared import prep

from .common_line import create_common_line_policy
from .geometry import (
    clean_geometry,
    contact_points,
    iter_polygons,
    fill_holes,
    polygon_interiors,
    rotate_and_normalize,
    safe_buffer,
    union_geometries,
)
from .models import (
    NestingCancelled,
    NestingConfig,
    NestingSolution,
    OrientedShape,
    PartDefinition,
    PartInstance,
    Placement,
    SheetLayout,
)


@dataclass(frozen=True)
class CandidatePosition:
    x: float
    y: float
    actual: BaseGeometry
    collision: BaseGeometry
    nested_in_hole: bool
    thermal_density: float
    score: tuple[float, float, float, float]


class NestingEngine:
    """Bottom-left-fill placement engine with clearance-aware collision checks."""

    def __init__(self, definitions: list[PartDefinition], config: NestingConfig):
        config.validate()
        self.config = config
        self.definitions = definitions
        self.instances: list[PartInstance] = []
        for definition in definitions:
            for _ in range(definition.quantity):
                self.instances.append(PartInstance(len(self.instances), definition))

        self._oriented_cache: dict[tuple[int, float], OrientedShape] = {}
        self._prepared_obstacles: dict[int, object] = {}
        self._minimum_part_area = min(
            (float(definition.geometry.area) for definition in definitions),
            default=0.0,
        )
        self._minimum_part_dimension = min(
            (
                min(
                    float(definition.bounds[2] - definition.bounds[0]),
                    float(definition.bounds[3] - definition.bounds[1]),
                )
                for definition in definitions
            ),
            default=0.0,
        )
        self._collision_tolerance = min(0.10, max(0.0, config.flatten_tolerance * 0.25))
        self._sheet_region = box(
            config.effective_edge_margin,
            config.effective_edge_margin,
            config.sheet_width - config.effective_edge_margin,
            config.sheet_height - config.effective_edge_margin,
        )
        self._evaluations = 0
        self.common_line_policy = create_common_line_policy(
            config.common_line_policy
        )

    @property
    def allowed_angles(self) -> list[tuple[float, ...]]:
        return [instance.definition.rotations_deg for instance in self.instances]

    def _collision_base(self, actual: BaseGeometry) -> BaseGeometry:
        if not self.config.allow_hole_nesting:
            return fill_holes(actual)

        # Tiny holes cannot contain any requested part. Filling them in the
        # collision layer greatly reduces geometry complexity while remaining
        # conservative for nesting.
        minimum_hole_area = self._minimum_part_area * 0.50
        minimum_hole_dimension = (
            self._minimum_part_dimension + self.config.effective_clearance
        )
        polygons: list[Polygon] = []
        for polygon in iter_polygons(actual):
            exterior = Polygon(polygon.exterior)
            if self._collision_tolerance > 0.0:
                exterior = exterior.simplify(
                    self._collision_tolerance, preserve_topology=True
                )
            holes: list[list[tuple[float, float]]] = []
            for interior in polygon.interiors:
                hole_polygon = Polygon(interior)
                hole_min_x, hole_min_y, hole_max_x, hole_max_y = hole_polygon.bounds
                hole_width = hole_max_x - hole_min_x
                hole_height = hole_max_y - hole_min_y
                if (
                    hole_polygon.area >= minimum_hole_area
                    and min(hole_width, hole_height) >= minimum_hole_dimension
                ):
                    simplified_hole = hole_polygon
                    if self._collision_tolerance > 0.0:
                        simplified_hole = simplified_hole.simplify(
                            self._collision_tolerance, preserve_topology=True
                        )
                    holes.append(
                        [(float(x), float(y)) for x, y, *_ in simplified_hole.exterior.coords]
                    )
            polygons.append(Polygon(exterior.exterior.coords, holes=holes))
        return union_geometries(polygons)

    def _oriented(self, instance_index: int, angle_deg: float) -> OrientedShape:
        instance = self.instances[instance_index]
        key = (instance_index, float(angle_deg))
        cached = self._oriented_cache.get(key)
        if cached is not None:
            return cached

        actual = rotate_and_normalize(instance.definition.geometry, angle_deg)
        obstacle_base = self._collision_base(actual)
        collision_distance = (
            self.config.effective_clearance / 2.0 + self._collision_tolerance
        )
        collision_raw = safe_buffer(obstacle_base, collision_distance)

        min_x, min_y, _, _ = collision_raw.bounds
        actual = translate(actual, xoff=-min_x, yoff=-min_y)
        collision = translate(collision_raw, xoff=-min_x, yoff=-min_y)
        result = OrientedShape(
            instance=instance,
            angle_deg=float(angle_deg),
            actual=clean_geometry(actual),
            collision=clean_geometry(collision),
            actual_bounds=tuple(float(value) for value in actual.bounds),
            collision_bounds=tuple(float(value) for value in collision.bounds),
        )
        self._oriented_cache[key] = result
        return result

    def _new_sheet(self, index: int) -> SheetLayout:
        return SheetLayout(
            index=index,
            width=self.config.sheet_width,
            height=self.config.sheet_height,
            margin=self.config.effective_edge_margin,
        )

    def _candidate_origins(
        self,
        sheet: SheetLayout,
        candidate: OrientedShape,
    ) -> list[tuple[float, float]]:
        sheet_min_x, sheet_min_y, sheet_max_x, sheet_max_y = self._sheet_region.bounds
        candidate_min_x, candidate_min_y, candidate_max_x, candidate_max_y = (
            candidate.collision_bounds
        )
        actual_min_x, actual_min_y, actual_max_x, actual_max_y = candidate.actual_bounds
        width = candidate_max_x - candidate_min_x
        height = candidate_max_y - candidate_min_y
        min_origin_x = sheet_min_x - actual_min_x
        min_origin_y = sheet_min_y - actual_min_y
        max_origin_x = sheet_max_x - actual_max_x
        max_origin_y = sheet_max_y - actual_max_y

        origins: set[tuple[float, float]] = {(min_origin_x, min_origin_y)}

        if sheet.obstacles is not None and not sheet.obstacles.is_empty:
            # Bounding-box skyline anchors guarantee that ordinary rectangular
            # gaps are not missed when the union contour is heavily simplified.
            for placed in sheet.placements:
                placement_bounds = placed.collision.bounds
                for obstacle_x in (placement_bounds[0], placement_bounds[2]):
                    for candidate_x in (candidate_min_x, candidate_max_x):
                        origins.add(
                            (
                                obstacle_x - candidate_x,
                                min_origin_y,
                            )
                        )
                        origins.add(
                            (
                                obstacle_x - candidate_x,
                                placement_bounds[3] - candidate_min_y,
                            )
                        )
                for obstacle_y in (placement_bounds[1], placement_bounds[3]):
                    origins.add((min_origin_x, obstacle_y - candidate_min_y))

            obstacle_points = contact_points(sheet.obstacles, max_points=16)
            candidate_points = contact_points(candidate.collision, max_points=6)
            candidate_points.extend(
                [
                    (candidate.collision.bounds[0], candidate.collision.bounds[1]),
                    (candidate.collision.bounds[2], candidate.collision.bounds[1]),
                    (candidate.collision.bounds[0], candidate.collision.bounds[3]),
                    (candidate.collision.bounds[2], candidate.collision.bounds[3]),
                ]
            )
            for obstacle_x, obstacle_y in obstacle_points:
                origins.add((obstacle_x - candidate_min_x, obstacle_y - candidate_min_y))
                origins.add((obstacle_x, min_origin_y))
                origins.add((min_origin_x, obstacle_y))
                for candidate_x, candidate_y in candidate_points:
                    origins.add((obstacle_x - candidate_x, obstacle_y - candidate_y))

        # Holes are strong candidates: aligning to their bounding boxes creates
        # useful starting positions even for concave inserted shapes.
        if self.config.allow_hole_nesting:
            for placement in sheet.placements:
                for hole in placement.holes:
                    hole_min_x, hole_min_y, hole_max_x, hole_max_y = hole.bounds
                    origins.add((hole_min_x - candidate_min_x, hole_min_y - candidate_min_y))
                    origins.add(
                        (
                            (hole_min_x + hole_max_x - width) / 2.0,
                            (hole_min_y + hole_max_y - height) / 2.0,
                        )
                    )
                    origins.add((hole_max_x - candidate_max_x, hole_max_y - candidate_max_y))

        filtered: list[tuple[float, float]] = []
        for x, y in origins:
            if x < min_origin_x - self.config.overlap_epsilon:
                continue
            if y < min_origin_y - self.config.overlap_epsilon:
                continue
            if x > max_origin_x + self.config.overlap_epsilon:
                continue
            if y > max_origin_y + self.config.overlap_epsilon:
                continue
            filtered.append((float(x), float(y)))

        # Quantize duplicate positions created by curved contours.
        unique: dict[tuple[int, int], tuple[float, float]] = {}
        for x, y in filtered:
            key = (round(x * 1000.0), round(y * 1000.0))
            unique.setdefault(key, (x, y))
        return list(unique.values())

    def _is_inside_hole(self, sheet: SheetLayout, actual: BaseGeometry) -> bool:
        if not self.config.allow_hole_nesting:
            return False
        for placement in sheet.placements:
            for hole in placement.holes:
                hole_bounds = hole.bounds
                if (
                    hole_bounds[0] <= actual.bounds[0]
                    and hole_bounds[1] <= actual.bounds[1]
                    and hole_bounds[2] >= actual.bounds[2]
                    and hole_bounds[3] >= actual.bounds[3]
                    and hole.covers(actual)
                ):
                    return True
        return False

    def _thermal_density(
        self,
        sheet: SheetLayout,
        actual: BaseGeometry,
    ) -> float:
        if self.config.thermal_density_limit >= 1.0 or not sheet.placements:
            return 0.0
        center = actual.centroid
        radius = max(1.0, self.config.thermal_radius)
        window = box(
            center.x - radius,
            center.y - radius,
            center.x + radius,
            center.y + radius,
        )
        denominator = float(window.area)
        if denominator <= 0.0:
            return 0.0
        occupied = 0.0
        for placement in sheet.placements:
            try:
                occupied += float(window.intersection(placement.actual).area)
            except Exception:
                continue
        return min(1.0, occupied / denominator)

    def _evaluate_position(
        self,
        sheet: SheetLayout,
        candidate: OrientedShape,
        x: float,
        y: float,
    ) -> CandidatePosition | None:
        actual = translate(candidate.actual, xoff=x, yoff=y)
        collision = translate(candidate.collision, xoff=x, yoff=y)

        if not self._sheet_region.covers(actual):
            return None

        if sheet.obstacles is not None and not sheet.obstacles.is_empty:
            try:
                prepared_obstacles = self._prepared_obstacles.get(sheet.index)
                intersects = (
                    prepared_obstacles.intersects(collision)
                    if prepared_obstacles is not None
                    else sheet.obstacles.intersects(collision)
                )
                if intersects:
                    positive_overlap = (
                        prepared_obstacles.overlaps(collision)
                        or prepared_obstacles.contains(collision)
                        if prepared_obstacles is not None
                        else sheet.obstacles.overlaps(collision)
                        or sheet.obstacles.contains(collision)
                    )
                    if positive_overlap:
                        overlap = float(collision.intersection(sheet.obstacles).area)
                        if overlap > self.config.overlap_epsilon:
                            return None
            except Exception:
                return None

        thermal_density = self._thermal_density(sheet, actual)
        if thermal_density > self.config.thermal_density_limit + 1.0e-9:
            return None

        nested_in_hole = self._is_inside_hole(sheet, actual)
        actual_min_x, actual_min_y, actual_max_x, actual_max_y = actual.bounds
        previous = sheet.used_bounds
        if previous is None:
            growth = float(actual_max_x - actual_min_x) * float(actual_max_y - actual_min_y)
        else:
            new_min_x = min(previous[0], actual_min_x)
            new_min_y = min(previous[1], actual_min_y)
            new_max_x = max(previous[2], actual_max_x)
            new_max_y = max(previous[3], actual_max_y)
            growth = float((new_max_x - new_min_x) * (new_max_y - new_min_y)) - sheet.used_bbox_area
        score = (
            0.0 if nested_in_hole else 1.0,
            float(actual_max_y),
            float(actual_min_x),
            float(growth),
        )
        return CandidatePosition(
            x=float(x),
            y=float(y),
            actual=actual,
            collision=collision,
            nested_in_hole=nested_in_hole,
            thermal_density=thermal_density,
            score=score,
        )

    def _best_position(
        self,
        sheet: SheetLayout,
        candidate: OrientedShape,
    ) -> CandidatePosition | None:
        best: CandidatePosition | None = None
        for x, y in self._candidate_origins(sheet, candidate):
            evaluated = self._evaluate_position(sheet, candidate, x, y)
            if evaluated is None:
                continue
            if best is None or evaluated.score < best.score:
                best = evaluated
        return best

    def _place(
        self,
        sheet: SheetLayout,
        candidate: CandidatePosition,
        sequence: int,
        oriented: OrientedShape,
    ) -> None:
        placement = Placement(
            sequence=sequence,
            instance=oriented.instance,
            sheet_index=sheet.index,
            x=candidate.x,
            y=candidate.y,
            angle_deg=oriented.angle_deg,
            actual=candidate.actual,
            collision=candidate.collision,
            holes=list(polygon_interiors(candidate.actual)),
            nested_in_hole=candidate.nested_in_hole,
            thermal_density=candidate.thermal_density,
        )
        sheet.placements.append(placement)
        existing = [item.collision for item in sheet.placements]
        sheet.obstacles = union_geometries(existing)
        self._prepared_obstacles[sheet.index] = prep(sheet.obstacles)

    def evaluate(
        self,
        schedule: list[tuple[int, float]],
        cancel_callback=None,
        progress_callback=None,
        try_all_rotations: bool = False,
    ) -> NestingSolution:
        self._evaluations += 1
        sheets: list[SheetLayout] = []
        unplaced: list[PartInstance] = []

        for sequence, (instance_index, angle_deg) in enumerate(schedule):
            if cancel_callback is not None and cancel_callback():
                raise NestingCancelled("\u7528\u6237\u53d6\u6d88\u6392\u7248")
            instance = self.instances[instance_index]
            allowed = instance.definition.rotations_deg
            if try_all_rotations:
                orientations = [self._oriented(instance_index, angle) for angle in allowed]
            else:
                chosen_angle = min(allowed, key=lambda value: abs(value - float(angle_deg)))
                orientations = [self._oriented(instance_index, chosen_angle)]

            chosen = None
            for sheet in sheets:
                for oriented in orientations:
                    best = self._best_position(sheet, oriented)
                    if best is None:
                        continue
                    candidate = (sheet, oriented, best)
                    if chosen is None or best.score < chosen[2].score:
                        chosen = candidate

            if chosen is not None:
                sheet, oriented, best = chosen
                self._place(sheet, best, sequence, oriented)
                if progress_callback is not None:
                    progress_callback(sequence + 1, len(schedule))
                continue

            if self.config.max_sheets and len(sheets) >= self.config.max_sheets:
                unplaced.append(instance)
                if progress_callback is not None:
                    progress_callback(sequence + 1, len(schedule))
                continue

            sheet = self._new_sheet(len(sheets))
            new_sheet_choice = None
            for oriented in orientations:
                best = self._best_position(sheet, oriented)
                if best is None:
                    continue
                if new_sheet_choice is None or best.score < new_sheet_choice[1].score:
                    new_sheet_choice = (oriented, best)
            if new_sheet_choice is None:
                unplaced.append(instance)
                if progress_callback is not None:
                    progress_callback(sequence + 1, len(schedule))
                continue
            oriented, best = new_sheet_choice
            sheets.append(sheet)
            self._place(sheet, best, sequence, oriented)
            if progress_callback is not None:
                progress_callback(sequence + 1, len(schedule))

        summary = {
            "sheet_width": self.config.sheet_width,
            "sheet_height": self.config.sheet_height,
            "margin": self.config.margin,
            "effective_edge_margin": self.config.effective_edge_margin,
            "part_clearance": self.config.part_clearance,
            "kerf": self.config.kerf,
            "lead_length": self.config.lead_length,
            "effective_clearance": self.config.effective_clearance,
            "allow_hole_nesting": self.config.allow_hole_nesting,
            "thermal_radius": self.config.thermal_radius,
            "thermal_density_limit": self.config.thermal_density_limit,
            "common_line_policy": self.config.common_line_policy,
        }
        return NestingSolution(
            sheets=sheets,
            unplaced=unplaced,
            schedule=schedule,
            optimizer="",
            evaluations=self._evaluations,
            config_summary=summary,
        )
