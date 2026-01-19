"""
Build a Premier League team strength file from Understat team match stats.

Outputs:
- JSON at data/derived/team_strengths_pl.json (one record per team, latest sample)
- Parquet at data/derived/team_strengths_pl.parquet (full per-match rolling features)

Intended to be run weekly after matches conclude. The Streamlit conditional
swaps UI will read the JSON artifact to surface team strength context.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import requests
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

import pandas as pd

try:
    import soccerdata as sd
except ImportError as exc:  # pragma: no cover - runtime guidance
    raise SystemExit(
        "soccerdata is required for this script. Install with `pip install soccerdata`."
    ) from exc

try:
    from soccerdata._common import make_game_id
except Exception:
    make_game_id = None  # type: ignore

logger = logging.getLogger(__name__)

DEFAULT_LEAGUE = "ENG-Premier League"
DEFAULT_OUTPUT_JSON = Path("data/derived/team_strengths_pl.json")
DEFAULT_OUTPUT_PARQUET = Path("data/derived/team_strengths_pl.parquet")
DEFAULT_UNDERSTAT_LEAGUE_ID = 1
DEFAULT_UNDERSTAT_SLUG = "EPL"


def _canonical_team_key(name: str) -> str:
    """
    Normalize to a lowercase alphanumeric key for stable lookups.
    """
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def _team_games_from_understat(df: pd.DataFrame) -> pd.DataFrame:
    """
    Convert Understat's home/away wide rows to one row per team per match.
    """
    df = df.reset_index()
    home_cols = {
        "home_team": "team",
        "away_team": "opponent",
        "home_team_code": "team_code",
        "home_goals": "goals_for",
        "away_goals": "goals_against",
        "home_xg": "xg_for",
        "away_xg": "xg_against",
        "home_np_xg": "npxg_for",
        "away_np_xg": "npxg_against",
        "home_ppda": "ppda_def",
        "away_ppda": "ppda_att",
        "home_deep_completions": "deep_for",
        "away_deep_completions": "deep_against",
        "home_points": "points",
        "home_expected_points": "xpoints",
    }
    away_cols = {
        "away_team": "team",
        "home_team": "opponent",
        "away_team_code": "team_code",
        "away_goals": "goals_for",
        "home_goals": "goals_against",
        "away_xg": "xg_for",
        "home_xg": "xg_against",
        "away_np_xg": "npxg_for",
        "home_np_xg": "npxg_against",
        "away_ppda": "ppda_def",
        "home_ppda": "ppda_att",
        "away_deep_completions": "deep_for",
        "home_deep_completions": "deep_against",
        "away_points": "points",
        "away_expected_points": "xpoints",
    }

    home = df[list(home_cols)].rename(columns=home_cols)
    home["venue"] = "home"
    away = df[list(away_cols)].rename(columns=away_cols)
    away["venue"] = "away"

    team_games = (
        pd.concat([home, away], ignore_index=True)
        .assign(
            date=pd.to_datetime(df["date"].tolist() * 2),
            season=df["season_id"].tolist() * 2,
            xg_diff=lambda x: x.xg_for - x.xg_against,
            npxg_diff=lambda x: x.npxg_for - x.npxg_against,
            deep_diff=lambda x: x.deep_for - x.deep_against,
        )
        .sort_values(["team", "date"])
    )
    return team_games


def _apply_rollups(team_games: pd.DataFrame, short: int = 5, long: int = 15) -> pd.DataFrame:
    """
    Add blended short/long rolling features for offense/defense/form.
    """

    def add_rolls(group: pd.DataFrame) -> pd.DataFrame:
        group = group.sort_values("date").copy()
        for window, prefix, weight in ((short, "s", 0.6), (long, "l", 0.4)):
            group[f"{prefix}_xg_for"] = group.xg_for.rolling(window, min_periods=3).mean() * weight
            group[f"{prefix}_xg_against"] = group.xg_against.rolling(window, min_periods=3).mean() * weight
            group[f"{prefix}_npxg_diff"] = group.npxg_diff.rolling(window, min_periods=3).mean() * weight
            group[f"{prefix}_deep_diff"] = group.deep_diff.rolling(window, min_periods=3).mean() * weight
            group[f"{prefix}_xpoints"] = group.xpoints.rolling(window, min_periods=3).mean() * weight
            group[f"{prefix}_ppda_def"] = group.ppda_def.rolling(window, min_periods=3).mean() * weight
        return group

    tg = team_games.groupby("team", group_keys=False).apply(add_rolls)
    tg = tg.assign(
        roll_xg_for=lambda x: x.s_xg_for.add(x.l_xg_for, fill_value=0),
        roll_xg_against=lambda x: x.s_xg_against.add(x.l_xg_against, fill_value=0),
        roll_npxg_diff=lambda x: x.s_npxg_diff.add(x.l_npxg_diff, fill_value=0),
        roll_deep_diff=lambda x: x.s_deep_diff.add(x.l_deep_diff, fill_value=0),
        roll_xpoints=lambda x: x.s_xpoints.add(x.l_xpoints, fill_value=0),
        roll_ppda_def=lambda x: x.s_ppda_def.add(x.l_ppda_def, fill_value=0),
    )
    return tg


def _season_zscores(team_games: pd.DataFrame, cols: Iterable[str]) -> pd.DataFrame:
    def zscore(group: pd.DataFrame) -> pd.DataFrame:
        g = group.copy()
        for col in cols:
            std = g[col].std(ddof=0)
            g[f"z_{col}"] = (g[col] - g[col].mean()) / std if std and std > 0 else 0.0
        return g

    return team_games.groupby("season", group_keys=False).apply(lambda g: zscore(g))


def _default_seasons(count: int = 5) -> list[str]:
    """
    Build a descending list of recent PL seasons (e.g., 2024/2025).
    """
    now = datetime.now(timezone.utc)
    start_year = now.year if now.month >= 7 else now.year - 1
    seasons = []
    for i in range(count):
        y = start_year - i
        seasons.append(f"{y}/{y + 1}")
    return seasons


def _season_id_from_code(season: str | int) -> int:
    try:
        return int(str(season).split("/")[0])
    except Exception:
        return int(season)


def _read_team_match_stats_fallback(
    *,
    seasons: list[str],
    proxy: Optional[str] = None,
    no_cache: bool = False,
    no_store: bool = False,
) -> pd.DataFrame:
    """
    Minimal reimplementation of Understat.read_team_match_stats that bypasses
    the leagues index (statData) and fetches league-season pages directly.
    """
    stats = {}

    # Build a simple requests proxy mapping if provided
    proxy_dict = None
    if proxy:
        if proxy == "tor":
            proxy_dict = {"http": "socks5h://127.0.0.1:9050", "https": "socks5h://127.0.0.1:9050"}
        elif proxy.startswith("http"):
            proxy_dict = {"http": proxy, "https": proxy}

    for season in seasons:
        season_id = _season_id_from_code(season)
        url = f"https://understat.com/league/{DEFAULT_UNDERSTAT_SLUG}/{season_id}"
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            proxies=proxy_dict,
            timeout=20,
        )
        if not resp.ok:
            logger.warning("Failed to fetch %s (status %s)", url, resp.status_code)
            continue
        html = resp.text

        def _extract_json(var_name: str) -> list | dict:
            pattern = rf"{var_name}\s*=\s*JSON.parse\('([^']+)'\)"
            m = re.search(pattern, html)
            if not m:
                return []
            raw = m.group(1)
            try:
                decoded = json.loads(raw.encode().decode("unicode_escape"))
                return decoded
            except Exception:
                try:
                    return json.loads(raw)
                except Exception:
                    return []

        dates_data = _extract_json("datesData") or []
        teams_data = _extract_json("teamsData") or {}

        schedule = {}
        matches = {}

        for match in dates_data:
            match_id = int(match["id"])
            match_date = match["datetime"]
            schedule[match_id] = {
                "league_id": DEFAULT_UNDERSTAT_LEAGUE_ID,
                "league": DEFAULT_LEAGUE,
                "season_id": season_id,
                "season": season,
                "game_id": match_id,
                "date": match["datetime"],
                "home_team_id": int(match["h"]["id"]),
                "away_team_id": int(match["a"]["id"]),
                "home_team": str(match["h"]["title"]),
                "away_team": str(match["a"]["title"]),
                "away_team_code": str(match["a"]["short_title"]),
                "home_team_code": str(match["h"]["short_title"]),
            }
            for side in ("h", "a"):
                team_id = int(match[side]["id"])
                matches[(match_date, team_id)] = match_id

        for team in getattr(teams_data, "values", lambda: [])():
            team_id = int(team["id"])
            for match in team.get("history", []):
                match_date = match["date"]
                match_id = matches.get((match_date, team_id))
                if match_id is None:
                    continue
                team_side = match["h_a"]
                prefix = "home" if team_side == "h" else "away"

                if match_id not in stats:
                    stats[match_id] = schedule.get(match_id, {})

                ppda = match.get("ppda") or {"att": 0, "def": 0}
                team_ppda = (ppda["att"] / ppda["def"]) if ppda.get("def") else pd.NA

                stats[match_id].update(
                    {
                        f"{prefix}_points": int(match.get("pts", 0)),
                        f"{prefix}_expected_points": float(match.get("xpts", 0) or 0),
                        f"{prefix}_goals": float(match.get("scored", 0) or 0),
                        f"{prefix}_xg": float(match.get("xG", 0) or 0),
                        f"{prefix}_np_xg": float(match.get("npxG", 0) or 0),
                        f"{prefix}_np_xg_difference": float(match.get("npxGD", 0) or 0),
                        f"{prefix}_ppda": float(team_ppda) if team_ppda is not pd.NA else pd.NA,
                        f"{prefix}_deep_completions": int(match.get("deep", 0) or 0),
                    }
                )

    index = ["league", "season", "game"]
    if len(stats) == 0:
        return pd.DataFrame(index=index)

    df = (
        pd.DataFrame.from_records(list(stats.values()))
        .assign(date=lambda g: pd.to_datetime(g["date"], format="%Y-%m-%d %H:%M:%S"))
        .assign(game=lambda g: g.apply(make_game_id, axis=1) if make_game_id else g["game_id"])
        .set_index(index)
        .sort_index()
        .convert_dtypes()
    )
    return df


def compute_strengths(
    *,
    seasons: Optional[list[str]] = None,
    short_window: int = 5,
    long_window: int = 15,
    proxy: Optional[str] = None,
    no_cache: bool = False,
    no_store: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Build latest strength per team for configured seasons.
    Returns (latest_strength, enriched_team_games).
    """
    resolved_seasons = seasons or _default_seasons()
    try:
        understat = sd.Understat(
            leagues=DEFAULT_LEAGUE,
            seasons=resolved_seasons,
            proxy=proxy,
            no_cache=no_cache,
            no_store=no_store,
        )
        raw = understat.read_team_match_stats()
    except KeyError as exc:
        logger.warning("Understat read_team_match_stats failed (%s); using fallback fetch.", exc)
        raw = _read_team_match_stats_fallback(
            seasons=resolved_seasons,
            proxy=proxy,
            no_cache=no_cache,
            no_store=no_store,
        )
    if raw.empty:
        raise ValueError(
            "No team match stats returned from Understat (fallback also empty). "
            "Understat may be blocking requests; try `--proxy tor` with Tor running on 9050 "
            "or provide cached Understat files under ~/soccerdata/data/Understat/."
        )

    team_games = _team_games_from_understat(raw)
    team_games = _apply_rollups(team_games, short=short_window, long=long_window)

    # Weighted sub-scores
    w_off_xg = 0.4
    w_off_deep = 0.2
    w_def_xga = 0.25
    w_def_ppda = 0.1
    w_form = 0.3

    team_games = team_games.assign(
        offense=lambda x: w_off_xg * x.roll_xg_for + w_off_deep * x.roll_deep_diff.clip(lower=0),
        defense=lambda x: w_def_xga * (-x.roll_xg_against) + w_def_ppda * (-x.roll_ppda_def),
        form=lambda x: w_form * (0.6 * x.roll_npxg_diff + 0.4 * x.roll_xpoints),
    )

    team_games = _season_zscores(team_games, cols=["offense", "defense", "form"])
    team_games["strength"] = (
        team_games["z_offense"] + team_games["z_defense"] + team_games["z_form"]
    )

    latest_strength = (
        team_games.sort_values("date")
        .groupby(["season", "team"], as_index=False)
        .tail(1)
        .assign(
            updated_at=datetime.now(timezone.utc).isoformat(),
            matches_in_sample=lambda x: x.groupby("team")["team"].transform("count"),
            short_window=short_window,
            long_window=long_window,
        )
    )
    return latest_strength, team_games


