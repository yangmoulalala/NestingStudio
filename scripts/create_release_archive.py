from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path


def build_archive(source_dir: Path, archive_path: Path) -> None:
    source_dir = source_dir.resolve()
    archive_path = archive_path.resolve()
    if not source_dir.is_dir():
        raise SystemExit(f"Source directory not found: {source_dir}")

    archive_path.parent.mkdir(parents=True, exist_ok=True)
    if archive_path.exists():
        archive_path.unlink()

    files = sorted(path for path in source_dir.rglob("*") if path.is_file())
    if not files:
        raise SystemExit(f"Source directory is empty: {source_dir}")

    with zipfile.ZipFile(
        archive_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
        allowZip64=True,
    ) as archive:
        for path in files:
            archive.write(path, path.relative_to(source_dir).as_posix())


def main() -> int:
    parser = argparse.ArgumentParser(description="Create the NestingStudio release ZIP.")
    parser.add_argument("source_dir", type=Path)
    parser.add_argument("archive_path", type=Path)
    args = parser.parse_args()
    build_archive(args.source_dir, args.archive_path)
    size = args.archive_path.stat().st_size
    print(f"Created {args.archive_path} ({size / (1024 * 1024):.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())