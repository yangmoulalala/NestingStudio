from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


_LOG_PATH: Path | None = None


def configure_logging(log_dir: Path | None = None) -> Path:
    global _LOG_PATH
    if _LOG_PATH is not None:
        return _LOG_PATH

    if log_dir is None:
        if getattr(sys, "frozen", False):
            base_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "NestingStudio"
        else:
            base_dir = Path(__file__).resolve().parents[1]
        log_dir = base_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "nesting_studio.log"

    level_name = os.environ.get("NESTING_STUDIO_LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        if getattr(handler, "_nesting_studio_handler", False):
            root.removeHandler(handler)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler._nesting_studio_handler = True  # type: ignore[attr-defined]
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler._nesting_studio_handler = True  # type: ignore[attr-defined]
    stream_handler.setLevel(level)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    logging.captureWarnings(True)
    _LOG_PATH = log_path
    logging.getLogger(__name__).info("日志系统已启动：%s", log_path)
    return log_path


def get_log_path() -> Path:
    return _LOG_PATH or configure_logging()
