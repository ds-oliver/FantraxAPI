#!/usr/bin/env python
"""
Script to update player mappings from various sources.
"""
import argparse
import pickle
from pathlib import Path
import pandas as pd
import requests
import logging
import time
from datetime import datetime, timezone
from thefuzz import fuzz
from unidecode import unidecode
import yaml

# NEW: optional imports used by ESD + raw fallback
try:
    import httpx  # only used by ESD raw fallbacks
except Exception:  # pragma: no cover
    httpx = None

try:
    import esd  # EasySoccerData
except Exception:  # pragma: no cover
    esd = None

API_BASE = "https://api.sofascore.com/api/v1"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36"
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

from fantraxapi.fantrax import FantraxAPI
from fantraxapi.player_mapping import PlayerMapping, PlayerMappingManager

# --------------------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------------------

def setup_logging(data_dir: Path) -> None:
    """Set up logging to both file and console."""
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"player_mapping_{timestamp}.log"
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[logging.FileHandler(log_file), logging.StreamHandler()]
    )
    logging.info(f"Logging to: {log_file}")

# --------------------------------------------------------------------------------------
# Fantrax + config
# --------------------------------------------------------------------------------------

def load_fantrax_players(league_id: str, session: requests.Session) -> list:
    """Get all players from Fantrax."""
    api = FantraxAPI(league_id, session=session)
    return api.get_all_players()

def load_team_mappings(config_dir: Path) -> tuple[dict, dict]:
    """
    Load team mappings from both files.
    Returns:
        Tuple of (code_mappings, club_mappings) where:
        - code_mappings: Dict mapping team codes to standard codes
        - club_mappings: Dict mapping team names to standard codes
    """
    with open(config_dir / "team_mappings.yaml") as f:
        code_mappings = yaml.safe_load(f)
    reverse_code_map = {}
    for standard, variations in code_mappings.items():
        for var in variations:
            reverse_code_map[var] = standard
        reverse_code_map[standard] = standard
    with open(config_dir / "club_team_mappings.yaml") as f:
        club_mappings = yaml.safe_load(f)
    reverse_club_map = {}
    for code, data in club_mappings.items():
        names = [
            data["long_name"],
            data["short_name"],
            *(data.get("long_name_variations", [])),
            *(data.get("short_name_variations", [])),
            *(data.get("nicknames", []))
        ]
        for name in names:
            reverse_club_map[name.lower()] = code
    return reverse_code_map, club_mappings

def load_ffscout_data(data_dir: Path) -> pd.DataFrame:
    """Load the most recent FFScout data."""
    files = list(data_dir.glob("scout_picks_rosters_*.parquet"))
    if not files:
        return pd.DataFrame()
    latest = max(files, key=lambda p: p.stat().st_mtime)
    return pd.read_parquet(latest)

# --------------------------------------------------------------------------------------
# SofaScore ratings-list (your existing HTTP path)
# --------------------------------------------------------------------------------------

