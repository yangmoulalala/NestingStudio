# Build and Release

## Local Windows build

Run:

```powershell
.\scripts\build_windows.ps1
```

The script performs these steps:

1. Installs runtime and PyInstaller dependencies.
2. Runs PyInstaller using `NestingStudio.spec`.
3. Starts the packaged application with `--smoke-test`.
4. Creates `dist/NestingStudio-1.0.3-windows-x64.zip` and its SHA-256 checksum.

Expected package:

```text
dist/
├─ NestingStudio/
│  └─ NestingStudio.exe
├─ NestingStudio-1.0.3-windows-x64.zip
└─ NestingStudio-1.0.3-windows-x64.zip.sha256
```

## CI

`.github/workflows/ci.yml` runs on Windows and performs:

- Ruff static checks.
- Full unit-test discovery with Qt offscreen mode.

## GitHub release

Create and push a version tag:

```powershell
git tag v1.0.3
git push origin v1.0.3
```

`.github/workflows/release.yml` then:

- Builds the Windows package.
- Runs packaged smoke testing.
- Uploads the ZIP artifact.
- Creates or updates the GitHub Release with generated release notes.

## Package size

The executable includes Python, PySide6 and OpenCascade/OCP. The one-folder distribution is intentionally used instead of one-file mode because it starts faster and avoids repeatedly extracting large CAD libraries.
