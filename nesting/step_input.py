from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from OCP.gp import gp_Vec
from shapely.geometry import Polygon
from shapely.geometry.base import BaseGeometry

from .geometry import clean_geometry, close_ring, stitch_point_sequences
from .occ import (
    edge_sample_points,
    face_area_center,
    face_is_plane,
    face_normal,
    face_outer_wire,
    face_wires,
    iter_faces,
    iter_wire_edges,
    vector_between,
)
from step_thickness_classifier import import_single_shape


def _planar_faces(shape: Any) -> list[Any]:
    faces: list[Any] = []
    for face in iter_faces(shape):
        try:
            area, _ = face_area_center(face)
            if face_is_plane(face) and area > 1.0e-8:
                faces.append(face)
        except Exception:
            continue
    return faces


def _select_broad_face(shape: Any) -> tuple[Any, float]:
    faces = _planar_faces(shape)
    if len(faces) < 2:
        raise ValueError("STEP does not contain enough planar contours")

    candidates: list[tuple[float, Any, Any, float, float]] = []
    for left_index, left in enumerate(faces):
        left_normal = face_normal(left)
        left_area, left_center = face_area_center(left)
        for right in faces[left_index + 1 :]:
            right_normal = face_normal(right)
            if float(left_normal.Dot(right_normal)) > -math.cos(math.radians(0.5)):
                continue
            distance = abs(
                float(vector_between(left_center, face_area_center(right)[1]).Dot(left_normal))
            )
            if distance <= 1.0e-5:
                continue
            right_area = face_area_center(right)[0]
            area_ratio = min(left_area, right_area) / max(left_area, right_area)
            score = min(left_area, right_area) * area_ratio
            candidates.append((score, left, right, area_ratio, distance))

    if not candidates:
        raise ValueError("STEP broad faces were not found")

    _, first, second, _, thickness = max(candidates, key=lambda item: item[0])

    # Prefer the face whose outward normal points to the positive side.
    # This gives a deterministic top view for arbitrarily rotated plate parts.
    def normal_key(face: Any) -> tuple[float, float, float]:
        normal = face_normal(face)
        return float(normal.Z()), float(normal.X()), float(normal.Y())

    selected = first if normal_key(first) >= normal_key(second) else second
    return selected, float(thickness)


def _flatten_wire(
    wire: Any,
    tolerance: float,
    origin: Any,
    axis_u: Any,
    axis_v: Any,
) -> list[list[list[float]]]:
    edge_sequences: list[list[list[float]]] = []
    for edge in iter_wire_edges(wire):
        try:
            sequence: list[list[float]] = []
            for point in edge_sample_points(edge, tolerance):
                delta = vector_between(origin, point)
                sequence.append([float(delta.Dot(axis_u)), float(delta.Dot(axis_v))])
            edge_sequences.append(sequence)
        except Exception:
            continue

    if not edge_sequences:
        raise ValueError("STEP \u9762\u4e0a\u7684\u8fb9\u65e0\u6cd5\u79bb\u6563")
    rings = stitch_point_sequences(edge_sequences, tolerance=max(tolerance, 1.0e-4))
    if not rings:
        raise ValueError("STEP \u9762\u4e0a\u7684\u8fb9\u65e0\u6cd5\u79bb\u6563")
    return rings


def load_step_geometry_details(
    path: Path, flatten_tolerance: float
) -> tuple[BaseGeometry, float]:
    shape = import_single_shape(path)
    face, thickness = _select_broad_face(shape)
    normal = face_normal(face).Normalized()
    helper = gp_Vec(1.0, 0.0, 0.0)
    if abs(float(normal.Dot(helper))) > 0.8:
        helper = gp_Vec(0.0, 1.0, 0.0)
    axis_u = helper.Crossed(normal).Normalized()
    axis_v = normal.Crossed(axis_u).Normalized()
    origin = face_area_center(face)[1]

    outer_2d = close_ring(
        _flatten_wire(face_outer_wire(face), flatten_tolerance, origin, axis_u, axis_v)[0]
    )
    holes_2d: list[list[list[float]]] = []
    for inner_wire, is_outer in face_wires(face):
        if is_outer:
            continue
        for ring in _flatten_wire(inner_wire, flatten_tolerance, origin, axis_u, axis_v):
            projected = close_ring(ring)
            if len(projected) >= 4 and abs(Polygon(projected).area) > 1.0e-8:
                holes_2d.append(projected)

    polygon = Polygon(outer_2d, holes=holes_2d)
    geometry = clean_geometry(polygon)
    if geometry.is_empty or float(geometry.area) <= 0.0:
        raise ValueError(f"STEP \u6295\u5f71\u8f6e\u5ed3\u65e0\u6548\uff1a{path}")
    return geometry, thickness


def load_step_geometry(path: Path, flatten_tolerance: float) -> BaseGeometry:
    return load_step_geometry_details(path, flatten_tolerance)[0]