def load_sofascore_data(data_dir: Path, code_mappings: dict, club_mappings: dict, force_refresh: bool = False) -> pd.DataFrame:
    """
    Load SofaScore player data from cache or API. (Ratings-based list)
    NOTE: This list often excludes deeper-squad players; we now optionally merge ESD rosters for completeness.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = data_dir / "silver" / "sofascore"
    cache_dir.mkdir(parents=True, exist_ok=True)
    print(f"Using cache directory: {cache_dir}")
    cache_files = list(cache_dir.glob("sofascore_players_*.parquet"))

    if not force_refresh and cache_files:
        latest = max(cache_files, key=lambda p: p.stat().st_mtime)
        if time.time() - latest.stat().st_mtime < 8 * 60 * 60:
            print(f"Loading SofaScore data from cache: {latest.name}")
            df = pd.read_parquet(latest)
            print(f"Loaded {len(df)} players from cache")
            return df

    # Keep your existing season id (historical endpoint)
    SEASON_ID = 76986  # 2023-24 season (left as-is to avoid breaking structure)

    base_url = "https://www.sofascore.com/api/v1/unique-tournament/17/season"
    headers = {
        "User-Agent": UA,
        "Accept": "*/*",
        "Referer": "https://www.sofascore.com/tournament/football/england/premier-league/17",
        "Cache-Control": "no-cache",
        "x-requested-with": "b548fe",
    }

    try:
        players = []
        print("Fetching SofaScore data from API...")
        page = 1
        while True:
            offset = (page - 1) * 20
            url = f"{base_url}/{SEASON_ID}/statistics"
            params = {
                "limit": 20,
                "offset": offset,
                "order": "-rating",
                "accumulation": "total",
                "group": "summary",
            }
            response = requests.get(url, headers=headers, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()

            for player in data.get("results", []):
                team_name = player["team"]["name"]
                std_team = standardize_team(team_name, code_mappings, club_mappings)
                player_record = {
                    "player_id": int(player["player"]["id"]),
                    "player_name": player["player"]["name"],
                    "team_name": team_name,
                    "team_code": std_team,
                }
                players.append(player_record)

            if page >= data.get("pages", 1):
                break
            page += 1
            time.sleep(0.5)

        df = pd.DataFrame(players)
        df["team_code"] = df["team_code"].apply(lambda x: standardize_team(x, code_mappings, club_mappings))

        timestamp = time.strftime("%Y%m%d_%H%M%S")
        cache_file = cache_dir / f"sofascore_players_{timestamp}.parquet"
        df.to_parquet(cache_file)
        logging.info(f"Saved SofaScore data to cache: {cache_file.name}")

        csv_file = cache_dir / f"sofascore_players_{timestamp}.csv"
        df.to_csv(csv_file, index=False)
        logging.info(f"Saved SofaScore data to CSV: {csv_file.name}")

        print(f"\nLoaded {len(players)} players (ratings list)")
        return df
    except Exception as e:
        logging.error(f"Error loading SofaScore data: {e}")
        return pd.DataFrame()

# --------------------------------------------------------------------------------------
# Helpers for names/teams/matching (unchanged)
# --------------------------------------------------------------------------------------

def extract_name_and_info(full_str: str) -> tuple[str, str, str, str]:
    if not full_str:
        return "", "", "", ""
    parts = full_str.split("(", 1)
    name = parts[0].strip()
    info = parts[1].strip(")").strip() if len(parts) > 1 else ""
    team = ""
    position = ""
    if " - " in info:
        team, position = info.split(" - ", 1)
    else:
        team = info
    return name, team.strip(), position.strip(), info

def normalize_name(name: str, remove_accents: bool = False) -> str:
    if not name:
        return ""
    name, _, _, _ = extract_name_and_info(name)
    name = name.lower()
    if remove_accents:
        name = unidecode(name)
    name = name.replace("-", " ").replace("'", "").replace(".", " ")
    name = " ".join(name.split())
    return name

def standardize_team(team: str, code_mappings: dict, club_mappings: dict) -> str:
    if not team:
        return ""
    team = team.strip().lower()

    for code, data in code_mappings.items():
        if isinstance(data, dict):
            variations = [v.lower() for v in data.get("variations", [])]
            if team in variations or team == code.lower():
                return code

    for code, data in club_mappings.items():
        if isinstance(data, dict):
            all_variations = [
                data.get("long_name", "").lower(),
                data.get("short_name", "").lower(),
                code.lower(),
                *[v.lower() for v in data.get("long_name_variations", [])],
                *[v.lower() for v in data.get("short_name_variations", [])],
                *[v.lower() for v in data.get("nicknames", [])],
            ]
            if team in all_variations:
                return code

    if len(team) > 10:
        best_score = 0
        best_code = None
        for code, data in club_mappings.items():
            if isinstance(data, dict):
                names_to_try = [
                    data.get("long_name", "").lower(),
                    *[v.lower() for v in data.get("long_name_variations", [])],
                ]
                for name in names_to_try:
                    if name:
                        score = fuzz.ratio(team, name)
                        if score > best_score and score >= 90:
                            best_score = score
                            best_code = code
        if best_code:
            return best_code

    if team.startswith("manchester "):
        if "city" in team or "mnc" in team or "mci" in team:
            return "MCI"
        if "united" in team or "utd" in team or "mun" in team:
            return "MUN"

    if len(team.split()) > 1:
        words = team.split()
        if len(words) >= 2 and words[0] in ["west", "east", "north", "south"]:
            return words[1][:3].upper()
        return words[0][:3].upper()
    return team[:3].upper()

def find_matches(
    name: str, 
    team_code: str,
    candidates: list[tuple[str, str]],  # (name, team_code)
    code_mappings: dict,
    club_mappings: dict,
    threshold: int = 75
) -> list[tuple[str, int, str, str]]:
    matches = []
    name_only, team, pos, _ = extract_name_and_info(name)
    norm_name = normalize_name(name_only)
    norm_name_no_accents = normalize_name(name_only, remove_accents=True)
    std_team = standardize_team(team_code or team, code_mappings, club_mappings)

    for candidate, cand_team in candidates:
        cand_name, cand_team_info, cand_pos, _ = extract_name_and_info(candidate)
        norm_cand = normalize_name(cand_name)
        norm_cand_no_accents = normalize_name(cand_name, remove_accents=True)
        std_cand_team = standardize_team(cand_team or cand_team_info, code_mappings, club_mappings)

        if norm_name == norm_cand:
            base_score = 100
        elif norm_name_no_accents == norm_cand_no_accents:
            base_score = 99
        else:
            len_diff = abs(len(norm_name) - len(norm_cand))
            if len_diff > 5:
                continue
            scores = [
                fuzz.ratio(norm_name, norm_cand) * 1.0,
                fuzz.token_sort_ratio(norm_name, norm_cand) * 0.9,
                fuzz.token_set_ratio(norm_name, norm_cand) * 0.8,
            ]
            base_score = max(scores)
            if len_diff > 0:
                base_score = base_score * (1 - len_diff * 0.05)

        if std_team == std_cand_team:
            if base_score >= 90:
                base_score = 100
            elif base_score >= 80:
                base_score = base_score * 1.2
        else:
            base_score = base_score * 0.5

        final_score = int(base_score)
        if final_score >= threshold:
            display_name = cand_name
            display_info = f"{std_cand_team}"
            if cand_pos:
                display_info += f" - {cand_pos}"
            matches.append((display_name, final_score, display_info, std_cand_team))

    matches.sort(key=lambda x: x[1], reverse=True)
    return matches

# --------------------------------------------------------------------------------------
# NEW: ESD helpers (season resolution + raw fallback + full roster pull)
# --------------------------------------------------------------------------------------

def _getv(obj, *names, default=None):
    for n in names:
        if isinstance(obj, dict) and n in obj:
            return obj[n]
        if hasattr(obj, n):
            return getattr(obj, n)
    return default

def _raw_get_json(url: str, referer: str | None = None) -> dict:
    if httpx is None:
        # fallback to requests
        h = HEADERS.copy()
        if referer:
            h["Referer"] = referer
        r = requests.get(url, headers=h, params={"_": int(datetime.now().timestamp()*1000)}, timeout=30)
        r.raise_for_status()
        return r.json()
    else:
        h = HEADERS.copy()
        if referer:
            h["Referer"] = referer
        with httpx.Client(follow_redirects=True, headers=h, timeout=30) as s:
            r = s.get(url, params={"_": int(datetime.now().timestamp()*1000)})
            r.raise_for_status()
            return r.json()

def _raw_get_seasons(tournament_id: int) -> list[dict]:
    url = f"{API_BASE}/unique-tournament/{tournament_id}/seasons"
    data = _raw_get_json(url)
    return data.get("seasons") or []

def _raw_iter_tournament_events(tournament_id: int, season_id: int, upcoming: bool):
    page = 0
    path = "next" if upcoming else "last"
    while True:
        url = f"{API_BASE}/unique-tournament/{tournament_id}/season/{season_id}/events/{path}/{page}"
        data = _raw_get_json(url)
        events = data.get("events") or []
        if not events:
            break
        for ev in events:
            yield ev
        page += 1

def _season_label(s) -> str:
    return str(_getv(s, "year", "name", default=""))

def _season_is_current(s) -> bool:
    return bool(_getv(s, "current", default=False))

def _season_id_of(s) -> int:
    return int(_getv(s, "id"))

def _choose_season_from_list(seasons: list, season_text: str | None) -> int:
    if season_text:
        target = season_text.strip().lower()
        for s in seasons:
            if _season_label(s).strip().lower() == target:
                return _season_id_of(s)
        for s in seasons:
            if target in _season_label(s).strip().lower():
                return _season_id_of(s)
        raise RuntimeError(f"Season '{season_text}' not found. Options: {[ _season_label(x) for x in seasons ]}")
    current = [s for s in seasons if _season_is_current(s)]
    if current:
        return _season_id_of(current[0])
    seasons_sorted = sorted(seasons, key=lambda x: _season_id_of(x), reverse=True)
    return _season_id_of(seasons_sorted[0])

def _pick_season_id(client: "esd.SofascoreClient", tournament_id: int, season_text: str | None, season_id: int | None) -> int:
    if season_id is not None:
        return int(season_id)
    seasons_esd = []
    try:
        seasons_esd = client.get_tournament_seasons(tournament_id)
    except Exception:
        seasons_esd = []
    if seasons_esd:
        return _choose_season_from_list(seasons_esd, season_text)
    seasons_raw = _raw_get_seasons(tournament_id)
    if not seasons_raw:
        raise RuntimeError(f"No seasons available for tournament {tournament_id} (both ESD and raw).")
    return _choose_season_from_list(seasons_raw, season_text)

def _iter_tournament_events_esd_or_raw(client: "esd.SofascoreClient", tournament_id: int, season_id: int, upcoming: bool):
    # ESD first
    page = 0
    seen = set()
    try:
        while True:
            batch = client.get_tournament_events(tournament_id, season_id, upcoming=upcoming, page=page)
            if not batch:
                break
            for ev in batch:
                ev_id = int(_getv(ev, "id"))
                if ev_id not in seen:
                    seen.add(ev_id)
                    yield ev
            page += 1
        return
    except Exception:
        pass
    # raw fallback
    for ev in _raw_iter_tournament_events(tournament_id, season_id, upcoming):
        yield ev

def load_esd_players(
    data_dir: Path,
    code_mappings: dict,
    club_mappings: dict,
    tournament_id: int = 17,
    season_text: str | None = None,
    season_id: int | None = None,
    browser_path: str | None = None,
    force_refresh: bool = False
) -> pd.DataFrame:
    """
    Fetch full squad lists per team using ESD's get_team_players(team_id).
    Returns DataFrame: [player_id, player_name, team_id, team_name, team_code]
    """
    out_dir = data_dir / "silver" / "esd"
    out_dir.mkdir(parents=True, exist_ok=True)

    # We'll write per (tournament_id, season_id) cache
    cache_key = f"{tournament_id}_{season_text or season_id or 'current'}"
    cache_path = out_dir / f"esd_players_{cache_key}.parquet"

    if cache_path.exists() and not force_refresh:
        # refresh cache every 8h for safety
        if time.time() - cache_path.stat().st_mtime < 8 * 60 * 60:
            try:
                df_cached = pd.read_parquet(cache_path)
                logging.info(f"Loaded ESD roster cache: {cache_path.name} ({len(df_cached)} players)")
                return df_cached
            except Exception:
                pass

    if esd is None:
        logging.warning("ESD not installed; skipping ESD roster fetch.")
        return pd.DataFrame()

    try:
        client = esd.SofascoreClient(browser_path=browser_path) if browser_path else esd.SofascoreClient()
        season_id_resolved = _pick_season_id(client, tournament_id, season_text, season_id)

        # Build team_id -> team_name map by scanning events (covers all teams in the season)
        team_names: dict[int, str] = {}
        logging.info("Enumerating teams from events to collect team IDs (ESD -> raw fallback)...")
        for ev in _iter_tournament_events_esd_or_raw(client, tournament_id, season_id_resolved, upcoming=False):
            home = _getv(ev, "homeTeam", "home_team", "home", default={}) or {}
            away = _getv(ev, "awayTeam", "away_team", "away", default={}) or {}
            hid = int(_getv(home, "id", default=0) or 0)
            aid = int(_getv(away, "id", default=0) or 0)
            hname = _getv(home, "name", "shortName", "short_name", "slug", default="?")
            aname = _getv(away, "name", "shortName", "short_name", "slug", default="?")
            if hid:
                team_names.setdefault(hid, hname)
            if aid:
                team_names.setdefault(aid, aname)

        if not team_names:
            logging.warning("No teams discovered from events; cannot fetch ESD rosters.")
            return pd.DataFrame()

        # Fetch full roster per team via ESD
        rows = []
        for tid, tname in team_names.items():
            try:
                players = client.get_team_players(int(tid))  # <-- ESD call
                for p in players or []:
                    info = getattr(p, "info", None)
                    pid = getattr(info, "id", None) if info else None
                    pname = getattr(info, "name", None) if info else None
                    if pid and pname:
                        rows.append({
                            "player_id": int(pid),
                            "player_name": str(pname),
                            "team_id": int(tid),
                            "team_name": str(tname),
                        })
                logging.info(f"ESD roster fetched: {tname} ({tid}) -> {len(players or [])} players")
                time.sleep(0.2)  # be gentle
            except Exception as e:
                logging.warning(f"ESD get_team_players failed for team {tid} ({tname}): {e}")

        if not rows:
            logging.warning("ESD returned no roster rows.")
            return pd.DataFrame()

        df = pd.DataFrame(rows)
        # Standardize team code
        df["team_code"] = df["team_name"].apply(lambda x: standardize_team(x, code_mappings, club_mappings))
        # Deduplicate
        df = df.drop_duplicates(subset=["player_id"]).reset_index(drop=True)

        # Cache
        df.to_parquet(cache_path)
        csv_path = cache_path.with_suffix(".csv")
        df.to_csv(csv_path, index=False)
        logging.info(f"Saved ESD roster cache: {cache_path.name} ({len(df)} players)")
        return df
    except Exception as e:
        logging.error(f"ESD roster load failed: {e}")
        return pd.DataFrame()

# --------------------------------------------------------------------------------------
# Main mapping pipeline
# --------------------------------------------------------------------------------------

def update_mappings(
    league_id: str,
    data_dir: Path,
    output_file: Path,
    cookie_file: str = "fantraxloggedin.cookie",
    interactive: bool = True,
    config_dir: Path = Path("config"),
    force_refresh: bool = False,
    # NEW: ESD options
    use_esd: bool = False,
    tournament_id: int = 17,
    season_text: str | None = None,
    season_id: int | None = None,
    browser_path: str | None = None,
) -> None:
    """
    Update player mappings from various sources.
    Optionally merges full ESD rosters for comprehensive matching.
    """
    setup_logging(data_dir)

    # Fantrax session
    session = requests.Session()
    try:
        with open(cookie_file, "rb") as f:
            cookies = pickle.load(f)
            for cookie in cookies:
                session.cookies.set(cookie["name"], cookie["value"])
            logging.info("Loaded Fantrax session cookies")
    except FileNotFoundError:
        logging.error(f"Cookie file not found: {cookie_file}")
        logging.error("Please run bootstrap_cookie.py first")
        return

    manager = PlayerMappingManager(str(output_file))
    logging.info(f"Initialized player mapping manager with {output_file}")

    # Fantrax players (source of truth)
    logging.info("Fetching players from Fantrax...")
    fantrax_players = load_fantrax_players(league_id, session)
    logging.info(f"Found {len(fantrax_players)} players in Fantrax")

    # Team mappings
    code_mappings, club_mappings = load_team_mappings(config_dir)
    logging.info("Loaded team mappings from config files")

    # FFScout (optional)
    ffscout_df = load_ffscout_data(data_dir / "silver/scout_picks")
    if not ffscout_df.empty:
        logging.info(f"Loaded FFScout data with {len(ffscout_df)} players")
    else:
        logging.warning("No FFScout data found")

    # SofaScore (ratings list)
    sofascore_df = load_sofascore_data(data_dir, code_mappings, club_mappings, force_refresh)
    if not sofascore_df.empty:
        logging.info(f"Found {len(sofascore_df)} players in SofaScore ratings list")
    else:
        logging.warning("No SofaScore ratings data found")

    # NEW: ESD rosters (merge for completeness)
    esd_df = pd.DataFrame()
    if use_esd:
        esd_df = load_esd_players(
            data_dir=data_dir,
            code_mappings=code_mappings,
            club_mappings=club_mappings,
            tournament_id=tournament_id,
            season_text=season_text,
            season_id=season_id,
            browser_path=browser_path,
            force_refresh=force_refresh,
        )
        if not esd_df.empty:
            logging.info(f"ESD rosters loaded: {len(esd_df)} players")
        else:
            logging.warning("ESD roster dataframe is empty; proceeding without it.")

    # Combined Sofa pool for matching
    if not esd_df.empty and not sofascore_df.empty:
        sofa_all_df = pd.concat([sofascore_df[["player_id", "player_name", "team_code"]],
                                 esd_df[["player_id", "player_name", "team_code"]]], ignore_index=True)
        sofa_all_df = sofa_all_df.drop_duplicates(subset=["player_id"]).reset_index(drop=True)
    elif not esd_df.empty:
        sofa_all_df = esd_df[["player_id", "player_name", "team_code"]].copy()
    else:
        sofa_all_df = sofascore_df[["player_id", "player_name", "team_code"]].copy() if not sofascore_df.empty else pd.DataFrame()

    stats = {
        "total_players": len(fantrax_players),
        "existing_mappings": 0,
        "new_mappings": 0,
        "ffscout_matches": 0,
        "no_ffscout_match": 0,
        "ffscout_exact_matches": 0,
        "ffscout_manual_matches": 0,
        "sofascore_matches": 0,
        "no_sofascore_match": 0,
        "sofascore_exact_matches": 0,
        "sofascore_manual_matches": 0
    }

    unmatched_players = []

    logging.info("\nPhase 1: Processing exact matches...")
    logging.info("Note: FFScout data only includes likely starters, while Fantrax includes all squad players.")
    logging.info("Many players without FFScout matches is expected and normal.\n")

    # Build quick lookup from Sofa pool name -> id (may be multiple; keep first)
    if not sofa_all_df.empty:
        # normalize keys for safety
        sofa_name_index = {}
        for _, r in sofa_all_df.iterrows():
            key = (r["player_name"], r["team_code"])
            if key not in sofa_name_index:
                sofa_name_index[key] = int(r["player_id"])

    for i, player in enumerate(fantrax_players, 1):
        if i % 50 == 0:
            logging.info(f"Processed {i}/{stats['total_players']} players...")

        mapping = manager.get_by_fantrax_id(player.id)
        if mapping:
            mapping.fantrax_name = player.name
            stats["existing_mappings"] += 1
            if mapping.ffscout_name:
                stats["ffscout_matches"] += 1
            if mapping.sofascore_id:
                stats["sofascore_matches"] += 1
            if mapping.ffscout_name and mapping.sofascore_id:
                continue
        else:
            mapping = PlayerMapping(
                fantrax_id=player.id,
                fantrax_name=player.name,
                sofascore_id=None,
                sofascore_name=None,
                ffscout_name=None,
                other_names=[]
            )
            stats["new_mappings"] += 1

        # Track unmatched counters up-front for consistency
        stats["no_ffscout_match"] += 1
        stats["no_sofascore_match"] += 1

        if player.first_name and player.last_name:
            mapping.other_names.extend([
                f"{player.first_name} {player.last_name}",
                f"{player.last_name}, {player.first_name}",
                player.last_name,
                f"{player.first_name[0]}. {player.last_name}"
            ])

        # FFScout matching
        if not ffscout_df.empty and not mapping.ffscout_name:
            ffscout_candidates = []
            for _, row in ffscout_df.iterrows():
                if row.get("player_full_from_title"):
                    ffscout_candidates.append((row["player_full_from_title"], row["team_code"]))
                if row.get("player_display"):
                    ffscout_candidates.append((row["player_display"], row["team_code"]))

            matches = find_matches(
                player.name,
                player.team.lower(),
                ffscout_candidates,
                code_mappings, club_mappings,
                threshold=70
            )

            if matches:
                best_score = matches[0][1]
                if best_score >= 95:
                    mapping.ffscout_name = matches[0][0]
                    stats["ffscout_matches"] += 1
                    stats["ffscout_exact_matches"] += 1
                    stats["no_ffscout_match"] -= 1
                    logging.info(f"Exact FFScout match: {player.name} ({player.team}) -> {matches[0][0]} ({matches[0][3]}) [score: {matches[0][1]}]")
                else:
                    matches_for_review = [m for m in matches if m[1] >= 75]
                    if matches_for_review:
                        unmatched_players.append((player, mapping, ("ffscout", matches_for_review)))
                    else:
                        unmatched_players.append((player, mapping, ("ffscout", [])))

            if mapping.ffscout_name:
                display_names = ffscout_df[
                    ffscout_df["player_full_from_title"] == mapping.ffscout_name
                ]["player_display"]
                if not display_names.empty:
                    display_name = display_names.iloc[0]
                    if display_name and display_name != mapping.ffscout_name:
                        mapping.other_names.append(display_name)

        # SofaScore (ratings list + ESD rosters merged)
        if not sofa_all_df.empty and not mapping.sofascore_id:
            sofa_candidates = [(row["player_name"], row["team_code"]) for _, row in sofa_all_df.iterrows()]
            matches = find_matches(
                player.name,
                player.team.lower(),
                sofa_candidates,
                code_mappings, club_mappings,
                threshold=70
            )
            if matches:
                best_score = matches[0][1]
                if best_score >= 95:
                    match_name = matches[0][0]
                    # get id by (name, team_code) if available; fallback to name-only first match
                    std_tc = matches[0][3]
                    # try exact key
                    pid = None
                    if 'sofa_name_index' in locals():
                        pid = sofa_name_index.get((match_name, std_tc))
                        if pid is None:
                            # fallback: first matching name (could collide across teams in rare cases)
                            subset = sofa_all_df[sofa_all_df["player_name"] == match_name]
                            if not subset.empty:
                                pid = int(subset.iloc[0]["player_id"])
                    else:
                        subset = sofa_all_df[sofa_all_df["player_name"] == match_name]
                        if not subset.empty:
                            pid = int(subset.iloc[0]["player_id"])

                    if pid is not None:
                        mapping.sofascore_id = pid
                        mapping.sofascore_name = match_name
                        stats["sofascore_matches"] += 1
                        stats["sofascore_exact_matches"] += 1
                        stats["no_sofascore_match"] -= 1
                        logging.info(f"Exact SofaScore match: {player.name} ({player.team}) -> {match_name} ({matches[0][3]}) [score: {matches[0][1]}, id: {pid}]")
                else:
                    matches_for_review = [m for m in matches if m[1] >= 75]
                    if matches_for_review:
                        unmatched_players.append((player, mapping, ("sofascore", matches_for_review)))
                    else:
                        unmatched_players.append((player, mapping, ("sofascore", [])))

        mapping.other_names = list(set(mapping.other_names))
        manager.add_mapping(mapping)

    # Phase 2: Manual review
    if unmatched_players and interactive:
        logging.info("\nPhase 2: Manual matching for remaining players...")
        logging.info(f"Found {len(unmatched_players)} players without exact matches (showing score >= 75)")
        for player, mapping, (source, matches) in unmatched_players:
            if not matches:
                continue
            std_player_team = standardize_team(player.team, code_mappings, club_mappings)
            logging.info(f"\nPotential {source.title()} matches for {player.name} ({std_player_team} - {player.position}):")
            for i, (match_name, score, display_info, std_team) in enumerate(matches[:5], 1):
                print(f"  {i}. {match_name} ({std_team}) [score: {score}]")
            print("  0. None of these")
            if source == "ffscout":
                logging.info("\nNote: It's normal if none match - FFScout only includes likely starters")

            while True:
                try:
                    choice = input("Select match number (0-5): ").strip()
                    if not choice:
                        break
                    choice = int(choice)
                    if 0 <= choice <= len(matches[:5]):
                        if choice > 0:
                            match_name = matches[choice-1][0]
                            if source == "ffscout":
                                mapping.ffscout_name = match_name
                                stats["ffscout_matches"] += 1
                                stats["ffscout_manual_matches"] += 1
                                stats["no_ffscout_match"] -= 1
                                logging.info(f"Manual FFScout match: {player.name} -> {match_name} ({matches[choice-1][3]}) [score: {matches[choice-1][1]}]")
                            else:
                                # SofaScore id lookup, considering merged pool
                                pid = None
                                # Prefer exact (name, team) match
                                if not sofa_all_df.empty:
                                    exact = sofa_all_df[
                                        (sofa_all_df["player_name"] == match_name) &
                                        (sofa_all_df["team_code"] == matches[choice-1][3])
                                    ]
                                    if not exact.empty:
                                        pid = int(exact.iloc[0]["player_id"])
                                    else:
                                        subset = sofa_all_df[sofa_all_df["player_name"] == match_name]
                                        if not subset.empty:
                                            pid = int(subset.iloc[0]["player_id"])
                                if pid is not None:
                                    mapping.sofascore_id = pid
                                    mapping.sofascore_name = match_name
                                    stats["sofascore_matches"] += 1
                                    stats["sofascore_manual_matches"] += 1
                                    stats["no_sofascore_match"] -= 1
                                    logging.info(f"Manual SofaScore match: {player.name} -> {match_name} [{pid}] ({matches[choice-1][3]}) [score: {matches[choice-1][1]}]")
                        break
                except ValueError:
                    pass
                logging.warning("Invalid choice. Please enter a number between 0 and 5.")
                print("Invalid choice. Please enter a number between 0 and 5.")

    # Stats
    logging.info("\nProcessing complete!")
    logging.info("=" * 50)
    logging.info("Statistics:")
    logging.info(f"Total Fantrax players: {stats['total_players']}")
    logging.info(f"Existing mappings updated: {stats['existing_mappings']}")
    logging.info(f"New mappings created: {stats['new_mappings']}")
    logging.info("\nFFScout matches:")
    logging.info(f"Total matches found: {stats['ffscout_matches']}")
    logging.info(f"  - Exact matches: {stats['ffscout_exact_matches']}")
    logging.info(f"  - Manual matches: {stats['ffscout_manual_matches']}")
    logging.info(f"Players without FFScout match: {stats['no_ffscout_match']}")
    logging.info(f"FFScout match rate: {stats['ffscout_matches']/stats['total_players']*100:.1f}%")
    logging.info("\nSofaScore matches (ratings + ESD rosters):")
    logging.info(f"Total matches found: {stats['sofascore_matches']}")
    logging.info(f"  - Exact matches: {stats['sofascore_exact_matches']}")
    logging.info(f"  - Manual matches: {stats['sofascore_manual_matches']}")
    logging.info(f"Players without SofaScore match: {stats['no_sofascore_match']}")
    logging.info(f"SofaScore match rate: {stats['sofascore_matches']/stats['total_players']*100:.1f}%")
    logging.info("=" * 50)

    manager.save_mappings()
    logging.info(f"\nSaved mappings to: {output_file}")

# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------

def main():
    # Your Premier League league ID
    LEAGUE_ID = "o90qdw15mc719reh"

    parser = argparse.ArgumentParser(description="Update player mappings from various sources")
    parser.add_argument("--data-dir", type=str, default="/Users/hogan/FantraxAPI/data", help="Directory containing data files")
    parser.add_argument("--output", type=str, default="config/player_mappings.yaml", help="Output YAML file for mappings")
    parser.add_argument("--cookie-file", type=str, default="fantraxloggedin.cookie", help="Path to cookie file")
    parser.add_argument("--noninteractive", action="store_true", help="Skip interactive prompts for uncertain matches")
    parser.add_argument("--force-refresh", action="store_true", help="Force refresh of SofaScore/ESD data, ignoring cache")

    # NEW: ESD options
    parser.add_argument("--use-esd", action="store_true",
                        help="Also fetch full team rosters via ESD get_team_players() and merge into matching pool")
    parser.add_argument("--tournament-id", type=int, default=17, help="SofaScore tournament id (default: 17 = Premier League)")
    parser.add_argument("--season", type=str, default=None, help="Season label to match (e.g. '2024/2025' or '24/25')")
    parser.add_argument("--season-id", type=int, default=None, help="Explicit season_id (overrides --season)")
    parser.add_argument("--browser-path", type=str, default="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                        help="Path to Chrome/Chromium if ESD needs it")
    args = parser.parse_args()

    update_mappings(
        LEAGUE_ID,
        Path(args.data_dir),
        Path(args.output),
        args.cookie_file,
        not args.noninteractive,
        force_refresh=args.force_refresh,
        # ESD
        use_esd=args.use_esd,
        tournament_id=args.tournament_id,
        season_text=args.season,
        season_id=args.season_id,
        browser_path=args.browser_path,
    )

if __name__ == "__main__":
    main()
