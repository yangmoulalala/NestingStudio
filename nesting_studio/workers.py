from __future__ import annotations

import logging
import traceback

from PySide6.QtCore import QThread, Signal

from nesting.inputs import PartRequest
from nesting.models import NestingCancelled, NestingConfig, PartDefinition
from nesting.optimize import OptimizerSettings
from nesting.pipeline import run_grouped_nesting

from .models import PartEntry


logger = logging.getLogger(__name__)


class LoadPartsWorker(QThread):
    progress = Signal(int, int, str)
    finished_successfully = Signal(object)
    failed = Signal(str)

    def __init__(self, requests: list[PartRequest], flatten_tolerance: float) -> None:
        super().__init__()
        self.requests = requests
        self.flatten_tolerance = flatten_tolerance
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def run(self) -> None:
        entries: list[PartEntry] = []
        try:
            total = len(self.requests)
            for index, request in enumerate(self.requests, 1):
                if self._cancelled:
                    return
                self.progress.emit(index - 1, total, f"读取 {request.name}")
                entry = PartEntry(request=request)
                try:
                    geometry, thickness = request.load_geometry_and_thickness(
                        self.flatten_tolerance
                    )
                    definition = PartDefinition(
                        key=request.name,
                        name=request.name,
                        geometry=geometry,
                        quantity=max(1, request.quantity),
                        rotations_deg=request.rotations_deg,
                        source=str(request.path or "polygon-list"),
                        grain_locked=request.grain_locked,
                        priority=request.priority,
                        thickness_mm=thickness,
                        face_up_locked=(
                            request.face_up_locked
                            if request.face_up_locked is not None
                            else entry.is_step
                        ),
                    )
                    entry.definition = definition
                except Exception as exc:
                    entry.error = f"{type(exc).__name__}: {exc}"
                    logger.error(
                        "Failed to load part: name=%s source=%s error=%s",
                        request.name,
                        request.path or "polygon-list",
                        entry.error,
                        exc_info=True,
                    )
                entries.append(entry)
                self.progress.emit(index, total, entry.display_name)
            self.finished_successfully.emit(entries)
        except Exception:
            logger.exception("\u52a0\u8f7d\u96f6\u4ef6\u540e\u53f0\u4efb\u52a1\u5931\u8d25")
            self.failed.emit(traceback.format_exc())


class NestingWorker(QThread):
    progress = Signal(str, float)
    finished_successfully = Signal(object)
    cancelled = Signal()
    failed = Signal(str)

    def __init__(
        self,
        definitions: list[PartDefinition],
        config: NestingConfig,
        settings: OptimizerSettings,
        thickness_tolerance: float,
        round_thickness: float,
    ) -> None:
        super().__init__()
        self.definitions = definitions
        self.config = config
        self.settings = settings
        self.thickness_tolerance = thickness_tolerance
        self.round_thickness = round_thickness
        self._cancelled = False

    def cancel(self) -> None:
        self._cancelled = True

    def _is_cancelled(self) -> bool:
        return self._cancelled

    def run(self) -> None:
        try:
            result = run_grouped_nesting(
                self.definitions,
                self.config,
                self.settings,
                thickness_tolerance=self.thickness_tolerance,
                round_thickness=self.round_thickness,
                progress_callback=self.progress.emit,
                cancel_callback=self._is_cancelled,
            )
            self.finished_successfully.emit(result)
        except NestingCancelled:
            logger.info("\u7528\u6237\u53d6\u6d88\u6392\u7248\u4efb\u52a1")
            self.cancelled.emit()
        except Exception:
            logger.exception("\u6392\u7248\u540e\u53f0\u4efb\u52a1\u5931\u8d25")
            self.failed.emit(traceback.format_exc())
