from __future__ import annotations

import math
from functools import reduce
from typing import Iterable, Iterator, Sequence

from shapely import make_valid, set_precision
from shapely.affinity import rotate, translate
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union


Point2D = tuple[float, float]
Ring2D = list[Point2D]


def close_ring(points: Iterable[Sequence[float]], tolerance: float = 1.0e-6) -> Ring2D:
    ring: Ring2D = [(float(point[0]), float(point[1])) for point in points]
    cleaned: Ring2D = []
    for point in ring:
        if not cleaned or math.dist(cleaned[-1], point) > tolerance:
            cleaned.append(point)
    if len(cleaned) > 1 and math.dist(cleaned[0], cleaned[-1]) <= tolerance:
        cleaned.pop()
    if len(cleaned) >= 3 and cleaned[0] != cleaned[-1]:
        cleaned.append(cleaned[0])
    return cleaned


def signed_area(points: Sequence[Sequence[float]]) -> float:
    if len(points) < 3:
        return 0.0
    return 0.5 * sum(
        float(points[index][0]) * float(points[(index + 1) % len(points)][1])
        - float(points[(index + 1) % len(points)][0]) * float(points[index][1])
        for index in range(len(points))
    )


def iter_polygons(geometry: BaseGeometry) -> Iterator[Polygon]:
    if geometry.is_empty:
        return
    if isinstance(geometry, Polygon):
        yield geometry
    elif isinstance(geometry, MultiPolygon):
        yield from geometry.geoms
    elif isinstance(geometry, GeometryCollection):
        for item in geometry.geoms:
            yield from iter_polygons(item)


def clean_geometry(geometry: BaseGeometry, grid_size: float = 1.0e-5) -> BaseGeometry:
    if geometry.is_empty:
        return geometry
    repaired = make_valid(geometry)
    if grid_size > 0.0:
        repaired = set_precision(repaired, grid_size)
    repaired = make_valid(repaired)
    polygons = list(iter_polygons(repaired))
    if not polygons:
        return GeometryCollection()
    if len(polygons) == 1:
        return polygons[0]
    return unary_union(polygons)


def geometry_from_rings(rings: Sequence[Ring2D], precision: float = 1.0e-5) -> BaseGeometry:
    """Build even-odd filled geometry from an unordered set of rings."""

    polygon_rings: list[Polygon] = []
    for raw_ring in rings:
        ring = close_ring(raw_ring)
        if len(ring) < 4 or abs(signed_area(ring[:-1])) < 1.0e-8:
            continue
        polygon = clean_geometry(Polygon(ring), precision)
        polygon_rings.extend(iter_polygons(polygon))

    if not polygon_rings:
        raise ValueError("没有找到有效的封闭轮廓")

    filled = polygon_rings[0]
    for polygon in polygon_rings[1:]:
        filled = filled.symmetric_difference(polygon)
    filled = clean_geometry(filled, precision)
    if filled.is_empty or float(filled.area) <= 0.0:
        raise ValueError("轮廓面积无效或轮廓互相抵消")
    return filled


def stitch_point_sequences(
    sequences: Sequence[Sequence[Sequence[float]]], tolerance: float = 1.0e-4
) -> list[Ring2D]:
    """Join open polyline fragments into closed rings using nearest endpoints."""

    fragments: list[list[Point2D]] = []
    for sequence in sequences:
        points = [(float(point[0]), float(point[1])) for point in sequence]
        if len(points) >= 2:
            fragments.append(points)
    if not fragments:
        return []

    rings: list[Ring2D] = []
    while fragments:
        chain = fragments.pop(0)
        changed = True
        while changed and fragments and math.dist(chain[0], chain[-1]) > tolerance:
            changed = False
            best_index = -1
            best_reverse = False
            best_distance = math.inf
            for index, fragment in enumerate(fragments):
                candidates = (
                    (math.dist(chain[-1], fragment[0]), False),
                    (math.dist(chain[-1], fragment[-1]), True),
                    (math.dist(chain[0], fragment[-1]), False),
                    (math.dist(chain[0], fragment[0]), True),
                )
                distance, reverse = min(candidates)
                if distance < best_distance:
                    best_distance = distance
                    best_index = index
                    best_reverse = reverse

            if best_index >= 0 and best_distance <= tolerance * 10.0:
                fragment = fragments.pop(best_index)
                if best_reverse:
                    fragment = list(reversed(fragment))
                if math.dist(chain[-1], fragment[0]) <= math.dist(chain[0], fragment[-1]):
                    chain.extend(fragment[1:])
                else:
                    fragment.extend(chain[1:])
                    chain = fragment
                changed = True

        if len(chain) >= 4 and math.dist(chain[0], chain[-1]) <= tolerance * 10.0:
            chain[0] = ((chain[0][0] + chain[-1][0]) / 2.0, (chain[0][1] + chain[-1][1]) / 2.0)
            chain.pop()
            rings.append(close_ring(chain, tolerance))
    return rings


def safe_buffer(geometry: BaseGeometry, distance: float) -> BaseGeometry:
    if abs(distance) <= 1.0e-12:
        return clean_geometry(geometry)
    return clean_geometry(
        geometry.buffer(
            distance,
            join_style=2,
            mitre_limit=8.0,
            cap_style=2,
            quad_segs=16,
        )
    )


def fill_holes(geometry: BaseGeometry) -> BaseGeometry:
    polygons: list[Polygon] = []
    for polygon in iter_polygons(geometry):
        polygons.append(Polygon(polygon.exterior))
    if not polygons:
        return geometry
    return clean_geometry(unary_union(polygons))


def rotate_and_normalize(geometry: BaseGeometry, angle_deg: float) -> BaseGeometry:
    rotated = rotate(geometry, angle_deg, origin=(0.0, 0.0), use_radians=False)
    min_x, min_y, _, _ = rotated.bounds
    return clean_geometry(translate(rotated, xoff=-min_x, yoff=-min_y))


def polygon_interiors(geometry: BaseGeometry) -> Iterator[Polygon]:
    for polygon in iter_polygons(geometry):
        for interior in polygon.interiors:
            yield Polygon(interior)


def as_ring_list(geometry: BaseGeometry) -> list[Ring2D]:
    rings: list[Ring2D] = []
    for polygon in iter_polygons(geometry):
        rings.append([(float(x), float(y)) for x, y, *_ in polygon.exterior.coords])
        for interior in polygon.interiors:
            rings.append([(float(x), float(y)) for x, y, *_ in interior.coords])
    return rings


def contact_points(
    geometry: BaseGeometry,
    max_points: int = 64,
    simplify_tolerance: float = 0.1,
) -> list[Point2D]:
    points: list[Point2D] = []
    for polygon in iter_polygons(geometry):
        simplified = polygon.simplify(simplify_tolerance, preserve_topology=True)
        for x, y, *_ in simplified.exterior.coords:
            points.append((float(x), float(y)))
        for interior in simplified.interiors:
            for x, y, *_ in interior.coords:
                points.append((float(x), float(y)))

    if len(points) <= max_points:
        return points
    step = max(1, len(points) // max_points)
    sampled = points[::step]
    return sampled[:max_points]


def geometry_distance_or_none(a: BaseGeometry, b: BaseGeometry) -> float | None:
    if a.is_empty or b.is_empty:
        return None
    return float(a.distance(b))


def union_geometries(geometries: Iterable[BaseGeometry]) -> BaseGeometry:
    items = [item for item in geometries if not item.is_empty]
    if not items:
        return GeometryCollection()
    return clean_geometry(reduce(lambda left, right: left.union(right), items))
