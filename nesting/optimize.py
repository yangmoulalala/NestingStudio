from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from typing import Iterable

from .engine import NestingEngine
from .geometry import rotate_and_normalize
from .models import NestingCancelled, NestingSolution, PartInstance


Schedule = list[tuple[int, float]]


@dataclass
class OptimizerSettings:
    optimizer: str = "sa"
    iterations: int = 80
    population_size: int = 12
    generations: int = 20
    seed: int = 2026
    time_limit_seconds: float = 60.0


def _bounding_box_score(
    engine: NestingEngine,
    instance_index: int,
    angle: float,
) -> tuple[float, float, float]:
    geometry = rotate_and_normalize(
        engine.instances[instance_index].definition.geometry,
        angle,
    )
    min_x, min_y, max_x, max_y = geometry.bounds
    width = max_x - min_x
    height = max_y - min_y
    ratio = max(width / max(height, 1.0e-9), height / max(width, 1.0e-9))
    return float(width * height), float(ratio), abs(float(angle))


def _preferred_angle(engine: NestingEngine, instance: PartInstance) -> float:
    return min(
        instance.definition.rotations_deg,
        key=lambda angle: _bounding_box_score(engine, instance.index, angle),
    )


def _schedule_from_order(
    engine: NestingEngine,
    order: Iterable[int],
    random_source: random.Random | None = None,
) -> Schedule:
    schedule: Schedule = []
    for instance_index in order:
        instance = engine.instances[instance_index]
        if random_source is None:
            angle = _preferred_angle(engine, instance)
        else:
            angle = random_source.choice(instance.definition.rotations_deg)
        schedule.append((instance_index, angle))
    return schedule


def initial_schedules(engine: NestingEngine) -> list[Schedule]:
    instances = engine.instances
    by_area = sorted(
        instances,
        key=lambda item: (
            -item.definition.priority,
            -item.definition.area,
            item.index,
        ),
    )
    by_longest_side = sorted(
        instances,
        key=lambda item: (
            -item.definition.priority,
            -max(item.definition.bounds[2] - item.definition.bounds[0], item.definition.bounds[3] - item.definition.bounds[1]),
            -item.definition.area,
        ),
    )
    by_complexity = sorted(
        instances,
        key=lambda item: (
            -item.definition.priority,
            -max(
                len(item.definition.geometry.exterior.coords)
                if hasattr(item.definition.geometry, 'exterior')
                else sum(len(poly.exterior.coords) for poly in getattr(item.definition.geometry, 'geoms', [])),
                0,
            ),
            -item.definition.area,
        ),
    )
    orders = [
        [item.index for item in by_area],
        [item.index for item in by_longest_side],
        [item.index for item in by_complexity],
    ]
    return [_schedule_from_order(engine, order) for order in orders]


def mutate_schedule(
    schedule: Schedule,
    engine: NestingEngine,
    random_source: random.Random,
) -> Schedule:
    mutated = list(schedule)
    length = len(mutated)
    if length < 2:
        return mutated

    operation = random_source.random()
    if operation < 0.55:
        left, right = sorted(random_source.sample(range(length), 2))
        mutated[left], mutated[right] = mutated[right], mutated[left]
    elif operation < 0.78:
        left, right = sorted(random_source.sample(range(length), 2))
        fragment = mutated[left : right + 1]
        del mutated[left : right + 1]
        insert_at = random_source.randrange(len(mutated) + 1)
        mutated[insert_at:insert_at] = fragment
    else:
        index = random_source.randrange(length)
        instance_index, _ = mutated[index]
        allowed = engine.instances[instance_index].definition.rotations_deg
        choices = [angle for angle in allowed if abs(angle - mutated[index][1]) > 1.0e-8]
        if choices:
            mutated[index] = (instance_index, random_source.choice(choices))
    return mutated


def order_crossover(
    first: Schedule,
    second: Schedule,
    random_source: random.Random,
) -> Schedule:
    length = len(first)
    if length < 2:
        return list(first)
    left, right = sorted(random_source.sample(range(length), 2))
    child: list[tuple[int, float] | None] = [None] * length
    child[left : right + 1] = first[left : right + 1]
    used = {gene[0] for gene in child if gene is not None}
    fill_values = [gene for gene in second if gene[0] not in used]
    fill_index = 0
    for index in list(range(right + 1, length)) + list(range(0, left)):
        if fill_index < len(fill_values):
            child[index] = fill_values[fill_index]
            fill_index += 1
    return [gene for gene in child if gene is not None]


