#!/usr/bin/env python3
"""
Explicit sync helper for conditional rule state.

Default remote root is /opt/FantraxAPI on host fantrax-vps.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


DEFAULT_REMOTE_HOST = os.getenv("CONDITIONAL_STATE_REMOTE_HOST", "fantrax-vps")
DEFAULT_REMOTE_ROOT = os.getenv("CONDITIONAL_STATE_REMOTE_ROOT", "/opt/FantraxAPI")


def _run(cmd: list[str]) -> None:
    print(">", " ".join(cmd))
    subprocess.run(cmd, check=True)


def _sync_dir(*, src: str, dst: str, delete: bool) -> None:
    cmd = ["rsync", "-az"]
    if delete:
        cmd.append("--delete")
    cmd.extend([src, dst])
    _run(cmd)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync conditional rule state between local and VPS.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pull-from-vps", action="store_true", help="Sync VPS -> local.")
    mode.add_argument("--push-to-vps", action="store_true", help="Sync local -> VPS.")
    parser.add_argument("--remote-host", default=DEFAULT_REMOTE_HOST, help="SSH host alias.")
    parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT, help="Remote repo root.")
    parser.add_argument("--delete", action="store_true", help="Delete extraneous files on destination.")
    args = parser.parse_args()

    local_root = Path(__file__).resolve().parents[1]
    local_rules = str(local_root / "data/conditional_rules/")  # trailing slash for rsync dir semantics
    local_journal = str(local_root / "data/conditional_rules_journal/")
    remote_rules = f"{args.remote_host}:{args.remote_root}/data/conditional_rules/"
    remote_journal = f"{args.remote_host}:{args.remote_root}/data/conditional_rules_journal/"

    Path(local_rules).mkdir(parents=True, exist_ok=True)
    Path(local_journal).mkdir(parents=True, exist_ok=True)

    if args.pull_from_vps:
        _sync_dir(src=remote_rules, dst=local_rules, delete=args.delete)
        _sync_dir(src=remote_journal, dst=local_journal, delete=args.delete)
        return

    _sync_dir(src=local_rules, dst=remote_rules, delete=args.delete)
    _sync_dir(src=local_journal, dst=remote_journal, delete=args.delete)


if __name__ == "__main__":
    main()
