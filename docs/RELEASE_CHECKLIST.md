# Release Checklist

## Before commit

- Confirm `pyproject.toml`, `nesting_studio/version.py`, `CITATION.cff` and `CHANGELOG.md` use the same version.
- Confirm `CITATION.cff` points to the intended GitHub repository.
- Run `ruff check nesting nesting_studio tests auto_nest.py --output-format concise`.
- Run `$env:QT_QPA_PLATFORM="offscreen"; python -m unittest discover -s tests -v`.
- Build with `scripts\build_windows.ps1`.
- Confirm `dist\NestingStudio\NestingStudio.exe` starts normally.
- Confirm `dist\NestingStudio-1.0.3-windows-x64.zip` exists and can be extracted.
- Verify the package does not contain logs, projects, customer CAD files, local paths or credentials.
- Review `README.md`, `CHANGELOG.md` and `THIRD_PARTY_NOTICES.md`.

## GitHub

- Create the `yangmoulalala/NestingStudio` repository.
- Push the `main` branch.
- Confirm the Windows CI workflow passes.
- Create and push the release tag `v1.0.3`.
- Confirm the Release workflow builds and uploads the ZIP.
- Download the release asset once and run its smoke test on a clean machine when possible.
- Add a short release description with known limitations and upgrade notes.

## Known release limitations

- Exact No-Fit Polygon placement is approximated by expanded boundaries and contact candidates.
- Common-line cutting currently exposes an interface rather than a production CAM postprocessor.
- Imported global reports require the original STEP/DXF/SVG source files unless a project file is available.
- Automatic face recognition for blind features is conservative; STEP parts remain face-up locked by default.
