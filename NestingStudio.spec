# -*- mode: python ; coding: utf-8 -*-

from importlib.util import find_spec
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

datas = []
binaries = []
hiddenimports = []

package_datas, package_binaries, package_hiddenimports = collect_all("OCP")
datas += package_datas
binaries += package_binaries
hiddenimports += package_hiddenimports

datas += collect_data_files("ezdxf")
hiddenimports += collect_submodules("nesting")
hiddenimports += collect_submodules("nesting_studio")
datas += [("nesting_studio/assets", "nesting_studio/assets")]

# OpenCascade's Python bindings expect the wheel's vtk.libs directory to exist
# and add it to the Windows DLL search path when OCP is imported.
vtk_spec = find_spec("vtkmodules")
if vtk_spec is not None and vtk_spec.origin:
    vtk_libs = Path(vtk_spec.origin).resolve().parent.parent / "vtk.libs"
    if vtk_libs.is_dir():
        datas += [
            (str(path), "vtk.libs")
            for path in vtk_libs.iterdir()
            if path.is_file()
        ]

a = Analysis(
    ["NestingStudio.pyw"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["scripts/pyinstaller_runtime_hook.py"],
    excludes=[
        "PyQt5",
        "PyQt6",
        "PySide2",
        "qtpy",
        "cadquery",
        "vtkmodules",
        "trame",
        "IPython",
        "jupyter",
        "jupyterlab",
        "notebook",
        "pandas",
        "matplotlib",
        "scipy",
        "pytest",
        "PIL",
        "sphinx",
        "numba",
        "llvmlite",
        "sklearn",
        "bokeh",
        "distributed",
        "h5py",
        "xarray",
        "statsmodels",
        "pyarrow",
        "sqlalchemy",
        "botocore",
        "openpyxl",
        "tkinter",
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="NestingStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="NestingStudio",
)
