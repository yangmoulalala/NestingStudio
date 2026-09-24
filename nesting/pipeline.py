from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .engine import NestingEngine
from .inputs import ThicknessGroup, group_definitions_by_thickness
from .models import NestingConfig, NestingSolution, PartDefinition
from .optimize import OptimizerSettings, solve_nesting
from .output import validate_solution


ProgressCallback = Callable[[str, float], None]
CancelCallback = Callable[[], bool]


@dataclass
class GroupNestingResult:
    group: ThicknessGroup
    solution: NestingSolution
    validation: dict


@dataclass
class GroupedNestingResult:
    groups: list[GroupNestingResult]
    config: NestingConfig
    settings: OptimizerSettings

    @property
    def requested_count(self) -> int:
        return sum(item.solution.requested_count for item in self.groups)

    @property
    def placed_count(self) -> int:
        return sum(item.solution.placed_count for item in self.groups)

    @property
    def unplaced_count(self) -> int:
        return sum(len(item.solution.unplaced) for item in self.groups)

    @property
    def sheet_count(self) -> int:
        return sum(len(item.solution.sheets) for item in self.groups)

    @property
    def part_area(self) -> float:
        return sum(item.solution.part_area for item in self.groups)

    @property
    def sheet_area(self) -> float:
        return sum(item.solution.sheet_area for item in self.groups)

    @property
    def utilization_percent(self) -> float:
        if self.sheet_area <= 0.0:
            return 0.0
        return 100.0 * self.part_area / self.sheet_area

    @property
    def hole_nested_count(self) -> int:
        return sum(item.solution.hole_nested_count for item in self.groups)

    @property
    def success(self) -> bool:
        return all(
            not item.solution.unplaced and item.validation.get("overlap_count", 0) == 0
            for item in self.groups
        )


def run_grouped_nesting(
    definitions: list[PartDefinition],
    config: NestingConfig,
    settings: OptimizerSettings,
    thickness_tolerance: float = 0.05,
    round_thickness: float = 0.05,
    progress_callback: ProgressCallback | None = None,
    cancel_callback: CancelCallback | None = None,
) -> GroupedNestingResult:
    """Run one independent nesting job for every thickness group."""

    groups = group_definitions_by_thickness(
        definitions,
        tolerance=thickness_tolerance,
        round_to=round_thickness,
    )
    if not groups:
        raise ValueError("没有可排版的厚度组")

    results: list[GroupNestingResult] = []
    for group_index, group in enumerate(groups, 1):
        if cancel_callback is not None and cancel_callback():
            from .models import NestingCancelled

            raise NestingCancelled("用户取消排版")

        label = (
            "未指定厚度"
            if group.value_mm is None
            else f"{group.value_mm:g} mm"
        )

        def group_progress(
            phase: str,
            current: int,
            total: int,
            best_cost: float,
        ) -> None:
            if progress_callback is None:
                return
            within_group = current / max(total, 1)
            overall = ((group_index - 1) + within_group) / len(groups)
            progress_callback(
                f"厚度 {label}：{phase.upper()} {current}/{total}，最优代价 {best_cost:.3f}",
                overall,
            )

        if progress_callback is not None:
            progress_callback(
                f"正在排版厚度组 {group_index}/{len(groups)}：{label}",
                (group_index - 1) / len(groups),
            )

        engine = NestingEngine(group.definitions, config)
        solution = solve_nesting(
            engine,
            settings,
            progress_callback=group_progress,
            cancel_callback=cancel_callback,
        )
        validation = validate_solution(solution, config)
        results.append(GroupNestingResult(group=group, solution=solution, validation=validation))

    if progress_callback is not None:
        progress_callback("所有厚度组排版完成", 1.0)
    return GroupedNestingResult(groups=results, config=config, settings=settings)
