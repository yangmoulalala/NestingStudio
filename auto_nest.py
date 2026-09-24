#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""板材零件自动套裁与嵌套排版工具。

厚度是排版的硬性分组条件：
- STEP 从 B-Rep 宽面几何自动计算厚度；
- DXF/SVG/坐标 JSON 必须由手册或命令行明确给出厚度；
- 程序不从文件名、目录名或零件名称推断任何厚度。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from nesting.engine import NestingEngine
from nesting.inputs import (
    PartRequest,
    compact_thickness,
    definitions_from_requests,
    group_definitions_by_thickness,
    load_requests,
    parse_rotations,
)
from nesting.models import NestingConfig
from nesting.optimize import OptimizerSettings, solve_nesting
from nesting.output import (
    export_sheet_dxf,
    solution_to_dict,
    validate_solution,
    write_csv_report,
    write_json_report,
)


def configure_console_stream(stream: object) -> None:
    if stream is None:
        return
    isatty = getattr(stream, "isatty", None)
    if callable(isatty) and not isatty() and hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


configure_console_stream(sys.stdout)
configure_console_stream(sys.stderr)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="板材零件自动套裁与嵌套排版（按厚度强制分组）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "inputs",
        nargs="*",
        type=Path,
        help="输入文件或文件夹；支持 DXF、SVG、STEP、STP、JSON",
    )
    parser.add_argument("--manifest", type=Path, help="JSON 零件清单")
    parser.add_argument(
        "--thickness",
        type=float,
        help="DXF/SVG/坐标 JSON 的明确厚度 mm；STEP 不需要",
    )
    parser.add_argument(
        "--thickness-tolerance",
        type=float,
        default=0.05,
        help="相同厚度组的最大误差 mm",
    )
    parser.add_argument(
        "--round-thickness",
        type=float,
        default=0.05,
        help="厚度文件夹标签取整步长 mm；0 表示不取整",
    )
    parser.add_argument(
        "--allow-unknown-thickness",
        action="store_true",
        help="允许把无法确定厚度的零件单独放入“未指定厚度”组",
    )
    parser.add_argument("-W", "--sheet-width", type=float, default=2440.0, help="板材宽度 mm")
    parser.add_argument("-H", "--sheet-height", type=float, default=1220.0, help="板材高度 mm")
    parser.add_argument("-q", "--quantity", type=int, default=1, help="普通输入文件的默认数量")
    parser.add_argument(
        "-r",
        "--rotations",
        default="0,90,180,270",
        help="默认允许旋转角，逗号分隔，例如 0,90,180,270",
    )
    parser.add_argument("--margin", type=float, default=10.0, help="真实轮廓与板材边缘的最小边距 mm")
    parser.add_argument("--clearance", type=float, default=5.0, help="零件间最小安全间距 mm")
    parser.add_argument("--kerf", type=float, default=0.0, help="刀缝宽度 mm")
    parser.add_argument("--lead-length", type=float, default=0.0, help="穿孔引线长度 mm")
    parser.add_argument(
        "--hole-nesting",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="是否允许小零件嵌套到大零件内孔",
    )
    parser.add_argument("--thermal-radius", type=float, default=80.0, help="热影响检查半径 mm")
    parser.add_argument(
        "--thermal-density-limit",
        type=float,
        default=0.90,
        help="半径范围内允许的最大材料密度；1 表示关闭热密度拒绝",
    )
    parser.add_argument("--flatten-tolerance", type=float, default=0.5, help="曲线离散容差 mm")
    parser.add_argument("--max-sheets", type=int, default=0, help="每个厚度组最多板材数；0 表示不限")
    parser.add_argument(
        "--optimizer",
        choices=("greedy", "sa", "ga"),
        default="greedy",
        help="顺序和旋转优化器；greedy 最快，sa/ga 用于进一步优化",
    )
    parser.add_argument("--iterations", type=int, default=80, help="SA 迭代次数")
    parser.add_argument("--population", type=int, default=12, help="GA 种群规模")
    parser.add_argument("--generations", type=int, default=20, help="GA 代数")
    parser.add_argument("--seed", type=int, default=2026, help="随机种子")
    parser.add_argument("--time-limit", type=float, default=60.0, help="每组优化时间上限秒")
    parser.add_argument("-o", "--output-dir", type=Path, help="输出目录")
    parser.add_argument("--prefix", default="nesting", help="输出文件前缀")
    parser.add_argument("--no-dxf", action="store_true", help="不导出 DXF")
    parser.add_argument("--dry-run", action="store_true", help="只计算并打印摘要，不写文件")
    parser.add_argument(
        "--common-line",
        choices=("disabled",),
        default="disabled",
        help="共边切割策略；当前保留接口，暂未启用",
    )
    return parser.parse_args()


