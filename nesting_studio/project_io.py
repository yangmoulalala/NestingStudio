from __future__ import annotations

from pathlib import Path
from typing import Any

from nesting.inputs import PartRequest


def is_global_summary(data: Any) -> bool:
    return isinstance(data, dict) and "thickness_groups" in data and "parts" not in data


def _config_for_panel(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "sheet_width": config.get("sheet_width", 2440.0),
        "sheet_height": config.get("sheet_height", 1220.0),
        "margin": config.get("margin", 10.0),
        "clearance": config.get("part_clearance", 5.0),
        "kerf": config.get("kerf", 0.0),
        "lead_length": config.get("lead_length", 0.0),
        "flatten_tolerance": config.get("flatten_tolerance", 0.5),
        "hole_nesting": config.get("allow_hole_nesting", True),
        "thermal_radius": config.get("thermal_radius", 80.0),
        "thermal_density": config.get("thermal_density_limit", 0.9),
        "max_sheets": config.get("max_sheets", 0),
        "common_line": config.get("common_line_policy", "disabled"),
    }


def import_global_summary(
    data: dict[str, Any],
    base_dir: Path,
) -> tuple[list[PartRequest], dict[str, Any], list[dict[str, Any]]]:
    """Convert an exported global summary back into project-style parts and layout."""

    requests_by_key: dict[tuple[str, str, float | None], PartRequest] = {}
    counts: dict[tuple[str, str, float | None], int] = {}
    layout_records: list[dict[str, Any]] = []
    missing_sources: list[str] = []

    for group_index, group in enumerate(data.get("thickness_groups", [])):
        thickness = group.get("thickness_mm")
        solution = group.get("solution", {})
        for sheet_index, sheet in enumerate(solution.get("sheets", [])):
            for order, placement in enumerate(sheet.get("placements", [])):
                source = placement.get("source")
                part_key = str(placement.get("part_key") or placement.get("part_name") or "part")
                part_name = str(placement.get("part_name") or part_key)
                if not source or source == "polygon-list":
                    raise ValueError(
                        f"导出报告中的零件“{part_name}”没有可重新读取的 CAD 源文件，"
                        "无法完整恢复。请使用保存的 .neststudio.json 项目文件。"
                    )
                source_path = Path(str(source)).expanduser()
                if not source_path.is_absolute():
                    source_path = base_dir / source_path
                source_path = source_path.resolve()
                if not source_path.exists():
                    missing_sources.append(str(source_path))
                    continue
                key = (str(source_path), part_key, None if thickness is None else float(thickness))
                counts[key] = counts.get(key, 0) + 1
                if key not in requests_by_key:
                    rotations = (0.0, 180.0) if thickness is None else (0.0, 90.0, 180.0, 270.0)
                    requests_by_key[key] = PartRequest(
                        name=part_name,
                        path=source_path,
                        quantity=1,
                        rotations_deg=rotations,
                        thickness_mm=None if thickness is None else float(thickness),
                        face_up_locked=True,
                    )

                anchor_x = placement.get("anchor_x", placement.get("x", 0.0))
                anchor_y = placement.get("anchor_y", placement.get("y", 0.0))
                actual_x = placement.get("x", anchor_x)
                actual_y = placement.get("y", anchor_y)
                layout_records.append(
                    {
                        "thickness_mm": thickness,
                        "part_key": part_key,
                        "sheet_index": sheet_index,
                        "instance_index": len(layout_records),
                        "sequence": order,
                        "x": anchor_x,
                        "y": anchor_y,
                        "rotation_deg": placement.get("rotation_deg", 0.0),
                        "actual_offset_x": actual_x - anchor_x,
                        "actual_offset_y": actual_y - anchor_y,
                        "mirrored": placement.get("mirrored", False),
                        "nested_in_hole": placement.get("nested_in_hole", False),
                    }
                )

    if missing_sources:
        unique = sorted(set(missing_sources))
        preview = "\n".join(unique[:8])
        raise ValueError(
            "导出报告中的以下源文件不存在，无法恢复：\n"
            f"{preview}"
            + (f"\n……共 {len(unique)} 个文件" if len(unique) > 8 else "")
        )

    requests = list(requests_by_key.values())
    for key, request in requests_by_key.items():
        request.quantity = counts[key]

    config = _config_for_panel(data.get("config", data.get("constraints", {})))
    return requests, config, layout_records
