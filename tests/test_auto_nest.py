from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import ezdxf

from nesting.engine import NestingEngine
from nesting.inputs import (
    PartRequest,
    definitions_from_requests,
    group_definitions_by_thickness,
    load_dxf_geometry,
)
from nesting.models import NestingConfig
from nesting.optimize import OptimizerSettings, initial_schedules, solve_nesting
from nesting.output import export_sheet_dxf, validate_solution


class AutoNestTests(unittest.TestCase):
    def _requests(self) -> list[PartRequest]:
        return [
            PartRequest(
                name="frame",
                quantity=1,
                rotations_deg=(0.0, 180.0),
                polygons=[
                    [[0, 0], [300, 0], [300, 200], [0, 200]],
                    [[60, 50], [240, 50], [240, 150], [60, 150]],
                ],
            ),
            PartRequest(
                name="small",
                quantity=4,
                rotations_deg=(0.0, 90.0, 180.0, 270.0),
                polygons=[[[0, 0], [80, 0], [80, 50], [0, 50]]],
            ),
        ]

    def test_hole_nesting_and_clearance(self) -> None:
        definitions = definitions_from_requests(self._requests(), 0.25)
        config = NestingConfig(
            sheet_width=500,
            sheet_height=400,
            margin=10,
            part_clearance=5,
            kerf=2,
            lead_length=5,
            thermal_density_limit=1.0,
        )
        engine = NestingEngine(definitions, config)
        solution = engine.evaluate(initial_schedules(engine)[0])
        validation = validate_solution(solution, config)

        self.assertEqual(solution.placed_count, 5)
        self.assertGreaterEqual(solution.hole_nested_count, 1)
        self.assertGreaterEqual(
            validation["minimum_pair_distance_mm"] + config.overlap_epsilon,
            config.effective_clearance,
        )
        self.assertGreaterEqual(
            validation["minimum_edge_distance_mm"] + config.overlap_epsilon,
            config.effective_edge_margin,
        )
        self.assertEqual(validation["overlap_count"], 0)

    def test_parts_are_grouped_by_geometry_thickness(self) -> None:
        requests = self._requests()
        requests[0].thickness_mm = 3.0
        requests[1].thickness_mm = 2.0
        definitions = definitions_from_requests(requests, 0.25)
        groups = group_definitions_by_thickness(definitions, tolerance=0.05)
        self.assertEqual([group.value_mm for group in groups], [2.0, 3.0])
        self.assertEqual([group.folder_name for group in groups], ["\u539a\u5ea6_2mm", "\u539a\u5ea6_3mm"])
        self.assertEqual(sum(len(group.definitions) for group in groups), 2)

    def test_greedy_tries_allowed_rotations(self) -> None:
        definitions = definitions_from_requests(
            [
                PartRequest(
                    name="long_plate",
                    thickness_mm=3.0,
                    rotations_deg=(0.0, 90.0),
                    polygons=[[[0, 0], [100, 0], [100, 20], [0, 20]]],
                )
            ],
            0.5,
        )
        config = NestingConfig(
            sheet_width=30,
            sheet_height=110,
            margin=1,
            part_clearance=0,
            thermal_density_limit=1.0,
        )
        engine = NestingEngine(definitions, config)
        solution = solve_nesting(
            engine,
            OptimizerSettings(optimizer="greedy"),
        )
        self.assertEqual(solution.placed_count, 1)
        self.assertAlmostEqual(solution.sheets[0].placements[0].angle_deg, 90.0)

    def test_dxf_export_layers(self) -> None:
        definitions = definitions_from_requests(self._requests(), 0.25)
        config = NestingConfig(
            sheet_width=500,
            sheet_height=400,
            margin=10,
            part_clearance=5,
            kerf=2,
            lead_length=5,
            thermal_density_limit=1.0,
        )
        engine = NestingEngine(definitions, config)
        solution = engine.evaluate(initial_schedules(engine)[0])
        with tempfile.TemporaryDirectory() as temporary:
            files = export_sheet_dxf(solution, config, Path(temporary), prefix="test")
            self.assertTrue(files)
            document = ezdxf.readfile(files[0])
            layers = {entity.dxf.layer for entity in document.modelspace()}
            self.assertIn("SHEET_BORDER", layers)
            self.assertIn("PART_OUTER", layers)
            self.assertIn("PART_HOLE", layers)
            self.assertIn("LEAD_IN", layers)

    def test_dxf_contour_with_hole(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "part.dxf"
            document = ezdxf.new("R2010")
            document.units = ezdxf.units.MM
            modelspace = document.modelspace()
            modelspace.add_lwpolyline(
                [(0, 0), (200, 0), (200, 100), (0, 100)], close=True
            )
            modelspace.add_lwpolyline(
                [(80, 40), (120, 40), (120, 60), (80, 60)], close=True
            )
            document.saveas(path)
            geometry = load_dxf_geometry(path, 0.2)
            self.assertAlmostEqual(geometry.area, 19200.0, places=6)
            self.assertEqual(len(list(geometry.interiors)), 1)


if __name__ == "__main__":
    unittest.main()
