from __future__ import annotations

import os
import unittest
from unittest.mock import patch
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtTest import QTest
from PySide6.QtGui import QDragEnterEvent
import cadquery as cq
from PySide6.QtWidgets import QApplication

from nesting.inputs import PartRequest, definitions_from_requests
from nesting.models import NestingConfig
from nesting.optimize import OptimizerSettings
from shapely.affinity import translate
from shapely import wkt
from shapely.geometry import Point

from nesting.pipeline import run_grouped_nesting
from nesting_studio.canvas import PlacementState
from nesting_studio.main_window import MainWindow, MovePlacementCommand
from nesting_studio.models import PartEntry
from nesting_studio.part_table import PartTablePanel
from nesting_studio.project_io import import_global_summary
from nesting_studio.workers import NestingWorker


class GuiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_manual_drag_validation_and_undo(self) -> None:
        requests = [
            PartRequest(
                name="plate",
                thickness_mm=3.0,
                polygons=[[[0, 0], [100, 0], [100, 50], [0, 50]]],
            )
        ]
        definitions = definitions_from_requests(requests, 0.5)
        config = NestingConfig(
            sheet_width=200,
            sheet_height=120,
            margin=5,
            part_clearance=3,
            thermal_density_limit=1.0,
        )
        result = run_grouped_nesting(
            definitions,
            config,
            OptimizerSettings(optimizer="greedy"),
        )
        window = MainWindow()
        window.config_panel.sheet_width.setValue(200)
        window.config_panel.sheet_height.setValue(120)
        window.config_panel.margin.setValue(5)
        window.config_panel.clearance.setValue(3)
        window.config_panel.thermal_density.setValue(1.0)
        window.part_panel.set_entries(
            [PartEntry(request=requests[0], definition=definitions[0])]
        )
        window._result = result
        window._populate_sheet_tree()
        placement = result.groups[0].solution.sheets[0].placements[0]
        old_state = PlacementState(
            x=placement.x,
            y=placement.y,
            actual=placement.actual,
            collision=placement.collision,
        )
        new_state = PlacementState(
            x=placement.x + 5,
            y=placement.y + 5,
            actual=translate(placement.actual, xoff=5, yoff=5),
            collision=translate(placement.collision, xoff=5, yoff=5),
        )
        valid, _ = window.canvas._validate_state(placement, new_state)
        self.assertTrue(valid)
        command = MovePlacementCommand(
            window.canvas,
            placement,
            old_state,
            new_state,
        )
        window.undo_stack.push(command)
        self.assertAlmostEqual(placement.actual.bounds[0], new_state.actual.bounds[0])
        window.undo_stack.undo()
        self.assertAlmostEqual(placement.actual.bounds[0], old_state.actual.bounds[0])
        invalid_state = PlacementState(
            x=500,
            y=500,
            actual=translate(placement.actual, xoff=500, yoff=500),
            collision=translate(placement.collision, xoff=500, yoff=500),
        )
        invalid, _ = window.canvas._validate_state(placement, invalid_state)
        self.assertFalse(invalid)
        records = window._serialize_layout()
        window._result = None
        window._restore_layout(records)
        self.assertIsNotNone(window._result)
        restored = window._result.groups[0].solution.sheets[0].placements[0]
        self.assertAlmostEqual(restored.actual.bounds[0], old_state.actual.bounds[0])
        window.close()

    def test_second_thickness_group_supports_model_actions(self) -> None:
        requests = [
            PartRequest(
                name="thick3",
                thickness_mm=3.0,
                polygons=[[[0, 0], [80, 0], [80, 40], [0, 40]]],
            ),
            PartRequest(
                name="thick2",
                thickness_mm=2.0,
                polygons=[[[0, 0], [60, 0], [60, 30], [0, 30]]],
            ),
        ]
        definitions = definitions_from_requests(requests, 0.5)
        result = run_grouped_nesting(
            definitions,
            NestingConfig(
                sheet_width=300,
                sheet_height=200,
                margin=5,
                part_clearance=3,
                thermal_density_limit=1.0,
            ),
            OptimizerSettings(optimizer="greedy"),
        )
        window = MainWindow()
        window.part_panel.set_entries(
            [PartEntry(request=request, definition=definition) for request, definition in zip(requests, definitions)]
        )
        window._result = result
        window._populate_sheet_tree()
        window.show()
        self.app.processEvents()
        second_group = window.sheet_tree.topLevelItem(1)
        second_sheet = second_group.child(0)
        window.sheet_tree.setCurrentItem(second_sheet)
        self.app.processEvents()
        window.canvas.fit_layout()
        self.app.processEvents()
        item = window.canvas._outer_items[0]
        position = window.canvas.mapFromScene(item.mapToScene(item.boundingRect().center()))
        QTest.mouseClick(
            window.canvas.viewport(),
            Qt.MouseButton.LeftButton,
            pos=position,
        )
        self.app.processEvents()
        self.assertTrue(window.action_copy.isEnabled())
        self.assertTrue(window.action_clone.isEnabled())
        self.assertTrue(window.action_delete.isEnabled())
        self.assertTrue(window.action_rotate_cw.isEnabled())
        self.assertTrue(window.action_flip_h.isEnabled())
        selected = window._selected_placement
        angle_before = selected.angle_deg
        window.action_rotate_cw.trigger()
        self.assertNotEqual(selected.angle_deg, angle_before)
        window.action_flip_h.trigger()
        self.assertTrue(selected.mirrored)
        window.action_copy.trigger()
        window.action_paste.trigger()
        self.app.processEvents()
        self.assertEqual(len(window.canvas._sheet.placements), 2)
        self.assertEqual(window.undo_stack.count(), 3)
        window.close()

    def test_model_clipboard_and_free_overlap(self) -> None:
        requests = [
            PartRequest(
                name="plate",
                thickness_mm=3.0,
                polygons=[[[0, 0], [100, 0], [100, 50], [0, 50]]],
            )
        ]
        definitions = definitions_from_requests(requests, 0.5)
        result = run_grouped_nesting(
            definitions,
            NestingConfig(
                sheet_width=300,
                sheet_height=200,
                margin=5,
                part_clearance=3,
                thermal_density_limit=1.0,
            ),
            OptimizerSettings(optimizer="greedy"),
        )
        window = MainWindow()
        window.part_panel.set_entries(
            [PartEntry(request=requests[0], definition=definitions[0])]
        )
        window._result = result
        window._populate_sheet_tree()
        placement = result.groups[0].solution.sheets[0].placements[0]
        window.copy_placement(placement)
        self.assertIn('"nesting_placement"', QApplication.clipboard().text())
        window.paste_selection()
        self.assertEqual(len(window.canvas._sheet.placements), 2)
        self.assertEqual(definitions[0].quantity, 2)
        clone = window.canvas._sheet.placements[-1]
        self.assertTrue(clone.manual_error)
        self.assertEqual(window.undo_stack.count(), 1)
        window.delete_placement(clone)
        self.assertEqual(len(window.canvas._sheet.placements), 1)
        self.assertEqual(definitions[0].quantity, 1)
        window.undo_stack.undo()
        self.assertEqual(len(window.canvas._sheet.placements), 2)
        self.assertEqual(definitions[0].quantity, 2)

        with patch(
            "nesting_studio.main_window.QInputDialog.getInt",
            return_value=(3, True),
        ):
            window.clone_placement(window.canvas._sheet.placements[0])
        self.assertEqual(len(window.canvas._sheet.placements), 5)
        self.assertEqual(definitions[0].quantity, 5)
        prepared = window._prepare_definitions()
        self.assertEqual(sum(item.quantity for item in prepared), 5)
        self.assertEqual(window.undo_stack.count(), 2)
        window.undo_stack.undo()
        self.assertEqual(len(window.canvas._sheet.placements), 2)
        self.assertEqual(definitions[0].quantity, 2)
        window.close()

    def test_real_mouse_drag_moves_placement(self) -> None:
        requests = [
            PartRequest(
                name="plate",
                thickness_mm=3.0,
                polygons=[[[0, 0], [100, 0], [100, 50], [0, 50]]],
            )
        ]
        definitions = definitions_from_requests(requests, 0.5)
        result = run_grouped_nesting(
            definitions,
            NestingConfig(
                sheet_width=300,
                sheet_height=200,
                margin=5,
                part_clearance=3,
                thermal_density_limit=1.0,
            ),
            OptimizerSettings(optimizer="greedy"),
        )
        window = MainWindow()
        window._result = result
        window._populate_sheet_tree()
        window.show()
        self.app.processEvents()
        window.canvas.fit_layout()
        self.app.processEvents()
        placement = result.groups[0].solution.sheets[0].placements[0]
        before_x = placement.x
        item = window.canvas._outer_items[0]
        center = item.boundingRect().center()
        position = window.canvas.mapFromScene(item.mapToScene(center))
        QTest.mousePress(
            window.canvas.viewport(),
            Qt.MouseButton.LeftButton,
            pos=position,
        )
        QTest.mouseMove(
            window.canvas.viewport(),
            position + QPoint(20, 0),
            delay=50,
        )
        QTest.mouseRelease(
            window.canvas.viewport(),
            Qt.MouseButton.LeftButton,
            pos=position + QPoint(20, 0),
            delay=50,
        )
        self.app.processEvents()
        self.assertGreater(placement.x, before_x)
        self.assertIsNone(window.canvas._drag_placement)
        self.assertEqual(window.undo_stack.count(), 1)
        window.undo_stack.undo()
        self.assertAlmostEqual(placement.x, before_x)

        # An invalid drop must roll back and clear the drag state.
        self.app.processEvents()
        item = window.canvas._outer_items[0]
        center = item.boundingRect().center()
        invalid_position = window.canvas.mapFromScene(item.mapToScene(center))
        QTest.mousePress(
            window.canvas.viewport(),
            Qt.MouseButton.LeftButton,
            pos=invalid_position,
        )
        QTest.mouseMove(
            window.canvas.viewport(),
            invalid_position - QPoint(300, 0),
            delay=50,
        )
        QTest.mouseRelease(
            window.canvas.viewport(),
            Qt.MouseButton.LeftButton,
            pos=invalid_position - QPoint(300, 0),
            delay=50,
        )
        self.app.processEvents()
        self.assertNotAlmostEqual(placement.x, before_x)
        self.assertIsNone(window.canvas._drag_placement)
        self.assertTrue(placement.manual_error)
        window.undo_stack.undo()
        self.assertAlmostEqual(placement.x, before_x)
        window.close()

    def test_stale_invalid_overlay_is_handled(self) -> None:
        requests = [
            PartRequest(
                name="plate",
                thickness_mm=3.0,
                polygons=[[[0, 0], [100, 0], [100, 50], [0, 50]]],
            )
        ]
        definitions = definitions_from_requests(requests, 0.5)
        result = run_grouped_nesting(
            definitions,
            NestingConfig(
                sheet_width=300,
                sheet_height=200,
                margin=5,
                part_clearance=3,
                thermal_density_limit=1.0,
            ),
            OptimizerSettings(optimizer="greedy"),
        )
        window = MainWindow()
        window._result = result
        window._populate_sheet_tree()
        placement = result.groups[0].solution.sheets[0].placements[0]
        state = PlacementState(
            x=placement.x,
            y=placement.y,
            actual=placement.actual,
            collision=placement.collision,
        )
        window.canvas._update_invalid_overlay(state)
        window.canvas._scene.clear()
        window.canvas._hide_invalid_overlay()
        self.assertIsNone(window.canvas._drag_invalid_item)
        window.close()

    def test_nesting_progress_signal_order(self) -> None:
        requests = [
            PartRequest(
                name="plate",
                thickness_mm=3.0,
                polygons=[[[0, 0], [100, 0], [100, 50], [0, 50]]],
            )
        ]
        definitions = definitions_from_requests(requests, 0.5)
        worker = NestingWorker(
            definitions,
            NestingConfig(
                sheet_width=300,
                sheet_height=200,
                margin=5,
                part_clearance=3,
                thermal_density_limit=1.0,
            ),
            OptimizerSettings(optimizer="greedy"),
            thickness_tolerance=0.05,
            round_thickness=0.05,
        )
        events = []
        worker.progress.connect(lambda message, fraction: events.append((message, fraction)))
        worker.run()
        self.assertTrue(events)
        self.assertTrue(all(isinstance(message, str) for message, _ in events))
        self.assertTrue(all(isinstance(fraction, float) for _, fraction in events))
        window = MainWindow()
        window.progress.setRange(0, 0)
        window._on_nesting_progress("invalid", 0.5)
        self.assertEqual(window.progress.maximum(), 1000)
        self.assertEqual(window.progress.value(), 500)
        window.close()

    def test_part_table_search_filter(self) -> None:
        requests = [
            PartRequest(
                name="alpha_plate",
                thickness_mm=2.0,
                polygons=[[[0, 0], [20, 0], [20, 10], [0, 10]]],
            ),
            PartRequest(
                name="beta_bracket",
                thickness_mm=3.0,
                polygons=[[[0, 0], [30, 0], [30, 10], [0, 10]]],
            ),
        ]
        definitions = definitions_from_requests(requests, 0.5)
        panel = PartTablePanel()
        panel.set_entries(
            [PartEntry(request=request, definition=definition) for request, definition in zip(requests, definitions)]
        )
        self.assertEqual(panel.table.rowCount(), 2)
        panel.search_edit.setText("alpha")
        self.assertEqual(panel.table.rowCount(), 1)
        self.assertEqual(panel.table.item(0, 0).text(), "alpha_plate")
        panel.resize_name_column()
        self.assertGreaterEqual(panel.table.columnWidth(0), 180)
        panel.search_edit.clear()
        self.assertEqual(panel.table.rowCount(), 2)

    def test_middle_button_pans_view(self) -> None:
        requests = [
            PartRequest(
                name="plate",
                thickness_mm=3.0,
                polygons=[[[0, 0], [100, 0], [100, 50], [0, 50]]],
            )
        ]
        definitions = definitions_from_requests(requests, 0.5)
        result = run_grouped_nesting(
            definitions,
            NestingConfig(
                sheet_width=2000,
                sheet_height=1500,
                margin=5,
                part_clearance=3,
                thermal_density_limit=1.0,
            ),
            OptimizerSettings(optimizer="greedy"),
        )
        window = MainWindow()
        window._result = result
        window._populate_sheet_tree()
        window.show()
        self.app.processEvents()
        window.canvas.scale(3, 3)
        self.app.processEvents()
        before = window.canvas.horizontalScrollBar().value()
        position = window.canvas.viewport().rect().center()
        QTest.mousePress(
            window.canvas.viewport(),
            Qt.MouseButton.MiddleButton,
            pos=position,
        )
        QTest.mouseMove(
            window.canvas.viewport(),
            position + QPoint(-100, 0),
            delay=50,
        )
        QTest.mouseRelease(
            window.canvas.viewport(),
            Qt.MouseButton.MiddleButton,
            pos=position + QPoint(-100, 0),
            delay=50,
        )
        self.app.processEvents()
        self.assertGreater(window.canvas.horizontalScrollBar().value(), before)
        self.assertFalse(window.canvas._panning)
        window.close()

    def test_circle_radius_and_snap_modes(self) -> None:
        circle = list(Point(50, 30).buffer(8, quad_segs=16).exterior.coords)
        requests = [
            PartRequest(
                name="hole_plate",
                thickness_mm=3.0,
                polygons=[
                    [[0, 0], [100, 0], [100, 60], [0, 60]],
                    [[float(x), float(y)] for x, y in circle],
                ],
            )
        ]
        definitions = definitions_from_requests(requests, 0.5)
        config = NestingConfig(
            sheet_width=200,
            sheet_height=120,
            margin=5,
            part_clearance=3,
            thermal_density_limit=1.0,
        )
        result = run_grouped_nesting(
            definitions,
            config,
            OptimizerSettings(optimizer="greedy"),
        )
        window = MainWindow()
        window._result = result
        window._populate_sheet_tree()
        window.canvas.set_measurement_mode("radius")
        window.canvas.set_snap_mode("circle")
        kinds = {point.kind for point in window.canvas._snap_points}
        self.assertIn("circle_center", kinds)
        self.assertIn("endpoint", kinds)
        self.assertIn("midpoint", kinds)
        circle_point = next(
            point for point in window.canvas._snap_points if point.kind == "circle_center"
        )
        nearest = window.canvas._nearest_snap_point(
            circle_point.scene,
            window.canvas.mapFromScene(circle_point.scene),
        )
        self.assertEqual(nearest.kind, "circle_center")
        self.assertAlmostEqual(float(nearest.radius), 8.0, places=4)
        window.canvas._add_radius_measurement(circle_point)
        self.assertGreaterEqual(len(window.canvas._measurement_items), 3)
        window.close()

    def test_rotate_flip_clone_and_restore(self) -> None:
        requests = [
            PartRequest(
                name="bracket",
                path=Path("fake_part.step"),
                thickness_mm=3.0,
                polygons=[
                    [[0, 0], [100, 0], [100, 30], [30, 30], [30, 70], [0, 70]]
                ],
            )
        ]
        definitions = definitions_from_requests(requests, 0.5)
        result = run_grouped_nesting(
            definitions,
            NestingConfig(
                sheet_width=400,
                sheet_height=300,
                margin=5,
                part_clearance=3,
                thermal_density_limit=1.0,
            ),
            OptimizerSettings(optimizer="greedy"),
        )
        window = MainWindow()
        window.part_panel.set_entries(
            [PartEntry(request=requests[0], definition=definitions[0])]
        )
        window._result = result
        window._populate_sheet_tree()
        placement = result.groups[0].solution.sheets[0].placements[0]
        before_width = placement.actual.bounds[2] - placement.actual.bounds[0]
        before_height = placement.actual.bounds[3] - placement.actual.bounds[1]
        window.rotate_placement(placement, -90)
        rotated_width = placement.actual.bounds[2] - placement.actual.bounds[0]
        rotated_height = placement.actual.bounds[3] - placement.actual.bounds[1]
        self.assertAlmostEqual(rotated_width, before_height, places=5)
        self.assertAlmostEqual(rotated_height, before_width, places=5)
        self.assertEqual(placement.angle_deg, 270.0)
        window.undo_stack.undo()
        self.assertAlmostEqual(placement.angle_deg, 0.0)
        self.assertTrue(definitions[0].face_up_locked)
        window.toggle_face_lock(placement)
        self.assertFalse(definitions[0].face_up_locked)
        window.flip_placement(placement, "horizontal")
        self.assertTrue(placement.mirrored)
        window.copy_placement(placement)
        window.paste_selection()
        clone = window.canvas._sheet.placements[-1]
        self.assertTrue(clone.mirrored)
        expected_clone = translate(placement.actual, xoff=5, yoff=5)
        self.assertEqual(wkt.dumps(clone.actual), wkt.dumps(expected_clone))
        records = window._serialize_layout()
        window._result = None
        window._restore_layout(records)
        restored = window._result.groups[0].solution.sheets[-1].placements[-1]
        self.assertTrue(restored.mirrored)
        self.assertEqual(wkt.dumps(restored.actual), wkt.dumps(expected_clone))
        window.close()

    def test_import_global_summary(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as temporary:
            step_path = Path(temporary) / "plate.step"
            cq.exporters.export(cq.Workplane("XY").box(100, 50, 3), str(step_path))
            summary = {"config": {"sheet_width":300.0,"sheet_height":200.0,"margin":5.0,"part_clearance":3.0,"thermal_density_limit":1.0}, "thickness_groups": [{"thickness_mm":3.0,"solution":{"sheets":[{"placements":[{"part_key":"plate","part_name":"plate","source":str(step_path),"x":5.0,"y":5.0,"anchor_x":3.4375,"anchor_y":3.4375,"rotation_deg":0.0,"mirrored":False}]}]}}]}
            requests, config, layout = import_global_summary(summary, Path(temporary))
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].quantity, 1)
            self.assertEqual(config["sheet_width"], 300.0)
            definitions = definitions_from_requests(requests, 0.5)
            window = MainWindow()
            window.part_panel.set_entries([PartEntry(request=requests[0], definition=definitions[0])])
            window.config_panel.from_dict(config)
            window._restore_layout(layout)
            self.assertIsNotNone(window._result)
            self.assertEqual(window._result.placed_count, 1)
            window.close()

    def test_main_window_builds_and_renders_result(self) -> None:
        requests = [
            PartRequest(
                name="plate",
                quantity=2,
                thickness_mm=3.0,
                polygons=[[[0, 0], [100, 0], [100, 50], [0, 50]]],
            )
        ]
        definitions = definitions_from_requests(requests, 0.5)
        config = NestingConfig(
            sheet_width=300,
            sheet_height=200,
            margin=5,
            part_clearance=3,
            thermal_density_limit=1.0,
        )
        result = run_grouped_nesting(
            definitions,
            config,
            OptimizerSettings(optimizer="greedy"),
        )
        window = MainWindow()
        entry = PartEntry(request=requests[0], definition=definitions[0])
        window.part_panel.set_entries([entry])
        self.assertEqual(window.measurement_mode_combo.itemText(1), "\u534a\u5f84")
        self.assertEqual(window.snap_mode_combo.itemText(2), "\u4e2d\u70b9")
        self.assertEqual(window.snap_mode_combo.itemText(3), "\u5706\u5fc3/\u5706\u5b54")
        self.assertNotIn("?", window.config_panel.result_box.title())
        self.assertTrue(
            all(
                "?" not in label.text()
                for label in window.config_panel.result_box.findChildren(
                    __import__("PySide6.QtWidgets", fromlist=["QLabel"]).QLabel
                )
            )
        )
        window.part_panel.table.selectRow(0)
        window.clone_selected_parts()
        self.assertEqual(len(window.part_panel.entries), 2)
        window.copy_selected_parts()
        self.assertIn('"parts"', QApplication.clipboard().text())
        window._result = result
        window._populate_sheet_tree()
        self.assertEqual(window.sheet_tree.topLevelItemCount(), 1)
        self.assertGreater(len(window.canvas.scene().items()), 0)

        # The main window accepts CAD/project files through drag and drop.
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(Path("examples/nesting_manifest.json").resolve()))])
        drag_event = QDragEnterEvent(
            QPoint(1, 1),
            Qt.DropAction.CopyAction,
            mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        window.dragEnterEvent(drag_event)
        self.assertTrue(drag_event.isAccepted())

        # Measurement creates a line and label after two points.
        window.canvas._add_measurement_point(QPointF(10, 10), QPointF(-100, -100))
        window.canvas._add_measurement_point(QPointF(60, 40), QPointF(-100, -100))
        self.assertGreaterEqual(len(window.canvas._measurement_items), 4)

        window.close()


if __name__ == "__main__":
    unittest.main()
