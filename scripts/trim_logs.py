#!/usr/bin/env python3
"""
Trim or rotate log files so they do not grow unbounded.

Intended for cron (e.g. daily or weekly). For each configured log path:
- If the file is larger than --max-size-mb, keep only the last --keep-lines lines.
- Optionally rotate: rename current to .1, write trimmed content to the base name.

Usage:
  python scripts/trim_logs.py
  python scripts/trim_logs.py --max-size-mb 10 --keep-lines 50000
  python scripts/trim_logs.py --dry-run

Schedule with cron (e.g. weekly Sunday 3am):
  0 3 * * 0 cd /path/to/FantraxAPI && python scripts/trim_logs.py
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _repo_root() -> Path:
    root = Path(__file__).resolve().parent.parent
    return root


def _default_log_dirs(root: Path) -> list[Path]:
    return [
        root / "logs",
        root / "data" / "logs",
    ]


def trim_file(
    path: Path,
    *,
    max_bytes: int,
    keep_lines: int,
    dry_run: bool = False,
) -> bool:
    """If path is larger than max_bytes, replace with last keep_lines lines. Returns True if trimmed."""
    if not path.is_file():
        return False
    size = path.stat().st_size
    if size <= max_bytes:
        return False

    if dry_run:
        print(f"[dry-run] would trim {path} (size {size}) to last {keep_lines} lines")
        return True

    import subprocess
    tmp = path.with_suffix(path.suffix + ".trim_tmp")
    try:
        with open(tmp, "wb") as out:
            subprocess.run(
                ["tail", "-n", str(keep_lines), str(path)],
                check=True,
                stdout=out,
            )
        tmp.replace(path)
        print(f"Trimmed {path}: kept last {keep_lines} lines")
        return True
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def main() -> int:
    root = _repo_root()
    parser = argparse.ArgumentParser(description="Trim log files to limit disk usage.")
    parser.add_argument(
        "--max-size-mb",
        type=float,
        default=5.0,
        help="Trim files larger than this many MB (default 5)",
    )
    parser.add_argument(
        "--keep-lines",
        type=int,
        default=50000,
        help="When trimming, keep this many last lines (default 50000)",
    )
    parser.add_argument(
        "--dirs",
        type=Path,
        nargs="*",
        default=None,
        help="Log directories to scan (default: repo logs/ and data/logs/)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only print what would be trimmed",
    )
    args = parser.parse_args()

    max_bytes = int(args.max_size_mb * 1024 * 1024)
    dirs = args.dirs if args.dirs is not None else _default_log_dirs(root)

    trimmed = 0
    for d in dirs:
        if not d.is_dir():
            continue
        for path in sorted(d.iterdir()):
            if path.suffix in (".log", ".out") or path.name.endswith(".log"):
                if trim_file(path, max_bytes=max_bytes, keep_lines=args.keep_lines, dry_run=args.dry_run):
                    trimmed += 1

    if args.dry_run and trimmed:
        print(f"[dry-run] would trim {trimmed} file(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
