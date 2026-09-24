from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path
from typing import Any

from nesting.inputs import PartRequest, load_requests

from .logging_config import configure_logging
from .project_io import import_global_summary, is_global_summary
from .version import __version__


def _requests_for_input(path: Path) -> list[PartRequest]:
    path = path.expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Input path does not exist: {path}")
    if path.is_file() and path.suffix.lower() == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if is_global_summary(data):
            requests, _, _ = import_global_summary(data, path.parent)
            return requests
    return load_requests(
        [path],
        manifest=None,
        default_quantity=1,
        default_rotations=(0.0, 90.0, 180.0, 270.0),
    )


def run_diagnostics(inputs: list[Path], tolerance: float) -> dict[str, Any]:
    requests: list[PartRequest] = []
    for path in inputs:
        requests.extend(_requests_for_input(path))

    records: list[dict[str, Any]] = []
    for request in requests:
        started = time.perf_counter()
        record: dict[str, Any] = {
            "name": request.name,
            "source": str(request.path or "polygon-list"),
            "quantity": request.quantity,
        }
        try:
            geometry, thickness = request.load_geometry_and_thickness(tolerance)
            record.update(
                {
                    "status": "ok",
                    "thickness_mm": thickness,
                    "area_mm2": float(geometry.area),
                    "bounds": [float(value) for value in geometry.bounds],
                    "hole_count": len(geometry.interiors),
                    "valid": bool(geometry.is_valid),
                }
            )
        except Exception as exc:
            record.update(
                {
                    "status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "traceback": traceback.format_exc(),
                }
            )
        record["elapsed_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
        records.append(record)

    succeeded = sum(record["status"] == "ok" for record in records)
    return {
        "version": __version__,
        "tolerance_mm": tolerance,
        "input_count": len(inputs),
        "part_count": len(records),
        "succeeded": succeeded,
        "failed": len(records) - succeeded,
        "parts": records,
    }


def diagnostic_main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Parse NestingStudio inputs and write a JSON diagnostic report."
    )
    parser.add_argument("--diagnose", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--tolerance", type=float, default=0.5)
    args = parser.parse_args(argv)

    configure_logging()
    report_path = args.report.expanduser().resolve()
    try:
        report = run_diagnostics(args.inputs, args.tolerance)
    except Exception:
        report = {
            "version": __version__,
            "tolerance_mm": args.tolerance,
            "status": "error",
            "traceback": traceback.format_exc(),
            "parts": [],
        }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return 0 if report.get("failed", 1) == 0 else 2


if __name__ == "__main__":
    sys.exit(diagnostic_main())