def write_outputs(df_latest: pd.DataFrame, df_full: pd.DataFrame, *, json_path: Path, parquet_path: Path) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)

    records = []
    for _, row in df_latest.iterrows():
        rec = {
            "team": row.get("team"),
            "team_code": row.get("team_code"),
            "season": int(row.get("season")) if pd.notna(row.get("season")) else None,
            "strength": float(row.get("strength", 0.0)),
            "offense": float(row.get("z_offense", 0.0)),
            "defense": float(row.get("z_defense", 0.0)),
            "form": float(row.get("z_form", 0.0)),
            "matches_in_sample": int(row.get("matches_in_sample", 0)),
            "short_window": int(row.get("short_window", 0)),
            "long_window": int(row.get("long_window", 0)),
            "updated_at": row.get("updated_at"),
        }
        records.append(rec)

    json_path.write_text(json.dumps(records, indent=2))
    df_full.to_parquet(parquet_path, index=False)
    logger.info("Wrote %s and %s", json_path, parquet_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Premier League team strengths from Understat.")
    parser.add_argument(
        "--season",
        action="append",
        help="Season(s) as used by Understat (e.g., 2023/2024). Omit to fetch all available.",
    )
    parser.add_argument("--json-path", default=str(DEFAULT_OUTPUT_JSON), help="Path to write JSON summary.")
    parser.add_argument(
        "--parquet-path",
        default=str(DEFAULT_OUTPUT_PARQUET),
        help="Path to write Parquet with per-match rolling features.",
    )
    parser.add_argument("--short-window", type=int, default=5, help="Short rolling window size.")
    parser.add_argument("--long-window", type=int, default=15, help="Long rolling window size.")
    parser.add_argument(
        "--proxy",
        default=None,
        help='Proxy passed to soccerdata Understat (e.g., "tor" or http proxy dict string).',
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="Force re-download, bypassing cached Understat files.",
    )
    args = parser.parse_args()

    short_window = max(3, args.short_window)
    long_window = max(short_window, args.long_window)

    logger.info("Fetching Understat data for league=%s seasons=%s", DEFAULT_LEAGUE, args.season or "all")
    latest, team_games = compute_strengths(
        seasons=args.season,
        short_window=short_window,
        long_window=long_window,
        proxy=args.proxy,
        no_cache=args.no_cache,
    )

    write_outputs(
        latest,
        team_games,
        json_path=Path(args.json_path),
        parquet_path=Path(args.parquet_path),
    )


if __name__ == "__main__":
    main()
