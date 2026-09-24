#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批量读取 STEP/STP 板材零件，判断厚度并按厚度分类到不同文件夹。

核心判断方法：
1. 读取 STEP 的 B-Rep 模型；
2. 找面积较大的平行、法向相反平面（板材的两个宽面）；
3. 以两个宽面的垂直距离作为板厚，宽面法向即厚度方向。

该实现不依赖零件坐标系，因此零件即使倾斜、旋转也能正确识别。
"""

from __future__ import annotations

import argparse
import csv
import filecmp
import math
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Any, Iterable, Sequence

# When output is redirected through a pipe, use UTF-8 to avoid mojibake.
# A real console keeps its native encoding, which is what Windows expects.
def configure_console_stream(stream: Any) -> None:
    if stream is None:
        return
    isatty = getattr(stream, "isatty", None)
    if callable(isatty) and not isatty() and hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")


configure_console_stream(sys.stdout)
configure_console_stream(sys.stderr)

try:
    from nesting.occ import (
        face_area_center,
        face_is_plane,
        face_normal,
        import_step_shape,
        iter_faces,
        vector_between,
    )
except ImportError:  # pragma: no cover - friendly dependency message
    if sys.stderr is not None:
        print(
            "OpenCascade/OCP is required. Install dependencies with:\n"
            "  python -m pip install -r requirements.txt",
            file=sys.stderr,
        )
    raise SystemExit(2)


STEP_SUFFIXES = {".step", ".stp"}


@dataclass(frozen=True)
class FaceInfo:
    """参与厚度判断的平面信息。"""

    index: int
    area: float
    normal: Any
    center: Any


@dataclass(frozen=True)
class ThicknessResult:
    """单个 STEP 的厚度测量结果。"""

    thickness_mm: float
    face_area_a: float
    face_area_b: float
    area_ratio: float
    confidence: str
    candidate_dominance: float | None
    planar_face_count: int


@dataclass
class ClassificationRecord:
    """用于日志、报告和复制阶段的记录。"""

    source: Path
    relative_source: str
    thickness: ThicknessResult | None = None
    error: str = ""
    group_value: float | None = None
    destination: Path | None = None
    copy_status: str = ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "批量读取一个文件夹中的 STEP/STP 板材零件，自动判断板厚，"
            "并按厚度复制/移动到不同子文件夹。"
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "input_dir",
        nargs="?",
        type=Path,
        help="STEP 文件夹；不填写时在 Windows 上打开文件夹选择窗口",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        help="输出目录；默认在输入目录旁边创建“<输入目录名>_按厚度分类”",
    )
    parser.add_argument(
        "-t",
        "--tolerance",
        type=float,
        default=0.05,
        help="同一厚度组的最大误差（mm）",
    )
    parser.add_argument(
        "--round-to",
        type=float,
        default=0.05,
        help="分类文件夹标签的取整步长（mm）；设为 0 表示不取整",
    )
    parser.add_argument(
        "--angle-tolerance",
        type=float,
        default=0.5,
        help="认定两个平面平行的最大夹角误差（度）",
    )
    parser.add_argument(
        "--min-thickness",
        type=float,
        default=0.05,
        help="小于该值的平行面距离不视为板厚（mm）",
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="只扫描输入目录第一层，不扫描子目录",
    )
    parser.add_argument(
        "--move",
        action="store_true",
        help="移动文件而不是复制（谨慎使用）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只分析和打印结果，不创建文件夹、不复制文件、不写报告",
    )
    parser.add_argument(
        "--report",
        type=Path,
        help="CSV 报告路径；默认是输出目录中的“分类报告.csv”",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="不逐件打印结果，只显示摘要",
    )
    return parser.parse_args()


def choose_input_directory() -> Path | None:
    """无参数运行时，在 Windows 上弹出目录选择窗口。"""

    if not sys.platform.startswith("win"):
        return None

    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(title="选择包含 STEP/STP 文件的文件夹")
        root.destroy()
        return Path(selected) if selected else None
    except Exception:
        return None


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def discover_step_files(input_dir: Path, recursive: bool, output_dir: Path) -> list[Path]:
    """查找 STEP/STP 文件，并排除输出目录及其子目录。"""

    iterator: Iterable[Path]
    if recursive:
        iterator = input_dir.rglob("*")
    else:
        iterator = input_dir.glob("*")

    files: list[Path] = []
    for path in iterator:
        try:
            if not path.is_file() or path.suffix.lower() not in STEP_SUFFIXES:
                continue

            resolved = path.resolve()
            if is_relative_to(resolved, output_dir):
                continue
            files.append(resolved)
        except (OSError, ValueError):
            continue

    return sorted(files, key=lambda item: str(item).casefold())


def import_single_shape(step_path: Path) -> Any:
    """Read STEP and combine multiple roots into a compound shape."""

    return import_step_shape(step_path)


def collect_planar_faces(shape: Any) -> list[FaceInfo]:
    planar_faces: list[FaceInfo] = []

    for index, face in enumerate(iter_faces(shape)):
        try:
            if not face_is_plane(face):
                continue
            area, center = face_area_center(face)
            if not math.isfinite(area) or area <= 0.0:
                continue
            planar_faces.append(
                FaceInfo(
                    index=index,
                    area=area,
                    normal=face_normal(face),
                    center=center,
                )
            )
        except Exception:
            continue

    return planar_faces


def confidence_label(area_ratio: float) -> str:
    if area_ratio >= 0.90:
        return "高"
    if area_ratio >= 0.65:
        return "中"
    return "低"


def measure_plate_thickness(
    shape: Any,
    min_thickness_mm: float,
    angle_tolerance_deg: float,
) -> ThicknessResult:
    """利用两个宽大、平行且法向相反的平面判断板厚。"""

    if min_thickness_mm <= 0.0:
        raise ValueError("--min-thickness 必须大于 0")
    if not 0.0 < angle_tolerance_deg < 90.0:
        raise ValueError("--angle-tolerance 必须在 0 到 90 度之间")

    faces = collect_planar_faces(shape)
    if len(faces) < 2:
        raise ValueError("没有找到足够的平面，无法判断板厚")

    opposite_dot_limit = -math.cos(math.radians(angle_tolerance_deg))
    candidates: list[tuple[float, float, float, float, float]] = []

    # 候选评分优先选择面积大且面积相近的一对平面。
    # 对普通板材，两个宽面的面积通常相同；对带缺口、孔和局部凸台的板材，
    # 面积近似但可能略有差异，因此同时保留面积比作为置信度。
    for left_index, left in enumerate(faces):
        for right in faces[left_index + 1 :]:
            normal_alignment = float(left.normal.Dot(right.normal))
            if normal_alignment > opposite_dot_limit:
                continue

            delta = vector_between(left.center, right.center)
            distance = abs(float(delta.Dot(left.normal)))
            if distance < min_thickness_mm or not math.isfinite(distance):
                continue

            min_area = min(left.area, right.area)
            max_area = max(left.area, right.area)
            area_ratio = min_area / max_area
            score = min_area * area_ratio
            candidates.append((score, distance, left.area, right.area, area_ratio))

    if not candidates:
        raise ValueError("没有找到符合厚度特征的平行宽面")

    candidates.sort(key=lambda item: item[0], reverse=True)
    best_score, thickness, area_a, area_b, area_ratio = candidates[0]
    runner_up_score = candidates[1][0] if len(candidates) > 1 else None
    dominance = (
        best_score / runner_up_score
        if runner_up_score is not None and runner_up_score > 0.0
        else None
    )

    return ThicknessResult(
        thickness_mm=thickness,
        face_area_a=area_a,
        face_area_b=area_b,
        area_ratio=area_ratio,
        confidence=confidence_label(area_ratio),
        candidate_dominance=dominance,
        planar_face_count=len(faces),
    )


def build_thickness_groups(
    records: Sequence[ClassificationRecord], tolerance: float
) -> list[tuple[float, list[ClassificationRecord]]]:
    """按厚度聚类，返回 (代表厚度, 记录列表)。"""

    if tolerance < 0.0:
        raise ValueError("--tolerance 不能小于 0")

    valid = [record for record in records if record.thickness is not None]
    valid.sort(key=lambda record: record.thickness.thickness_mm)  # type: ignore[union-attr]

    clusters: list[list[ClassificationRecord]] = []
    for record in valid:
        value = record.thickness.thickness_mm  # type: ignore[union-attr]
        if not clusters:
            clusters.append([record])
            continue

        best_cluster: list[ClassificationRecord] | None = None
        best_distance = math.inf
        for cluster in clusters:
            cluster_value = median(
                item.thickness.thickness_mm  # type: ignore[union-attr]
                for item in cluster
            )
            distance = abs(value - cluster_value)
            if distance < best_distance:
                best_distance = distance
                best_cluster = cluster

        if best_cluster is not None and best_distance <= tolerance:
            best_cluster.append(record)
        else:
            clusters.append([record])

    result: list[tuple[float, list[ClassificationRecord]]] = []
    for cluster in clusters:
        representative = median(
            item.thickness.thickness_mm  # type: ignore[union-attr]
            for item in cluster
        )
        result.append((representative, cluster))
    result.sort(key=lambda item: item[0])
    return result


def rounded_group_value(value: float, round_to: float) -> float:
    if round_to <= 0.0:
        return value
    return round(value / round_to) * round_to


def compact_number(value: float, decimals: int = 3) -> str:
    text = f"{value:.{decimals}f}".rstrip("0").rstrip(".")
    return text if text and text != "-0" else "0"


def unique_destination(destination_dir: Path, source: Path) -> tuple[Path, str]:
    """返回不覆盖不同文件的复制目标路径。"""

    candidate = destination_dir / source.name
    if not candidate.exists():
        return candidate, "待复制"

    try:
        if candidate.is_file() and filecmp.cmp(candidate, source, shallow=False):
            return candidate, "已存在"
    except OSError:
        pass

    for index in range(2, 10000):
        candidate = destination_dir / f"{source.stem}__{index}{source.suffix}"
        if not candidate.exists():
            return candidate, "待复制"
        try:
            if candidate.is_file() and filecmp.cmp(candidate, source, shallow=False):
                return candidate, "已存在"
        except OSError:
            continue

    raise RuntimeError(f"无法为 {source.name} 找到可用的目标文件名")


def copy_or_move(source: Path, destination: Path, move: bool) -> str:
    if move:
        if source.resolve() != destination.resolve():
            shutil.move(str(source), str(destination))
        return "已移动"
    shutil.copy2(source, destination)
    return "已复制"


def write_report(report_path: Path, records: Sequence[ClassificationRecord]) -> None:
    report_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "序号",
        "源文件",
        "相对源路径",
        "分类",
        "厚度_mm",
        "原始厚度_mm",
        "面积比",
        "置信度",
        "最佳候选优势",
        "两宽面面积_mm2",
        "平面数量",
        "状态",
        "错误",
        "目标文件",
    ]

    with report_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for index, record in enumerate(records, 1):
            thickness = record.thickness
            error = record.error.replace("\r", " ").replace("\n", " ")
            writer.writerow(
                {
                    "序号": index,
                    "源文件": record.source.name,
                    "相对源路径": record.relative_source,
                    "分类": (
                        f"厚度_{compact_number(record.group_value)}mm"
                        if record.group_value is not None
                        else "未识别"
                    ),
                    "厚度_mm": (
                        f"{record.group_value:.6f}"
                        if record.group_value is not None
                        else ""
                    ),
                    "原始厚度_mm": (
                        f"{thickness.thickness_mm:.6f}" if thickness else ""
                    ),
                    "面积比": f"{thickness.area_ratio:.6f}" if thickness else "",
                    "置信度": thickness.confidence if thickness else "",
                    "最佳候选优势": (
                        f"{thickness.candidate_dominance:.6f}"
                        if thickness and thickness.candidate_dominance is not None
                        else ""
                    ),
                    "两宽面面积_mm2": (
                        f"{thickness.face_area_a:.6f}|{thickness.face_area_b:.6f}"
                        if thickness
                        else ""
                    ),
                    "平面数量": thickness.planar_face_count if thickness else "",
                    "状态": record.copy_status,
                    "错误": error,
                    "目标文件": str(record.destination) if record.destination else "",
                }
            )


def run(args: argparse.Namespace) -> int:
    input_dir = args.input_dir or choose_input_directory()
    if input_dir is None:
        print(
            "未指定输入目录。用法示例：\n"
            '  python step_thickness_classifier.py "D:\\零件STEP目录"',
            file=sys.stderr,
        )
        return 2

    input_dir = input_dir.expanduser().resolve()
    if not input_dir.is_dir():
        print(f"输入目录不存在或不是文件夹：{input_dir}", file=sys.stderr)
        return 2

    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else input_dir.parent / f"{input_dir.name}_按厚度分类"
    )
    if output_dir == input_dir or is_relative_to(input_dir, output_dir):
        print("输出目录不能是输入目录或其父目录。", file=sys.stderr)
        return 2

    step_files = discover_step_files(input_dir, not args.no_recursive, output_dir)
    if not step_files:
        print(f"没有找到 STEP/STP 文件：{input_dir}", file=sys.stderr)
        return 1

    records: list[ClassificationRecord] = []
    print(f"输入目录：{input_dir}")
    print(f"找到 {len(step_files)} 个 STEP/STP 文件。")
    if not args.dry_run:
        print(f"输出目录：{output_dir}")
    else:
        print("模式：试运行（不会写入任何文件）")

    for index, source in enumerate(step_files, 1):
        try:
            relative_source = str(source.relative_to(input_dir))
        except ValueError:
            relative_source = str(source)

        record = ClassificationRecord(source=source, relative_source=relative_source)
        try:
            shape = import_single_shape(source)
            record.thickness = measure_plate_thickness(
                shape,
                min_thickness_mm=args.min_thickness,
                angle_tolerance_deg=args.angle_tolerance,
            )
            if not args.quiet:
                result = record.thickness
                print(
                    f"[{index:>{len(str(len(step_files)))}}/{len(step_files)}] "
                    f"{source.name} -> {result.thickness_mm:.4f} mm "
                    f"(面积比 {result.area_ratio:.3f}, 置信度 {result.confidence})"
                )
        except Exception as exc:
            record.error = f"{type(exc).__name__}: {exc}"
            if not args.quiet:
                print(
                    f"[{index:>{len(str(len(step_files)))}}/{len(step_files)}] "
                    f"{source.name} -> 未识别：{record.error}"
                )

        records.append(record)

    groups = build_thickness_groups(records, args.tolerance)
    for representative, group in groups:
        group_value = rounded_group_value(representative, args.round_to)
        for record in group:
            record.group_value = group_value
            record.copy_status = "已分类"

    for record in records:
        if record.thickness is None:
            record.copy_status = "未识别"

    if args.dry_run:
        print("\n分类结果：")
        for representative, group in groups:
            label = rounded_group_value(representative, args.round_to)
            print(f"  厚度 {compact_number(label)} mm：{len(group)} 件")
        unidentified = sum(record.thickness is None for record in records)
        if unidentified:
            print(f"  未识别：{unidentified} 件")
        print("\n试运行完成，未复制或移动文件。")
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    for representative, group in groups:
        group_value = rounded_group_value(representative, args.round_to)
        destination_dir = output_dir / f"厚度_{compact_number(group_value)}mm"
        destination_dir.mkdir(parents=True, exist_ok=True)

        for record in group:
            destination, status = unique_destination(destination_dir, record.source)
            if status == "待复制":
                record.copy_status = copy_or_move(record.source, destination, args.move)
            else:
                record.copy_status = status
            record.destination = destination

    unidentified_records = [record for record in records if record.thickness is None]
    if unidentified_records:
        destination_dir = output_dir / "未识别"
        destination_dir.mkdir(parents=True, exist_ok=True)
        for record in unidentified_records:
            destination, status = unique_destination(destination_dir, record.source)
            if status == "待复制":
                record.copy_status = copy_or_move(record.source, destination, args.move)
            else:
                record.copy_status = status
            record.destination = destination

    report_path = (
        args.report.expanduser().resolve()
        if args.report
        else output_dir / "分类报告.csv"
    )
    write_report(report_path, records)

    print("\n分类完成：")
    total_classified = 0
    for representative, group in groups:
        label = compact_number(rounded_group_value(representative, args.round_to))
        total_classified += len(group)
        print(f"  厚度 {label} mm：{len(group)} 件")
    if unidentified_records:
        print(f"  未识别：{len(unidentified_records)} 件")
    print(f"报告：{report_path}")
    print(f"成功分类 {total_classified}/{len(records)} 件。")
    return 0 if total_classified == len(records) else 1


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
