"""
Player mapping synchronization helpers.

This module derives the set of SofaScore players from the cached lineups,
compares them against the existing Fantrax mappings, and automatically
adds deterministic matches (same canonical name) using a local Fantrax
player export. Unmatched players are written to a CSV report for manual
follow-up so the pipeline never forgets to run the legacy script.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import requests
from fantraxapi.player_mapping import PlayerMapping, PlayerMappingManager
from rapidfuzz import fuzz
from unidecode import unidecode

log = logging.getLogger(__name__)


def _canon(name: str | None) -> str:
    if not name:
        return ""
    stripped = (
        unidecode(name)
        .replace("-", " ")
        .replace(".", " ")
        .replace("'", "")
        .lower()
    )
    for token in (" iii", " ii", " jr", " sr"):
        stripped = stripped.replace(token, "")
    return " ".join(stripped.split())


@dataclass
class SofaPlayer:
    sofascore_id: int
    name: str
    team_id: Optional[int]
    team_name: Optional[str]
    role: str
    source_file: Path

    @property
    def canon(self) -> str:
        return _canon(self.name)


class PlayerMappingSync:
    """Synchronize SofaScore lineup players with Fantrax mappings."""

    def __init__(
        self,
        *,
        lineups_dir: Path = Path("data/sofascore/lineups"),
        ratings_dir: Path = Path("data/silver/sofascore"),
        players_csv: Path = Path("players.csv"),
        mapping_file: Path = Path("config/player_mappings.yaml"),
        report_dir: Path = Path("data/sofascore/reports"),
        refresh_ratings: bool = True,
        tournament_id: int = 17,
        season_id: int = 76986,
    ) -> None:
        self.lineups_dir = lineups_dir
        self.ratings_dir = ratings_dir
        self.players_csv = players_csv
        self.mapping_file = mapping_file
        self.report_dir = report_dir
        self.refresh_ratings = refresh_ratings
        self.tournament_id = tournament_id
        self.season_id = season_id

    def sync(self) -> dict:
        """Run synchronization and return stats."""
        fantrax_df = self._load_fantrax_players()
        if fantrax_df.empty:
            log.warning("[mapping] Fantrax players CSV empty or missing; skipping mapping sync")
            return {"auto_added": 0, "unmatched": 0, "report": None}

        sofa_players = self._load_sofascore_players()
        if not sofa_players:
            log.info("[mapping] No SofaScore players detected; nothing to sync")
            return {"auto_added": 0, "unmatched": 0, "report": None}

        manager = PlayerMappingManager(mapping_file=str(self.mapping_file))
        existing_sofa_ids = {
            mapping.sofascore_id
            for mapping in manager._mappings.values()  # type: ignore[attr-defined]
            if mapping.sofascore_id is not None
        }

        auto_added: List[PlayerMapping] = []
        unmatched: List[dict] = []
        for player in sofa_players:
            if player.sofascore_id in existing_sofa_ids:
                continue
            match = self._match_player(player, fantrax_df)
            if match is None:
                unmatched.append(
                    {
                        "sofascore_id": player.sofascore_id,
                        "name": player.name,
                        "team_name": player.team_name or "",
                        "role": player.role,
                        "source": str(player.source_file.name),
                    }
                )
                continue

            mapping = PlayerMapping(
                fantrax_id=str(match["id"]),
                fantrax_name=str(match["name"]),
                sofascore_id=int(player.sofascore_id),
                sofascore_name=player.name,
                display_name=str(match["name"]),
            )
            manager._mappings[mapping.fantrax_id] = mapping  # type: ignore[attr-defined]
            existing_sofa_ids.add(player.sofascore_id)
            auto_added.append(mapping)

        if auto_added:
            manager.save_mappings()
            log.info("[mapping] Auto-added %s mappings", len(auto_added))

        report_path = None
        report_link: Optional[str] = None
        if unmatched:
            report_path = self._write_report(unmatched)
            report_link = f"{Path(report_path).resolve()}:1"
            log.warning(
                "[mapping] %s SofaScore players remain unmapped. Report -> %s",
                len(unmatched),
                report_link,
            )
        else:
            log.info("[mapping] All SofaScore lineup players have Fantrax mappings")

        return {
            "auto_added": len(auto_added),
            "unmatched": len(unmatched),
            "report": report_link,
        }

    def _load_fantrax_players(self) -> pd.DataFrame:
        if not self.players_csv.exists():
            log.warning("[mapping] Fantrax players CSV missing at %s", self.players_csv)
            return pd.DataFrame()
        df = pd.read_csv(self.players_csv)
        if df.empty or "name" not in df.columns:
            return pd.DataFrame()
        df["name_canon"] = df["name"].map(_canon)
        df["team"] = df.get("team", "").fillna("")
        return df

    def _load_sofascore_players(self) -> List[SofaPlayer]:
        players: Dict[int, SofaPlayer] = {}
        if not self.lineups_dir.exists():
            log.warning("[mapping] Lineups directory %s missing", self.lineups_dir)
            return []

        json_paths = sorted(self.lineups_dir.glob("*.json"))
        for path in json_paths:
            try:
                with path.open() as fh:
                    payload = json.load(fh)
            except Exception as exc:
                log.debug("[mapping] Failed to parse %s: %s", path, exc)
                continue

            for side in ("home", "away"):
                block = payload.get(side) or {}
                team_name = block.get("team_name") or None
                for role_key in ("starters", "subs", "missing"):
                    for player in block.get(role_key, []) or []:
                        pid = player.get("id")
                        if pid is None:
                            continue
                        if pid in players:
                            continue
                        players[pid] = SofaPlayer(
                            sofascore_id=int(pid),
                            name=player.get("name") or "",
                            team_id=player.get("team_id"),
                            team_name=team_name,
                            role=role_key.rstrip("s"),
                            source_file=path,
                        )

        # Supplement with ratings-based player pool
        for rating in self._load_ratings_players():
            if rating.sofascore_id not in players:
                players[rating.sofascore_id] = rating

        return list(players.values())

    def _load_ratings_players(self) -> List[SofaPlayer]:
        ratings: List[SofaPlayer] = []
        if self.refresh_ratings:
            self._refresh_ratings_cache()
        if not self.ratings_dir.exists():
            return ratings
        csv_files = list(self.ratings_dir.glob("sofascore_players_*.csv"))
        if not csv_files:
            return ratings
        latest = max(csv_files, key=lambda p: p.stat().st_mtime)
        try:
            df = pd.read_csv(latest)
        except Exception as exc:
            log.warning("[mapping] Failed to read ratings file %s: %s", latest, exc)
            return ratings
        if df.empty:
            return ratings
        log.info("[mapping] Loaded %s players from ratings file %s", len(df), latest.name)
        for _, row in df.iterrows():
            try:
                pid = int(row.get("player_id"))
            except Exception:
                continue
            name = str(row.get("player_name") or "").strip()
            if not pid or not name:
                continue
            ratings.append(
                SofaPlayer(
                    sofascore_id=pid,
                    name=name,
                    team_id=None,
                    team_name=row.get("team_name"),
                    role="ratings",
                    source_file=latest,
                )
            )
        return ratings

    def _refresh_ratings_cache(self) -> Optional[Path]:
        self.ratings_dir.mkdir(parents=True, exist_ok=True)
        try:
            players = []
            page = 1
            base_url = (
                f"https://www.sofascore.com/api/v1/unique-tournament/"
                f"{self.tournament_id}/season/{self.season_id}/statistics"
            )
            headers = {
                "User-Agent": "Mozilla/5.0",
                "Accept": "*/*",
                "Referer": "https://www.sofascore.com/",
            }
            while True:
                params = {
                    "limit": 30,
                    "offset": (page - 1) * 30,
                    "order": "-rating",
                    "accumulation": "total",
                    "group": "summary",
                }
                resp = requests.get(base_url, headers=headers, params=params, timeout=30)
                resp.raise_for_status()
                data = resp.json() or {}
                results = data.get("results") or []
                for item in results:
                    pl = item.get("player") or {}
                    team = item.get("team") or {}
                    if not pl.get("id") or not pl.get("name"):
                        continue
                    players.append(
                        {
                            "player_id": int(pl["id"]),
                            "player_name": pl["name"],
                            "team_name": team.get("name"),
                            "team_id": team.get("id"),
                        }
                    )
                if page >= int(data.get("pages") or 1):
                    break
                page += 1
            if not players:
                log.warning("[mapping] SofaScore ratings API returned no players")
                return None
            df = pd.DataFrame(players)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            out_csv = self.ratings_dir / f"sofascore_players_{timestamp}.csv"
            df.to_csv(out_csv, index=False)
            log.info("[mapping] Refreshed ratings file -> %s (%s players)", out_csv, len(df))
            return out_csv
        except Exception as exc:
            log.warning("[mapping] Failed to refresh ratings data: %s", exc)
            return None

    def _match_player(self, sofa_player: SofaPlayer, fantrax_df: pd.DataFrame) -> Optional[pd.Series]:
        canon = sofa_player.canon
        if not canon:
            return None
        candidates = fantrax_df[fantrax_df["name_canon"] == canon]
        if len(candidates) == 1:
            return candidates.iloc[0]

        # Fallback: fuzzy match if a single clear candidate exists
        working = fantrax_df.copy()
        working["fuzz"] = working["name_canon"].apply(lambda name: fuzz.ratio(name, canon))
        fuzzy = working.sort_values("fuzz", ascending=False).head(3)
        if not fuzzy.empty and fuzzy.iloc[0]["fuzz"] >= 96:
            top = fuzzy.iloc[0]
            if len(fuzzy) == 1 or fuzzy.iloc[1]["fuzz"] <= top["fuzz"] - 5:
                return top
        return None

    def _write_report(self, rows: List[dict]) -> str:
        self.report_dir.mkdir(parents=True, exist_ok=True)
        report_path = self.report_dir / "unmatched_players.csv"
        df = pd.DataFrame(rows)
        df.to_csv(report_path, index=False)
        return str(report_path)
