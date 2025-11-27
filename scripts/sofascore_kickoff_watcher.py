#!/usr/bin/env python3
"""
High-frequency SofaScore confirmed-lineup watcher.

Intended for cron: a coarse trigger (e.g., every 5 minutes) enters a tight
polling loop (default 10s) for fixtures within a pre-kickoff window
(e.g., 80–70 minutes). Uses the same lock as sofascore_lineup_listener to
avoid concurrent runs. Exits quickly when no fixtures are in the window or
when everything in-window is confirmed.
"""
from __future__ import annotations

import argparse
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path

from services.sofascore_lineup_service import SofaScoreLineupService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SofaScore kickoff watcher (high-frequency confirmed polling).")
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
        "--window-minutes",
        type=int,
        default=80,
        help="Upper bound of minutes before kickoff for confirmed listener window",
    )
    parser.add_argument(
        "--min-window-minutes",
        type=int,
        default=70,
        help="Lower bound of minutes before kickoff for confirmed listener window",
    )
    parser.add_argument(
        "--poll-interval-seconds",
        type=int,
        default=10,
        help="Seconds to sleep between polls while window is active",
    )
    parser.add_argument(
        "--max-watch-minutes",
        type=int,
        default=15,
        help="Maximum watch duration per invocation",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional max number of fixtures to inspect per poll (debug/testing)",
    )
    parser.add_argument(
        "--lock-file",
        type=Path,
        default=None,
        help="Optional explicit lock file path; defaults to <output-dir>/.sofascore_lineup.lock",
    )
    return parser.parse_args()


@contextmanager
def _acquire_lock(lock_path: Path):
    """
    Simple filesystem lock to avoid overlapping runs with other scripts.
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
    log = logging.getLogger("sofascore_kickoff_watcher")
    log.info(
        "Config: tournament_id=%s season=%s season_id=%s output_dir=%s window_minutes=%s min_window_minutes=%s poll_interval_seconds=%s max_watch_minutes=%s limit=%s disable_raw=%s",
        args.tournament_id,
        args.season,
        args.season_id,
        args.output_dir,
        args.window_minutes,
        args.min_window_minutes,
        args.poll_interval_seconds,
        args.max_watch_minutes,
        args.limit,
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

            schedule = service.load_upcoming_schedule_from_csv()
            if not schedule:
                log.info("Kickoff watcher: no cached schedule available; exiting")
                return

            stats = service._listen_for_confirmed_from_rows(
                schedule,
                window_minutes=args.window_minutes,
                min_minutes_before=args.min_window_minutes,
                limit=args.limit,
            )
            log.info(
                "Kickoff watcher initial: candidates=%s processed=%s saved=%s skipped_confirmed=%s",
                stats.get("candidate_fixtures", 0),
                stats.get("processed", 0),
                stats.get("saved", 0),
                stats.get("skipped_confirmed", 0),
            )

            start_ts = time.time()
            while True:
                candidates = stats.get("candidate_fixtures", 0)
                skipped = stats.get("skipped_confirmed", 0)

                if candidates == 0 or candidates <= skipped:
                    log.info(
                        "Kickoff watcher complete: candidates=%s skipped_confirmed=%s (all done or window empty)",
                        candidates,
                        skipped,
                    )
                    break

                if time.time() - start_ts > args.max_watch_minutes * 60:
                    log.warning(
                        "Kickoff watcher timeout: max_watch_minutes=%s, last stats=%s",
                        args.max_watch_minutes,
                        stats,
                    )
                    break

                time.sleep(args.poll_interval_seconds)

                stats = service._listen_for_confirmed_from_rows(
                    schedule,
                    window_minutes=args.window_minutes,
                    min_minutes_before=args.min_window_minutes,
                    limit=args.limit,
                )
                log.info(
                    "Kickoff watcher poll: candidates=%s processed=%s saved=%s skipped_confirmed=%s",
                    stats.get("candidate_fixtures", 0),
                    stats.get("processed", 0),
                    stats.get("saved", 0),
                    stats.get("skipped_confirmed", 0),
                )
    except RuntimeError as e:
        log.warning("%s", e)


if __name__ == "__main__":
    main()
