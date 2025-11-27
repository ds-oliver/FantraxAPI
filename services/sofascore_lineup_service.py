"""
SofaScore lineup service.

Provides reusable helpers to fetch schedules/lineups via EasySoccerData (ESD)
with raw-HTTP fallbacks plus two automation flows:
    - refresh_predictions(): sweep future fixtures to capture predicted lineups
    - listen_for_confirmed(): poll fixtures within 75-90 minutes of kickoff
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import esd
import httpx

log = logging.getLogger(__name__)

API_BASE = "https://api.sofascore.com/api/v1"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.sofascore.com",
    "Referer": "https://www.sofascore.com/",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Connection": "keep-alive",
}


# --------------------------- helpers ---------------------------
def getv(obj: Any, *names: str, default=None):
    """Return first matching attribute or dict key."""
    for n in names:
        if isinstance(obj, dict) and n in obj:
            return obj[n]
        if hasattr(obj, n):
            return getattr(obj, n)
    return default


def is_mapping(x) -> bool:
    return isinstance(x, dict)


def raw_get_json(url: str, referer: Optional[str] = None) -> dict:
    headers = HEADERS.copy()
    if referer:
        headers["Referer"] = referer
    with httpx.Client(follow_redirects=True, headers=headers, timeout=30) as s:
        time.sleep(0.3 + random.random() * 0.3)
        r = s.get(url, params={"_": int(datetime.now().timestamp() * 1000)})
        r.raise_for_status()
        return r.json()


def raw_get_seasons(tournament_id: int) -> list[dict]:
    url = f"{API_BASE}/unique-tournament/{tournament_id}/seasons"
    data = raw_get_json(url)
    return data.get("seasons") or []


def raw_iter_tournament_events(
    tournament_id: int, season_id: int, upcoming: bool
) -> Iterable[dict]:
    page = 0
    path = "next" if upcoming else "last"
    while True:
        url = (
            f"{API_BASE}/unique-tournament/{tournament_id}/season/{season_id}/events/{path}/{page}"
        )
        data = raw_get_json(url)
        events = data.get("events") or []
        if not events:
            break
        for ev in events:
            yield ev
        page += 1


def raw_get_lineups(event_id: int) -> dict:
    url = f"{API_BASE}/event/{event_id}/lineups"
    referer = f"https://www.sofascore.com/football/match/-/-/#id:{event_id}"
    return raw_get_json(url, referer=referer)


def safe_team_name(team_obj) -> str:
    if not team_obj:
        return "?"
    if is_mapping(team_obj):
        return (
            team_obj.get("name")
            or team_obj.get("shortName")
            or team_obj.get("short_name")
            or team_obj.get("slug")
            or "?"
        )
    return (
        getattr(team_obj, "name", None)
        or getattr(team_obj, "short_name", None)
        or getattr(team_obj, "slug", None)
        or "?"
    )


def safe_team_id(team_obj) -> Optional[int]:
    if not team_obj:
        return None
    if is_mapping(team_obj):
        tid = team_obj.get("id")
    else:
        tid = getattr(team_obj, "id", None)
    return int(tid) if tid is not None else None


def fmt_ts(ts) -> str:
    if ts is None:
        return ""
    try:
        dt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M:%S%z")
    except Exception:
        return ""


def parse_kickoff_dt(kickoff_str: str | None) -> Optional[datetime]:
    if not kickoff_str:
        return None
    try:
        # Stored as "YYYY-mm-dd HH:MM:SS+0000"
        sanitized = kickoff_str.replace("+0000", "+00:00")
        return datetime.fromisoformat(sanitized)
    except Exception:
        return None


def event_to_row(ev, tournament_id: int, season_id: int) -> dict:
    start_ts = getv(ev, "startTimestamp", "start_timestamp", "startTime")
    home = getv(ev, "homeTeam", "home_team", "home")
    away = getv(ev, "awayTeam", "away_team", "away")
    round_info = getv(ev, "roundInfo", "round_info", default={}) or {}
    status = getv(ev, "status", default={}) or {}

    round_val = getv(round_info, "round", default="")
    status_code = getv(status, "code", default="")

    return {
        "event_id": int(getv(ev, "id")),
        "kickoff_utc": fmt_ts(start_ts),
        "home_team": safe_team_name(home),
        "away_team": safe_team_name(away),
        "home_team_id": safe_team_id(home) or "",
        "away_team_id": safe_team_id(away) or "",
        "tournament_id": tournament_id,
        "season_id": season_id,
        "round": round_val,
        "status_code": status_code,
    }


def lineup_to_json(event_id: int, lu: Any) -> dict:
    """Normalize ESD lineups OR raw dict into uniform JSON."""

    def pack_from_esd_player(pl):
        info = getattr(pl, "info", None) or {}
        return {
            "id": getattr(info, "id", None),
            "name": getattr(info, "name", None),
            "position": getattr(info, "position", None),
            "team_id": getattr(pl, "team_id", None),
            "substitute": bool(getattr(pl, "substitute", False)),
            "captain": bool(getattr(pl, "captain", False)),
        }

    def pack_from_raw_player(d):
        info = d.get("player", {}) if isinstance(d, dict) else {}
        return {
            "id": info.get("id"),
            "name": info.get("name"),
            "position": info.get("position"),
            "team_id": d.get("teamId"),
            "substitute": bool(d.get("substitute", False)),
            "captain": bool(d.get("captain", False)),
        }

    def pack_from_esd_missing(mp):
        p = getattr(mp, "player", None) or {}
        return {
            "id": getattr(p, "id", None),
            "name": getattr(p, "name", None),
            "position": getattr(p, "position", None),
            "reason": getattr(mp, "reason", None),
        }

    def pack_from_raw_missing(d):
        p = d.get("player", {}) if isinstance(d, dict) else {}
        return {
            "id": p.get("id"),
            "name": p.get("name"),
            "position": p.get("position"),
            "reason": d.get("reason"),
        }

    def pack_side_esd(side):
        players = list(getattr(side, "players", []) or [])
        starters = [pack_from_esd_player(p) for p in players if not getattr(p, "substitute", False)]
        subs = [pack_from_esd_player(p) for p in players if getattr(p, "substitute", False)]
        missing = [
            pack_from_esd_missing(m) for m in (getattr(side, "missing_players", []) or [])
        ]
        return {"formation": getattr(side, "formation", None), "starters": starters, "subs": subs, "missing": missing}

    def pack_side_raw(side: dict):
        players = side.get("players") or []
        starters = [pack_from_raw_player(p) for p in players if not p.get("substitute", False)]
        subs = [pack_from_raw_player(p) for p in players if p.get("substitute", False)]
        missing = [pack_from_raw_missing(m) for m in (side.get("missingPlayers") or [])]
        return {"formation": side.get("formation"), "starters": starters, "subs": subs, "missing": missing}

    if isinstance(lu, dict):
        home = lu.get("home") or {}
        away = lu.get("away") or {}
        return {
            "event_id": event_id,
            "confirmed": bool(lu.get("confirmed", False)),
            "home": pack_side_raw(home),
            "away": pack_side_raw(away),
        }
    else:
        return {
            "event_id": event_id,
            "confirmed": bool(getv(lu, "confirmed", default=False)),
            "home": pack_side_esd(getv(lu, "home")),
            "away": pack_side_esd(getv(lu, "away")),
        }


def write_csv(rows: list[dict], path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = (
        list(rows[0].keys())
        if rows
        else [
            "event_id",
            "kickoff_utc",
            "home_team",
            "away_team",
            "home_team_id",
            "away_team_id",
            "tournament_id",
            "season_id",
            "round",
            "status_code",
        ]
    )
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def append_lineups_index(index_path: Path, items: list[dict]):
    index_path.parent.mkdir(parents=True, exist_ok=True)
    exists = index_path.exists()
    fieldnames = ["event_id", "confirmed", "home_starters", "away_starters", "saved_at_utc", "source"]
    with index_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        if not exists:
            w.writeheader()
        for it in items:
            w.writerow(it)


def choose_season_from_list(seasons: list[Any], season_text: Optional[str]) -> int:
    if season_text:
        target = season_text.strip().lower()
        for s in seasons:
            if str(getv(s, "year", "name", default="")).strip().lower() == target:
                return int(getv(s, "id"))
        for s in seasons:
            if target in str(getv(s, "year", "name", default="")).strip().lower():
                return int(getv(s, "id"))
        raise RuntimeError(
            f"Season '{season_text}' not found. Options: {[str(getv(x, 'year', 'name', default='')) for x in seasons]}"
        )
    current = [s for s in seasons if bool(getv(s, "current", default=False))]
    if current:
        return int(getv(current[0], "id"))
    seasons_sorted = sorted(seasons, key=lambda x: int(getv(x, "id")), reverse=True)
    return int(getv(seasons_sorted[0], "id"))


def event_is_finished(ev) -> bool:
    status = getv(ev, "status", default={}) or {}
    return int(getv(status, "code", default=0)) == 100


class SofaScoreLineupService:
    """High-level helper for schedule/lineup automation."""

    def __init__(
        self,
        *,
        tournament_id: int = 17,
        season_text: Optional[str] = None,
        season_id: Optional[int] = None,
        browser_path: str | None = None,
        output_dir: Path = Path("data/sofascore"),
        disable_raw: bool = False,
    ) -> None:
        self.client = esd.SofascoreClient(browser_path=browser_path)
        self.tournament_id = tournament_id
        self.season_text = season_text
        self.explicit_season_id = season_id
        self.disable_raw = disable_raw
        self._season_id_cache: Optional[int] = None
        self.output_dir = output_dir
        self.schedules_dir = self.output_dir / "schedules"
        self.lineups_dir = self.output_dir / "lineups"
        self.index_path = self.output_dir / "lineups_index.csv"
        self.schedules_dir.mkdir(parents=True, exist_ok=True)
        self.lineups_dir.mkdir(parents=True, exist_ok=True)

    # ----- season + events -------------------------------------------------
    def season_id(self) -> int:
        if self.explicit_season_id:
            return int(self.explicit_season_id)
        if self._season_id_cache:
            return self._season_id_cache

        try:
            seasons_esd = self.client.get_tournament_seasons(self.tournament_id)
        except Exception as e:
            log.warning("ESD get_tournament_seasons failed: %s", e)
            seasons_esd = []

        if seasons_esd:
            sid = choose_season_from_list(seasons_esd, self.season_text)
            log.info("[season] picked season_id=%s from ESD", sid)
            self._season_id_cache = sid
            return sid

        if self.disable_raw:
            raise RuntimeError("ESD did not return seasons and raw fallback disabled.")

        seasons_raw = raw_get_seasons(self.tournament_id)
        if not seasons_raw:
            raise RuntimeError("Unable to resolve seasons via ESD or raw HTTP.")
        sid = choose_season_from_list(seasons_raw, self.season_text)
        log.info("[season] picked season_id=%s from RAW", sid)
        self._season_id_cache = sid
        return sid

    def iter_events(self, *, upcoming: bool) -> Iterable[Any]:
        season_id = self.season_id()
        try:
            page = 0
            seen = set()
            total = 0
            while True:
                batch = self.client.get_tournament_events(
                    self.tournament_id, season_id, upcoming=upcoming, page=page
                )
                if not batch:
                    break
                for ev in batch:
                    ev_id = int(getv(ev, "id"))
                    if ev_id in seen:
                        continue
                    seen.add(ev_id)
                    total += 1
                    yield ev
                page += 1
            log.info("[events] ESD returned %s events (mode=%s)", total, "upcoming" if upcoming else "last")
            return
        except Exception as e:
            log.warning("[events] ESD get_tournament_events failed: %s", e)

        if self.disable_raw:
            log.warning("[events] RAW fallback disabled; yielding nothing.")
            return
        for ev in raw_iter_tournament_events(self.tournament_id, season_id, upcoming):
            yield ev

    def fetch_schedule(self, *, upcoming: bool, finished_only: bool = False) -> list[dict]:
        rows = []
        for ev in self.iter_events(upcoming=upcoming):
            if finished_only and not event_is_finished(ev):
                continue
            rows.append(event_to_row(ev, self.tournament_id, self.season_id()))
        mode = "upcoming" if upcoming else "last"
        schedule_csv = self.schedules_dir / f"{self.tournament_id}_{self.season_id()}_{mode}.csv"
        write_csv(rows, schedule_csv)
        log.info("[schedule] wrote %s rows -> %s", len(rows), schedule_csv)
        return rows

    def load_upcoming_schedule_from_csv(self) -> list[dict]:
        """
        Load the cached upcoming schedule from disk.
        Assumes a periodic job keeps the CSV fresh.
        """
        season_id = self.season_id()
        path = self.schedules_dir / f"{self.tournament_id}_{season_id}_upcoming.csv"
        if not path.exists():
            log.warning("[schedule] cached upcoming schedule %s not found on disk", path)
            return []
        rows: list[dict] = []
        try:
            with path.open("r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    rows.append(row)
            log.info("[schedule] loaded %s rows from cached %s", len(rows), path)
        except Exception as e:
            log.warning("[schedule] failed to load cached %s: %s", path, e)
        return rows

    # ----- lineup orchestration -------------------------------------------
    def refresh_predictions(self, *, horizon_days: int = 7, limit: Optional[int] = None) -> dict:
        """
        Fetch predicted lineups for fixtures within next horizon_days.
        Stores the normalized JSON even if not confirmed.
        """
        schedule = self.fetch_schedule(upcoming=True)
        now = datetime.now(timezone.utc)
        horizon = now + timedelta(days=horizon_days)
        processed = saved = 0
        for row in sorted(schedule, key=lambda r: r.get("kickoff_utc", "")):
            kickoff_dt = parse_kickoff_dt(row.get("kickoff_utc"))
            if not kickoff_dt:
                continue
            if not (now < kickoff_dt <= horizon):
                continue
            event_id = int(row["event_id"])
            if limit and processed >= limit:
                break
            processed += 1
            lineup = self._fetch_lineup(event_id, allow_unconfirmed=True)
            if lineup:
                self._store_lineup(lineup, source="predicted")
                saved += 1
        return {"mode": "predictions", "processed": processed, "saved": saved}

    def listen_for_confirmed(
        self,
        *,
        window_minutes: int = 90,
        min_minutes_before: int | None = None,
        limit: Optional[int] = None,
    ) -> dict:
        """
        Poll fixtures whose kickoff is within window_minutes and store confirmed lineups.
        If min_minutes_before is provided, only include fixtures at or above that lower bound.
        """
        schedule = self.fetch_schedule(upcoming=True)
        stats = self._listen_for_confirmed_from_rows(
            schedule,
            window_minutes=window_minutes,
            min_minutes_before=min_minutes_before,
            limit=limit,
        )
        log.info(
            "Confirmed listener window: min=%s max=%s in_window=%s candidates=%s processed=%s saved=%s skipped_confirmed=%s",
            min_minutes_before,
            window_minutes,
            stats.get("in_window"),
            stats.get("candidate_fixtures"),
            stats.get("processed"),
            stats.get("saved"),
            stats.get("skipped_confirmed"),
        )
        return stats

    def _listen_for_confirmed_from_rows(
        self,
        rows: list[dict],
        *,
        window_minutes: int,
        min_minutes_before: int | None,
        limit: Optional[int],
    ) -> dict:
        now = datetime.now(timezone.utc)
        processed = saved = skipped_confirmed = candidate_fixtures = in_window = 0
        for row in sorted(rows, key=lambda r: r.get("kickoff_utc", "")):
            kickoff_dt = parse_kickoff_dt(row.get("kickoff_utc"))
            if not kickoff_dt:
                continue
            minutes_before_kickoff = (kickoff_dt - now).total_seconds() / 60
            if minutes_before_kickoff < 0 or minutes_before_kickoff > window_minutes:
                continue
            in_window += 1
            if min_minutes_before is not None and minutes_before_kickoff < min_minutes_before:
                continue
            candidate_fixtures += 1
            event_id = int(row["event_id"])
            if limit and processed >= limit:
                break
            processed += 1
            lineup = self._fetch_lineup(event_id, allow_unconfirmed=False)
            if lineup and lineup.get("confirmed"):
                if self._existing_confirmed_matches(event_id, lineup):
                    skipped_confirmed += 1
                    continue
                self._store_lineup(lineup, source="confirmed")
                saved += 1
        return {
            "mode": "confirmed",
            "processed": processed,
            "saved": saved,
            "skipped_confirmed": skipped_confirmed,
            "in_window": in_window,
            "candidate_fixtures": candidate_fixtures,
        }

    def refresh_next_round_predictions(
        self, *, expected_matches: int = 10
    ) -> dict:
        """
        Detect the next round based on upcoming fixtures and pull lineups only for
        that round. Provides an explicit sanity check on match count.
        """
        schedule = self.fetch_schedule(upcoming=True)
        now = datetime.now(timezone.utc)
        upcoming = []
        for row in schedule:
            kickoff_dt = parse_kickoff_dt(row.get("kickoff_utc"))
            if not kickoff_dt or kickoff_dt <= now:
                continue
            upcoming.append((row, kickoff_dt))

        if not upcoming:
            log.warning("[round] No upcoming fixtures found")
            return {"mode": "next_round", "round": None, "processed": 0, "saved": 0, "matchups": 0}

        upcoming.sort(key=lambda item: item[1])
        next_round = upcoming[0][0].get("round")

        if next_round in (None, ""):
            log.warning("[round] Unable to determine round from schedule")
            return {"mode": "next_round", "round": None, "processed": 0, "saved": 0, "matchups": 0}

        round_rows = [row for row, _ in upcoming if row.get("round") == next_round]
        matchup_count = len(round_rows)
        if matchup_count != expected_matches:
            log.warning(
                "[round] Expected %s matchups for round %s but found %s",
                expected_matches,
                next_round,
                matchup_count,
            )

        processed = saved = 0
        for row in round_rows:
            event_id = int(row["event_id"])
            processed += 1
            lineup = self._fetch_lineup(event_id, allow_unconfirmed=True)
            if lineup:
                self._store_lineup(lineup, source=f"round:{next_round}")
                saved += 1

        return {
            "mode": "next_round",
            "round": next_round,
            "processed": processed,
            "saved": saved,
            "matchups": matchup_count,
        }

    # ----- lineup fetch/store ---------------------------------------------
    def _fetch_lineup(self, event_id: int, *, allow_unconfirmed: bool) -> Optional[dict]:
        def _from_esd() -> Optional[Any]:
            try:
                return self.client.get_match_lineups(event_id)
            except Exception as e:
                log.debug("[lineup] ESD get_match_lineups failed for event=%s: %s", event_id, e)
                return None

        lineup_obj = _from_esd()
        if not lineup_obj and not self.disable_raw:
            try:
                lineup_obj = raw_get_lineups(event_id)
            except Exception as e:
                log.debug("[lineup] RAW lineups failed for event=%s: %s", event_id, e)

        if not lineup_obj:
            log.info("[lineup] no data for event=%s", event_id)
            return None

        data = lineup_to_json(event_id, lineup_obj)
        if not data.get("home", {}).get("starters") and not data.get("away", {}).get("starters"):
            log.info("[lineup] empty starters for event=%s", event_id)
            return None

        if not allow_unconfirmed and not data.get("confirmed"):
            log.info("[lineup] event=%s not confirmed yet; skipping", event_id)
            return None

        data["fetched_at_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S%z")
        return data

    def _store_lineup(self, lineup: dict, *, source: str) -> None:
        event_id = lineup.get("event_id")
        out_path = self.lineups_dir / f"{event_id}.json"
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(lineup, f, ensure_ascii=False, indent=2)
        log.info(
            "[lineup] saved event=%s confirmed=%s -> %s", event_id, lineup.get("confirmed"), out_path
        )
        entry = {
            "event_id": event_id,
            "confirmed": bool(lineup.get("confirmed")),
            "home_starters": len((lineup.get("home") or {}).get("starters") or []),
            "away_starters": len((lineup.get("away") or {}).get("starters") or []),
            "saved_at_utc": lineup.get("fetched_at_utc"),
            "source": source,
        }
        append_lineups_index(self.index_path, [entry])

    def _is_snapshot_confirmed(self, path: Path) -> bool:
        """Return True if snapshot is marked confirmed; on read/parse errors treat as not confirmed so a fresh fetch can overwrite."""
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return bool(data.get("confirmed"))
        except Exception as e:
            log.debug("[lineup] unable to read lineup snapshot %s: %s", path, e)
            return False

    def _existing_confirmed_matches(self, event_id: int, new_lineup: dict) -> bool:
        """
        Return True if an existing confirmed snapshot matches the newly fetched confirmed lineup.
        Ignores fetched_at_utc to avoid needless rewrites.
        """
        path = self.lineups_dir / f"{event_id}.json"
        if not path.exists():
            return False
        try:
            with path.open("r", encoding="utf-8") as f:
                existing = json.load(f)
            if not existing.get("confirmed"):
                return False
            return self._normalize_lineup_for_compare(existing) == self._normalize_lineup_for_compare(new_lineup)
        except Exception as e:
            log.debug("[lineup] unable to compare lineup snapshot %s: %s", path, e)
            return False

    @staticmethod
    def _normalize_lineup_for_compare(data: dict) -> dict:
        return {k: v for k, v in data.items() if k != "fetched_at_utc"}