def solve_nesting(
    engine: NestingEngine,
    settings: OptimizerSettings,
    progress_callback=None,
    cancel_callback=None,
) -> NestingSolution:
    optimizer = settings.optimizer.lower().strip()
    if optimizer not in {"greedy", "sa", "ga"}:
        raise ValueError("--optimizer 只支持 greedy、sa 或 ga")

    start_evaluations = engine._evaluations
    start_time = time.monotonic()
    random_source = random.Random(settings.seed)
    schedules = initial_schedules(engine)
    if optimizer == "greedy":
        schedules = schedules[:1]
    elif optimizer == "sa":
        schedules = schedules[:1]
    evaluated: list[tuple[float, Schedule, NestingSolution]] = []

    def check_cancelled() -> None:
        if cancel_callback is not None and cancel_callback():
            raise NestingCancelled("\u7528\u6237\u53d6\u6d88\u6392\u7248")

    def evaluate(schedule: Schedule) -> tuple[float, NestingSolution]:
        check_cancelled()
        placement_progress = None
        if progress_callback is not None:
            def report_placement(current: int, total: int) -> None:
                progress_callback("placement", current, total, 0.0)

            placement_progress = report_placement
        solution = engine.evaluate(
            schedule,
            cancel_callback=cancel_callback,
            progress_callback=placement_progress,
            try_all_rotations=(optimizer == "greedy"),
        )
        return solution.cost, solution

    for schedule in schedules:
        cost, solution = evaluate(schedule)
        evaluated.append((cost, schedule, solution))

    evaluated.sort(key=lambda item: item[0])
    best_cost, best_schedule, best_solution = evaluated[0]
    history: list[dict[str, float | int | str]] = [
        {"phase": "initial", "evaluation": 0, "cost": best_cost}
    ]

    if optimizer == "greedy":
        best_solution.optimizer = "greedy"
        best_solution.evaluations = engine._evaluations - start_evaluations
        best_solution.history = history
        return best_solution

    if optimizer == "sa":
        current_cost = best_cost
        current_schedule = best_schedule
        initial_temperature = max(1.0e7, best_cost * 1.0e-4)
        temperature = initial_temperature
        maximum = max(1, settings.iterations)
        for iteration in range(maximum):
            check_cancelled()
            if time.monotonic() - start_time >= settings.time_limit_seconds:
                break
            candidate_schedule = mutate_schedule(current_schedule, engine, random_source)
            candidate_cost, candidate_solution = evaluate(candidate_schedule)
            delta = candidate_cost - current_cost
            accepted = delta <= 0.0
            if not accepted:
                accepted = random_source.random() < math.exp(
                    -delta / max(temperature, 1.0e-12)
                )
            if accepted:
                current_cost = candidate_cost
                current_schedule = candidate_schedule
            if candidate_cost < best_cost:
                best_cost = candidate_cost
                best_schedule = candidate_schedule
                best_solution = candidate_solution
                history.append(
                    {"phase": "sa", "evaluation": iteration + 1, "cost": best_cost}
                )
            temperature = initial_temperature * (1.0 - (iteration + 1) / maximum) ** 2
            if progress_callback is not None and (iteration + 1) % 10 == 0:
                progress_callback("sa", iteration + 1, maximum, best_cost)
    else:
        population_size = max(4, settings.population_size)
        population: list[Schedule] = [item[1] for item in evaluated[:population_size]]
        while len(population) < population_size:
            base_order = [item.index for item in engine.instances]
            random_source.shuffle(base_order)
            population.append(_schedule_from_order(engine, base_order, random_source))

        scores: dict[tuple, float] = {}
        for generation in range(max(1, settings.generations)):
            check_cancelled()
            if time.monotonic() - start_time >= settings.time_limit_seconds:
                break
            ranked: list[tuple[float, Schedule]] = []
            for schedule in population:
                key = tuple(schedule)
                if key not in scores:
                    scores[key], _ = evaluate(schedule)
                ranked.append((scores[key], schedule))
            ranked.sort(key=lambda item: item[0])
            population = [item[1] for item in ranked]
            generation_cost, generation_schedule = ranked[0]
            if generation_cost < best_cost:
                best_cost = generation_cost
                best_schedule = generation_schedule
                best_solution = engine.evaluate(generation_schedule)
                history.append(
                    {
                        "phase": "ga",
                        "evaluation": generation + 1,
                        "cost": best_cost,
                    }
                )

            elites = population[: max(2, population_size // 6)]
            new_population = list(elites)
            while len(new_population) < population_size:
                def tournament() -> Schedule:
                    choices = random_source.sample(population, min(3, len(population)))
                    return min(choices, key=lambda item: scores.get(tuple(item), math.inf))

                parent_a = tournament()
                parent_b = tournament()
                child = order_crossover(parent_a, parent_b, random_source)
                if random_source.random() < 0.35:
                    child = mutate_schedule(child, engine, random_source)
                new_population.append(child)
            population = new_population
            if progress_callback is not None:
                progress_callback("ga", generation + 1, max(1, settings.generations), best_cost)

    best_solution.optimizer = optimizer
    best_solution.schedule = best_schedule
    best_solution.evaluations = engine._evaluations - start_evaluations
    best_solution.history = history
    return best_solution
