# Changelog

## 1.0.1 - 2026-09-24

### Added

- Detailed parsing errors, per-file traceback logging and a release diagnostic report command.

## 1.0.0 - 2026-09-24

### Added

- Thickness-aware STEP/DXF/SVG/JSON nesting.
- Independent nesting and DXF export for every thickness group.
- Clearance, margin, kerf and lead-in constraints.
- Hole nesting, thermal-density constraints and validation.
- Greedy, simulated annealing and genetic algorithm optimizers.
- PySide6 visualization with sheet navigation and layer controls.
- Manual move, rotate, flip, clone, copy, paste and delete.
- Automatic multi-rotation attempts in Greedy mode.
- Undo and redo for geometry edits.
- Save/open projects and import of `nesting_global_summary.json`.
- Custom material presets and last-session restoration.
- Part-name search, resizable columns and name-column autofit.
- Rotating diagnostic logs.
- Windows CI and GitHub Release workflows.

### Changed

- Use OpenCascade/OCP directly at runtime to reduce the Windows package size; CadQuery remains a development-only test fixture dependency.
- Hardened the Windows build with checked native commands, a ZIP64-compatible archive writer and archive integrity validation.

### Fixed

- Progress events used by the GUI.
- Stale Qt graphics objects after invalid placement overlays.
- Second and subsequent drag operations on additional sheets.
- Selection action state after switching thickness groups.
- Interactive performance caused by full-board validation after each edit.
- Compact session persistence for projects with many complex parts.