def _progress(phase: str, current: int, total: int, best_cost: float) -> None:
    if phase == "placement":
        return
    print(f"  [{phase.upper()}] {current}/{total}，当前最优代价 {best_cost:.3f}")


def _apply_explicit_thickness(
    requests: list[PartRequest],
    thickness_mm: float | None,
) -> list[str]:
    missing: list[str] = []
    for request in requests:
        is_step = (
            request.path is not None
            and request.path.suffix.lower() in {".step", ".stp"}
        )
        if is_step or request.thickness_mm is not None:
            continue
        if thickness_mm is not None:
            request.thickness_mm = thickness_mm
        else:
            missing.append(request.name)
    return missing


def run(args: argparse.Namespace) -> int:
    if not args.inputs and args.manifest is None:
        print("请提供输入文件、输入目录或 --manifest。", file=sys.stderr)
        return 2
    if args.quantity < 1:
        print("--quantity 必须大于等于 1。", file=sys.stderr)
        return 2
    if args.thickness is not None and args.thickness <= 0.0:
        print("--thickness 必须大于 0。", file=sys.stderr)
        return 2

    if args.thickness_tolerance < 0.0:
        print("--thickness-tolerance \u4e0d\u80fd\u5c0f\u4e8e 0\u3002", file=sys.stderr)
        return 2
    if (
        args.round_thickness > 0.0
        and args.round_thickness > args.thickness_tolerance
    ):
        print(
            "\u4e3a\u907f\u514d\u4e0d\u540c\u539a\u5ea6\u7ec4\u7684\u8f93\u51fa\u76ee\u5f55\u91cd\u540d\uff0c--round-thickness \u4e0d\u80fd\u5927\u4e8e "
            "--thickness-tolerance\uff1b\u53ef\u8bbe\u7f6e --round-thickness 0\u3002",
            file=sys.stderr,
        )
        return 2

    rotations = parse_rotations(args.rotations)
    requests = load_requests(
        args.inputs,
        args.manifest,
        default_quantity=args.quantity,
        default_rotations=rotations,
    )
    missing_thickness = _apply_explicit_thickness(requests, args.thickness)
    if missing_thickness and not args.allow_unknown_thickness:
        print(
            "以下 2D/坐标零件没有可信厚度，程序不会从文件名或目录名猜测：\n"
            + "\n".join(f"  - {name}" for name in missing_thickness)
            + "\n请通过 --thickness 3.0 或清单中的 thickness_mm 指定；"
            "如果只是测试，可加 --allow-unknown-thickness。",
            file=sys.stderr,
        )
        return 2

    print(f"正在读取 {len(requests)} 种零件轮廓与厚度...")
    definitions = definitions_from_requests(requests, args.flatten_tolerance)
    thickness_groups = group_definitions_by_thickness(
        definitions,
        tolerance=args.thickness_tolerance,
        round_to=args.round_thickness,
    )

    config = NestingConfig(
        sheet_width=args.sheet_width,
        sheet_height=args.sheet_height,
        margin=args.margin,
        part_clearance=args.clearance,
        kerf=args.kerf,
        lead_length=args.lead_length,
        flatten_tolerance=args.flatten_tolerance,
        allow_hole_nesting=args.hole_nesting,
        thermal_radius=args.thermal_radius,
        thermal_density_limit=args.thermal_density_limit,
        common_line_policy=args.common_line,
        max_sheets=args.max_sheets,
    )
    config.validate()

    settings = OptimizerSettings(
        optimizer=args.optimizer,
        iterations=args.iterations,
        population_size=args.population,
        generations=args.generations,
        seed=args.seed,
        time_limit_seconds=args.time_limit,
    )

    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else Path.cwd() / f"{args.prefix}_output"
    )
    if not args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    print(
        "工艺参数："
        f"板材 {config.sheet_width:g}×{config.sheet_height:g} mm，"
        f"边距 {config.effective_edge_margin:g} mm，"
        f"零件间距 {config.effective_clearance:g} mm，"
        f"引线预留 {config.lead_length:g} mm"
    )
    print(f"按厚度分成 {len(thickness_groups)} 组，优化器 {args.optimizer.upper()}")

    global_groups: list[dict] = []
    all_success = True
    total_placed = 0
    total_requested = 0
    total_sheet_area = 0.0
    total_part_area = 0.0
    total_sheets = 0
    total_hole_nested = 0
    total_unplaced = 0

    for group_index, group in enumerate(thickness_groups, 1):
        thickness_label = (
            "未指定厚度"
            if group.value_mm is None
            else f"{compact_thickness(group.value_mm)} mm"
        )
        group_count = sum(definition.quantity for definition in group.definitions)
        print(f"\n[{group_index}/{len(thickness_groups)}] 厚度组 {thickness_label}"
              f"，{len(group.definitions)} 种 / {group_count} 件")

        engine = NestingEngine(group.definitions, config)
        solution = solve_nesting(engine, settings, progress_callback=_progress)
        validation = validate_solution(solution, config)
        report = solution_to_dict(solution, config)

        print(f"  使用板材：{len(solution.sheets)} 张")
        print(f"  已放置：{solution.placed_count}/{solution.requested_count} 件")
        print(f"  孔内嵌套：{solution.hole_nested_count} 件")
        print(f"  总利用率：{solution.total_utilization_percent:.3f}%")
        print(
            f"  最近间距：{validation['minimum_pair_distance_mm']:.3f} mm；"
            f"最近边距：{validation['minimum_edge_distance_mm']:.3f} mm"
        )
        for sheet in solution.sheets:
            print(
                f"  {sheet.name}: {len(sheet.placements)} 件，"
                f"利用率 {sheet.utilization_percent:.3f}%，"
                f"孔内嵌套 {sheet.hole_nested_count} 件"
            )
        if solution.unplaced:
            print(f"  未放置：{len(solution.unplaced)} 件")

        group_output_dir = output_dir / group.folder_name
        if not args.dry_run:
            group_output_dir.mkdir(parents=True, exist_ok=True)
            write_json_report(
                solution,
                config,
                group_output_dir / f"{args.prefix}_layout.json",
            )
            write_csv_report(
                solution,
                group_output_dir / f"{args.prefix}_placements.csv",
            )
            if not args.no_dxf:
                export_sheet_dxf(
                    solution,
                    config,
                    group_output_dir,
                    prefix=args.prefix,
                )

        group_success = not solution.unplaced and validation["overlap_count"] == 0
        all_success = all_success and group_success
        total_placed += solution.placed_count
        total_requested += solution.requested_count
        total_sheet_area += solution.sheet_area
        total_part_area += solution.part_area
        total_sheets += len(solution.sheets)
        total_hole_nested += solution.hole_nested_count
        total_unplaced += len(solution.unplaced)

        global_groups.append(
            {
                "folder": group.folder_name,
                "thickness_mm": group.value_mm,
                "part_types": len(group.definitions),
                "requested_parts": solution.requested_count,
                "placed_parts": solution.placed_count,
                "unplaced_parts": len(solution.unplaced),
                "sheet_count": len(solution.sheets),
                "part_area_mm2": round(solution.part_area, 6),
                "sheet_area_mm2": round(solution.sheet_area, 6),
                "utilization_percent": round(solution.total_utilization_percent, 6),
                "hole_nested_parts": solution.hole_nested_count,
                "optimizer": solution.optimizer,
                "evaluations": solution.evaluations,
                "validation": validation,
                "sheets": report["sheets"],
            }
        )

    global_report = {
        "summary": {
            "thickness_groups": len(thickness_groups),
            "requested_parts": total_requested,
            "placed_parts": total_placed,
            "unplaced_parts": total_unplaced,
            "sheet_count": total_sheets,
            "part_area_mm2": round(total_part_area, 6),
            "sheet_area_mm2": round(total_sheet_area, 6),
            "total_utilization_percent": (
                round(100.0 * total_part_area / total_sheet_area, 6)
                if total_sheet_area > 0.0
                else 0.0
            ),
            "hole_nested_parts": total_hole_nested,
        },
        "constraints": {
            "sheet_width_mm": config.sheet_width,
            "sheet_height_mm": config.sheet_height,
            "margin_mm": config.margin,
            "effective_edge_margin_mm": config.effective_edge_margin,
            "part_clearance_mm": config.part_clearance,
            "kerf_mm": config.kerf,
            "lead_length_mm": config.lead_length,
            "effective_clearance_mm": config.effective_clearance,
            "thickness_tolerance_mm": args.thickness_tolerance,
            "hole_nesting": config.allow_hole_nesting,
            "thermal_radius_mm": config.thermal_radius,
            "thermal_density_limit": config.thermal_density_limit,
            "common_line_policy": config.common_line_policy,
        },
        "thickness_groups": global_groups,
    }

    if not args.dry_run:
        global_path = output_dir / f"{args.prefix}_global_summary.json"
        global_path.write_text(
            json.dumps(global_report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n全局报告：{global_path}")

    print(
        f"\n全部完成：已放置 {total_placed}/{total_requested} 件，"
        f"使用 {total_sheets} 张板材，总利用率 "
        f"{global_report['summary']['total_utilization_percent']:.3f}%。"
    )
    return 0 if all_success else 1


def main() -> int:
    try:
        return run(parse_args())
    except KeyboardInterrupt:
        print("\n用户中止。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"程序运行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
