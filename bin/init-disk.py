#!/usr/bin/env python3
"""Select a persistent disk without tying its name to the container seed version."""
import os
from pathlib import Path
import shutil
import sys
import tempfile


def validate_disk(path: Path) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size == 0:
        raise ValueError(f"{path} must be a non-empty regular file (not a symlink).")


def initialize_disk(data_dir: Path, seed: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / "chr.vdi"
    if target.exists() or target.is_symlink():
        validate_disk(target)
        return target
    legacy = sorted(data_dir.glob("chr-*.vdi"))
    if len(legacy) > 1:
        raise ValueError(
            "Multiple legacy disks found: " + ", ".join(p.name for p in legacy)
            + ". Stop the router, back up data, and copy the chosen disk to chr.vdi; "
            "see docs/upgrades.md. No disk was selected."
        )
    source_image = legacy[0] if legacy else seed
    validate_disk(source_image)
    # Publish only a complete copy, without replacing an existing destination.
    fd, temporary = tempfile.mkstemp(prefix=".chr-init-", dir=data_dir)
    try:
        with os.fdopen(fd, "wb") as destination, source_image.open("rb") as source:
            shutil.copyfileobj(source, destination)
            destination.flush()
            os.fsync(destination.fileno())
        os.link(temporary, target)
    finally:
        os.unlink(temporary)
    return target


def main() -> int:
    if len(sys.argv) != 3:
        print("Usage: init-disk.py DATA_DIR SEED_IMAGE", file=sys.stderr)
        return 1
    try:
        print(initialize_disk(Path(sys.argv[1]), Path(sys.argv[2])))
        return 0
    except (OSError, ValueError) as error:
        print(f"ERROR: disk initialization failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
