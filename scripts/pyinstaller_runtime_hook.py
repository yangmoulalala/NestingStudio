"""PyInstaller runtime setup for OpenCascade and its DLL search paths."""

from __future__ import annotations

import os
import sys


def _add_dll_directory(path: str) -> None:
    if not os.path.isdir(path):
        return
    try:
        os.add_dll_directory(path)
    except (AttributeError, FileNotFoundError, OSError):
        pass


base_dir = getattr(sys, "_MEIPASS", "")
if base_dir:
    vtk_dir = os.path.join(base_dir, "vtk.libs")
    os.makedirs(vtk_dir, exist_ok=True)
    _add_dll_directory(base_dir)
    _add_dll_directory(vtk_dir)
