from __future__ import annotations

import math
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from OCP.BRep import BRep_Builder
from OCP.BRepAdaptor import BRepAdaptor_Curve, BRepAdaptor_Surface
from OCP.BRepGProp import BRepGProp
from OCP.BRepTools import BRepTools
from OCP.GProp import GProp_GProps
from OCP.GeomAbs import GeomAbs_Plane
from OCP.IFSelect import IFSelect_RetDone
from OCP.Interface import Interface_Static
from OCP.STEPControl import STEPControl_Reader
from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_REVERSED, TopAbs_WIRE
from OCP.TopExp import TopExp, TopExp_Explorer
from OCP.TopTools import TopTools_IndexedMapOfShape
from OCP.TopoDS import TopoDS, TopoDS_Compound
from OCP.gp import gp_Pnt, gp_Vec


def _shape_iterator(shape: Any, shape_type: Any) -> Iterator[Any]:
    explorer = TopExp_Explorer(shape, shape_type)
    while explorer.More():
        yield explorer.Current()
        explorer.Next()


def import_step_shape(path: Path) -> Any:
    """Read one STEP file with OpenCascade and return a single shape."""
    Interface_Static.SetCVal_s("xstep.cascade.unit", "MM")
    reader = STEPControl_Reader()
    status = reader.ReadFile(str(path))
    if status != IFSelect_RetDone:
        raise ValueError(f"STEP file could not be loaded: {path}")
    for index in range(reader.NbRootsForTransfer()):
        reader.TransferRoot(index + 1)

    shapes = [reader.Shape(index + 1) for index in range(reader.NbShapes())]
    if not shapes:
        raise ValueError("STEP file contains no readable geometry")
    if len(shapes) == 1:
        return shapes[0]

    builder = BRep_Builder()
    compound = TopoDS_Compound()
    builder.MakeCompound(compound)
    for shape in shapes:
        builder.Add(compound, shape)
    return compound


def iter_faces(shape: Any) -> Iterator[Any]:
    for item in _shape_iterator(shape, TopAbs_FACE):
        yield TopoDS.Face_s(item)


def face_area_center(face: Any) -> tuple[float, gp_Pnt]:
    properties = GProp_GProps()
    BRepGProp.SurfaceProperties_s(face, properties)
    return float(properties.Mass()), properties.CentreOfMass()


def face_normal(face: Any) -> gp_Vec:
    surface = BRepAdaptor_Surface(face)
    u_mid = (surface.FirstUParameter() + surface.LastUParameter()) * 0.5
    v_mid = (surface.FirstVParameter() + surface.LastVParameter()) * 0.5
    point = gp_Pnt()
    du = gp_Vec()
    dv = gp_Vec()
    surface.D1(u_mid, v_mid, point, du, dv)
    normal = du.Crossed(dv).Normalized()
    if face.Orientation() == TopAbs_REVERSED:
        normal.Reverse()
    return normal


def face_is_plane(face: Any) -> bool:
    return BRepAdaptor_Surface(face).GetType() == GeomAbs_Plane


def face_outer_wire(face: Any) -> Any:
    return BRepTools.OuterWire_s(face)


def face_wires(face: Any) -> Iterator[tuple[Any, bool]]:
    wire_map = TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(face, TopAbs_WIRE, wire_map)
    outer_wire = face_outer_wire(face)
    for index in range(1, wire_map.Size() + 1):
        wire = TopoDS.Wire_s(wire_map.FindKey(index))
        yield wire, wire.IsSame(outer_wire)


def iter_wire_edges(wire: Any) -> Iterator[Any]:
    for item in _shape_iterator(wire, TopAbs_EDGE):
        yield TopoDS.Edge_s(item)


def edge_sample_points(edge: Any, tolerance: float) -> list[gp_Pnt]:
    properties = GProp_GProps()
    BRepGProp.LinearProperties_s(edge, properties)
    length = float(properties.Mass())
    curve = BRepAdaptor_Curve(edge)
    first = curve.FirstParameter()
    last = curve.LastParameter()
    if edge.Orientation() == TopAbs_REVERSED:
        first, last = last, first

    count = max(1, min(1024, int(math.ceil(length / max(tolerance, 1.0e-6)))))
    points: list[gp_Pnt] = []
    for index in range(count + 1):
        parameter = first + (last - first) * index / count
        points.append(curve.Value(parameter))
    return points


def vector_between(origin: gp_Pnt, point: gp_Pnt) -> gp_Vec:
    return gp_Vec(origin, point)
