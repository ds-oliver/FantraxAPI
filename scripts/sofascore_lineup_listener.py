#!/usr/bin/env python3
"""
High-level SofaScore lineup automation entrypoint.

This script wraps services/sofascore_lineup_service.py to support two flows:
    1) Predicted sweep: capture unconfirmed lineups days in advance.
    2) Confirmed listener: poll fixtures within ~75-90 minutes of kickoff.
    (For high-frequency 10s polling in a hot window, use scripts/sofascore_kickoff_watcher.py.)
Limitations / assumptions:
    - SofaScore kickoff times are treated as UTC; keep host clock NTP-synced.
    - Cron granularity approximates the desired window (e.g., */5 yields ~±5m around 70–80m).
    - If ESD and raw HTTP both fail during a run, that run may miss the window; frequent runs mitigate.
    - --with-mappings is heavier; prefer running after predictions, not every 5 minutes on match days.
    - Browser path must point to a Chrome/Chromium binary that works headless under the cron user.

Suggested cron usage (adjust paths and Python env as needed):
    - Daily predictions sweep: 0 3 * * * /path/to/venv/bin/python /path/to/script.py --mode predictions --horizon-days 7
    - Match-day confirmed listener (every 5 minutes, 70-80m before kickoff):
        */5 * * * * /path/to/venv/bin/python /path/to/script.py --mode confirmed --window-minutes 80 --min-window-minutes 70

Note: Set --browser-path for the target server's Chrome/Chromium binary; the default path here is macOS-specific.
"""
from __future__ import annotations

import argparse
import logging
import os
from contextlib import contextmanager
from pathlib import Path

from services.sofascore_lineup_service import SofaScoreLineupService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SofaScore lineup exporter (v3).")
    parser.add_argument("--tournament-id", type=int, default=17, help="SofaScore tournament id (default Premier League)")
    parser.add_argument("--season", type=str, default=None, help="Optional season label (e.g. '2024/2025')")
    parser.add_argument("--season-id", type=int, default=None, help="Explicit season id overrides --season")
    parser.add_argument(
        "--browser-path",
        type=str,
        default="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        help="Chrome path for ESD if it needs to drive the browser (set for your server)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/sofascore"),
        help="Base directory for schedules and lineups",
    )
    parser.add_argument("--disable-raw", action="store_true", help="Disable raw HTTP fallbacks")
    parser.add_argument(
        "--mode",
        choices=["predictions", "confirmed", "both", "next-round"],
        default="both",
        help="Which worker(s) to run",
    )
    parser.add_argument(
        "--horizon-days",
        type=int,
        default=7,
        help="Predictions: only capture matches within this many days",
    )
    parser.add_argument(
        "--window-minutes",
        type=int,
        default=90,
        help="Confirmed listener: poll matches kicking off within this window",
    )
    parser.add_argument(
        "--min-window-minutes",
        type=int,
        default=None,
        help="Optional lower bound of minutes before kickoff for confirmed listener",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional max number of fixtures to inspect per mode (debug/testing)",
    )
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=None,
        help="Optional explicit lock file path; defaults to <output-dir>/.sofascore_lineup.lock",
    )
    parser.add_argument(
        "--with-mappings",
        "--with_mappings",  # underscore alias for convenience
        action="store_true",
        help="After fetching lineups, sync SofaScore players with Fantrax mappings",
    )
    parser.add_argument(
        "--fantrax-players-csv",
        type=Path,
        default=Path("players.csv"),
        help="Fantrax players export used for automatic mapping",
    )
    parser.add_argument(
        "--mapping-file",
        type=Path,
        default=Path("config/player_mappings.yaml"),
        help="Player mapping YAML file to update",
    )
    return parser.parse_args()


@contextmanager
def _acquire_lock(lock_path: Path):
    """
    Simple filesystem lock to avoid overlapping cron runs.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock_file = lock_path.open("x")
    except FileExistsError:
        raise RuntimeError(f"Another sofascore lineup run is in progress (lock: {lock_path})")
    try:
        lock_file.write(str(os.getpid()))
        lock_file.flush()
        yield
    finally:
        try:
            lock_file.close()
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass


def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    log = logging.getLogger("sofascore_lineup_v3")
    log.info(
        "Config: mode=%s tournament_id=%s season=%s season_id=%s output_dir=%s horizon_days=%s window_minutes=%s min_window_minutes=%s limit=%s with_mappings=%s disable_raw=%s",
        args.mode,
        args.tournament_id,
        args.season,
        args.season_id,
        args.output_dir,
        args.horizon_days,
        args.window_minutes,
        args.min_window_minutes,
        args.limit,
        args.with_mappings,
        args.disable_raw,
    )

    lock_path = args.lock_file or (Path(args.output_dir) / ".sofascore_lineup.lock")
    try:
        with _acquire_lock(lock_path):
            service = SofaScoreLineupService(
                tournament_id=args.tournament_id,
                season_text=args.season,
                season_id=args.season_id,
                browser_path=args.browser_path,
                output_dir=args.output_dir,
                disable_raw=args.disable_raw,
            )

            if args.mode in ("predictions", "both"):
                stats = service.refresh_predictions(horizon_days=args.horizon_days, limit=args.limit)
                log.info("Predictions sweep: processed=%s saved=%s", stats["processed"], stats["saved"])

            if args.mode in ("confirmed", "both"):
                stats = service.listen_for_confirmed(
                    window_minutes=args.window_minutes,
                    min_minutes_before=args.min_window_minutes,
                    limit=args.limit,
                )
                processed = stats.get("processed", 0)
                saved = stats.get("saved", 0)
                skipped = stats.get("skipped_confirmed", 0)
                candidates = stats.get("candidate_fixtures", 0)
                in_window = stats.get("in_window", candidates)
                log.info(
                    "Confirmed listener: processed=%s saved=%s skipped_confirmed=%s candidates=%s in_window=%s",
                    processed,
                    saved,
                    skipped,
                    candidates,
                    in_window,
                )

            if args.mode == "next-round":
                stats = service.refresh_next_round_predictions()
                log.info(
                    "Next round sweep: round=%s matchups=%s processed=%s saved=%s",
                    stats.get("round"),
                    stats.get("matchups"),
                    stats.get("processed"),
                    stats.get("saved"),
                )

            if args.with_mappings:
                from services.player_mapping_sync import PlayerMappingSync

                syncer = PlayerMappingSync(
                    lineups_dir=args.output_dir / "lineups",
                    players_csv=args.fantrax_players_csv,
                    mapping_file=args.mapping_file,
                )
                try:
                    stats = syncer.sync()
                    log.info(
                        "Mapping sync: auto_added=%s unmatched=%s report=%s",
                        stats["auto_added"],
                        stats["unmatched"],
                        stats["report"],
                    )
                except Exception:
                    log.exception("Mapping sync failed")
    except RuntimeError as e:
        log.warning("%s", e)
        return


if __name__ == "__main__":
    main()
