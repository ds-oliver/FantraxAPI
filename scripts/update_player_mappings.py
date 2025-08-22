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
from datetime import datetime
from thefuzz import fuzz
from unidecode import unidecode
import yaml

from fantraxapi.fantrax import FantraxAPI
from fantraxapi.player_mapping import PlayerMapping, PlayerMappingManager

def setup_logging(data_dir: Path) -> None:
    """Set up logging to both file and console."""
    # Create logs directory
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    
    # Create timestamped log file
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = log_dir / f"player_mapping_{timestamp}.log"
    
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler()  # Also log to console
        ]
    )
    
    logging.info(f"Logging to: {log_file}")

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
    # Load team code mappings
    with open(config_dir / "team_mappings.yaml") as f:
        code_mappings = yaml.safe_load(f)
    
    # Create reverse lookup for codes (variation -> standard)
    reverse_code_map = {}
    for standard, variations in code_mappings.items():
        for var in variations:
            reverse_code_map[var] = standard
        # Also map standard code to itself
        reverse_code_map[standard] = standard
        
    # Load club team mappings
    with open(config_dir / "club_team_mappings.yaml") as f:
        club_mappings = yaml.safe_load(f)
        
    # Create reverse lookup for club names (any name -> standard code)
    reverse_club_map = {}
    for code, data in club_mappings.items():
        # Add all possible names for this club
        names = [
            data["long_name"],
            data["short_name"],
            *(data.get("long_name_variations", [])),
            *(data.get("short_name_variations", [])),
            *(data.get("nicknames", []))
        ]
        for name in names:
            reverse_club_map[name.lower()] = code
            
    return reverse_code_map, reverse_club_map

def load_ffscout_data(data_dir: Path) -> pd.DataFrame:
    """Load the most recent FFScout data."""
    files = list(data_dir.glob("scout_picks_rosters_*.parquet"))
    if not files:
        return pd.DataFrame()
    latest = max(files, key=lambda p: p.stat().st_mtime)
    return pd.read_parquet(latest)

def load_sofascore_data(data_dir: Path, code_mappings: dict, club_mappings: dict, force_refresh: bool = False) -> pd.DataFrame:
    """
    Load SofaScore player data from cache or API.
    
    Args:
        data_dir: Base data directory
        code_mappings: Dict mapping team codes to standard codes
        club_mappings: Dict mapping team names to standard codes
        force_refresh: If True, ignore cache and fetch fresh data
    """
    # Ensure data directories exist
    data_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = data_dir / "silver" / "sofascore"
    cache_dir.mkdir(parents=True, exist_ok=True)
    print(f"Using cache directory: {cache_dir}")
    cache_files = list(cache_dir.glob("sofascore_players_*.parquet"))
    
    # Check cache unless force refresh is requested
    if not force_refresh and cache_files:
        # Use most recent cache file
        latest = max(cache_files, key=lambda p: p.stat().st_mtime)
        # Check if cache is fresh (less than 8 hours old)
        if time.time() - latest.stat().st_mtime < 8 * 60 * 60:
            print(f"Loading SofaScore data from cache: {latest.name}")
            df = pd.read_parquet(latest)
            print(f"Loaded {len(df)} players from cache")
            return df
    
    # Premier League season ID for current season
    SEASON_ID = 76986  # 2023-24 season
    
    # API configuration
    base_url = "https://www.sofascore.com/api/v1/unique-tournament/17/season"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
        "Accept": "*/*",
        "Referer": "https://www.sofascore.com/tournament/football/england/premier-league/17",
        "Cache-Control": "no-cache",
        "x-requested-with": "b548fe"
    }
    
    try:
        players = []
        print("Fetching SofaScore data from API...")
        page = 1
        
        while True:
            # Calculate offset for pagination
            offset = (page - 1) * 20
            
            # Build URL with pagination and sorting
            url = f"{base_url}/{SEASON_ID}/statistics"
            params = {
                "limit": 20,
                "offset": offset,
                "order": "-rating",  # Sort by rating descending
                "accumulation": "total",
                "group": "summary"
            }
            
            # Make request
            response = requests.get(url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            
            # Extract player data from this page
            for player in data.get("results", []):
                # Get team name and standardize it immediately
                team_name = player["team"]["name"]
                std_team = standardize_team(team_name, code_mappings, club_mappings)
                
                # Create player record
                player_record = {
                    "player_id": int(player["player"]["id"]),  # Convert to Python int
                    "player_name": player["player"]["name"],
                    "team_name": team_name,  # Keep original for reference
                    "team_code": std_team,  # Already standardized
                }
                players.append(player_record)
            
            # Show progress
            if page % 5 == 0:
                print(f"Processed {page} pages...")
            
            # Check if we've reached the last page
            if page >= data.get("pages", 1):
                print(f"Completed {page} pages")
                break
                
            page += 1
            
            # Add a small delay to avoid rate limiting
            time.sleep(0.5)
        
        print(f"\nLoaded {len(players)} players")
        df = pd.DataFrame(players)
        
        # Standardize team codes using the passed in mappings
        df["team_code"] = df["team_code"].apply(lambda x: standardize_team(x, code_mappings, club_mappings))
        
        # Save to cache
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        cache_file = cache_dir / f"sofascore_players_{timestamp}.parquet"
        df.to_parquet(cache_file)
        logging.info(f"Saved SofaScore data to cache: {cache_file.name}")
        
        # Also save as CSV for easy viewing
        csv_file = cache_dir / f"sofascore_players_{timestamp}.csv"
        df.to_csv(csv_file, index=False)
        logging.info(f"Saved SofaScore data to CSV: {csv_file.name}")
        
        return df
    except Exception as e:
        logging.error(f"Error loading SofaScore data: {e}")
        return pd.DataFrame()

def extract_name_and_info(full_str: str) -> tuple[str, str, str, str]:
    """
    Extract name, team, and position info from strings like 'Name (TEAM - POS)'.
    Returns:
        Tuple of (name, team, position, original_info)
    """
    if not full_str:
        return "", "", "", ""
    
    parts = full_str.split("(", 1)
    name = parts[0].strip()
    info = parts[1].strip(")").strip() if len(parts) > 1 else ""
    
    # Split info into team and position if possible
    team = ""
    position = ""
    if " - " in info:
        team, position = info.split(" - ", 1)
    else:
        team = info
        
    return name, team.strip(), position.strip(), info

def normalize_name(name: str, remove_accents: bool = False) -> str:
    """
    Normalize a player name for comparison.
    
    Args:
        name: Name to normalize
        remove_accents: Whether to remove accents (é -> e, etc.)
    """
    if not name:
        return ""
        
    # Extract just the name part if it includes team/position
    name, _, _, _ = extract_name_and_info(name)
    
    # Convert to lowercase
    name = name.lower()
    
    # Remove accents if requested
    if remove_accents:
        name = unidecode(name)
    
    # Remove special characters (but keep accents if not removed)
    name = name.replace("-", " ").replace("'", "").replace(".", " ")
    
    # Remove extra whitespace
    name = " ".join(name.split())
    
    return name

def standardize_team(team: str, code_mappings: dict, club_mappings: dict) -> str:
    """
    Convert a team name or code to standard code.
    Example: 'TOTTENHAM HOTSPUR' -> 'TOT'
    """
    if not team:
        return ""
        
    team = team.strip().lower()
    
    # Try exact matches first
    for code, data in code_mappings.items():
        if isinstance(data, dict):  # New format
            variations = [v.lower() for v in data.get("variations", [])]
            if team in variations or team == code.lower():
                return code
    
    # Try club mappings with exact matches
    for code, data in club_mappings.items():
        if isinstance(data, dict):
            # Check all possible variations
            all_variations = [
                data.get("long_name", "").lower(),
                data.get("short_name", "").lower(),
                code.lower(),  # The code itself
                *[v.lower() for v in data.get("long_name_variations", [])],
                *[v.lower() for v in data.get("short_name_variations", [])],
                *[v.lower() for v in data.get("nicknames", [])]
            ]
            if team in all_variations:
                return code
                
    # Try fuzzy matching for long team names
    if len(team) > 10:  # Only for longer names
        best_score = 0
        best_code = None
        for code, data in club_mappings.items():
            if isinstance(data, dict):
                # Try matching against long name and variations
                names_to_try = [
                    data.get("long_name", "").lower(),
                    *[v.lower() for v in data.get("long_name_variations", [])]
                ]
                for name in names_to_try:
                    if name:
                        score = fuzz.ratio(team, name)
                        if score > best_score and score >= 90:  # Must be very close match
                            best_score = score
                            best_code = code
        if best_code:
            return best_code
            
    # Special handling for Manchester teams
    if team.startswith("manchester "):
        if "city" in team or "mnc" in team or "mci" in team:
            return "MCI"
        if "united" in team or "utd" in team or "mun" in team:
            return "MUN"
    
    # If no match found, try to extract a 3-letter code
    if len(team.split()) > 1:
        # For multi-word names, try to use a meaningful part
        words = team.split()
        if len(words) >= 2 and words[0] in ["west", "east", "north", "south"]:
            return words[1][:3].upper()  # e.g., "west ham" -> "HAM"
        return words[0][:3].upper()  # e.g., "crystal palace" -> "CRY"
    
    # For single words, just take first 3 letters
    return team[:3].upper()

def find_matches(
    name: str, 
    team_code: str,
    candidates: list[tuple[str, str]],  # List of (name, team_code) tuples
    code_mappings: dict,
    club_mappings: dict,
    threshold: int = 75
) -> list[tuple[str, int, str, str]]:
    """
    Find potential matches for a name among candidates.
    
    Args:
        name: Player name to match
        team_code: Player's team code
        candidates: List of (name, team_code) tuples
        code_mappings: Dict mapping team codes to standard codes
        club_mappings: Dict mapping team names to standard codes
        threshold: Minimum match score
    
    Returns:
        List of tuples (candidate_name, score, display_info, team_code) sorted by score descending
    """
    matches = []
    name_only, team, pos, _ = extract_name_and_info(name)
    norm_name = normalize_name(name_only)
    norm_name_no_accents = normalize_name(name_only, remove_accents=True)
    
    # Standardize the player's team code
    std_team = standardize_team(team_code or team, code_mappings, club_mappings)
    
    for candidate, cand_team in candidates:
        cand_name, cand_team_info, cand_pos, cand_orig_info = extract_name_and_info(candidate)
        norm_cand = normalize_name(cand_name)
        norm_cand_no_accents = normalize_name(cand_name, remove_accents=True)
        
        # Standardize candidate's team code
        std_cand_team = standardize_team(cand_team or cand_team_info, code_mappings, club_mappings)
        
        # Try exact matches first (with and without accents)
        if norm_name == norm_cand:
            base_score = 100
        elif norm_name_no_accents == norm_cand_no_accents:
            base_score = 99
        else:
            # Skip if names are too different in length
            len_diff = abs(len(norm_name) - len(norm_cand))
            if len_diff > 5:  # Names shouldn't differ by more than 5 chars
                continue
                
            # Try different fuzzy matching algorithms
            scores = [
                fuzz.ratio(norm_name, norm_cand) * 1.0,  # Exact character matching
                fuzz.token_sort_ratio(norm_name, norm_cand) * 0.9,  # Word order independent
                fuzz.token_set_ratio(norm_name, norm_cand) * 0.8,  # Partial word matching
            ]
            base_score = max(scores)
            
            # Apply length penalty
            if len_diff > 0:
                base_score = base_score * (1 - len_diff * 0.05)  # 5% penalty per char difference
        
        # Check team match
        if std_team == std_cand_team:
            # Same team - boost the score to 100 if it's already high
            if base_score >= 90:
                base_score = 100
            elif base_score >= 80:
                base_score = base_score * 1.2  # 20% bonus for team match
        else:
            # Different teams - heavy penalty
            base_score = base_score * 0.5  # 50% penalty for team mismatch
            
        final_score = int(base_score)
        if final_score >= threshold:
            # Create standardized display string
            display_name = cand_name
            display_info = f"{std_cand_team}"  # Use standardized team code
            if cand_pos:
                display_info += f" - {cand_pos}"
            matches.append((display_name, final_score, display_info, std_cand_team))
            
    # Sort by score descending
    matches.sort(key=lambda x: x[1], reverse=True)
    return matches

def update_mappings(
    league_id: str,
    data_dir: Path,
    output_file: Path,
    cookie_file: str = "fantraxloggedin.cookie",
    interactive: bool = True,
    config_dir: Path = Path("config"),
    force_refresh: bool = False
) -> None:
    """
    Update player mappings from various sources.
    
    Args:
        league_id: Fantrax league ID
        data_dir: Directory containing data files
        output_file: Where to save the mappings YAML
        cookie_file: Path to Fantrax cookie file
        interactive: Whether to prompt for confirmation on uncertain matches
        config_dir: Directory containing configuration files
        force_refresh: Whether to force refresh of SofaScore data
    """
    # Set up logging
    setup_logging(data_dir)
    # Initialize session with saved cookies
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
        
    # Load existing mappings
    manager = PlayerMappingManager(str(output_file))
    logging.info(f"Initialized player mapping manager with {output_file}")
    
    # Get Fantrax players (source of truth)
    logging.info("Fetching players from Fantrax...")
    fantrax_players = load_fantrax_players(league_id, session)
    logging.info(f"Found {len(fantrax_players)} players in Fantrax")
    
    # Load team mappings
    code_mappings, club_mappings = load_team_mappings(config_dir)
    logging.info("Loaded team mappings from config files")
    
    # Load FFScout data if available
    ffscout_df = load_ffscout_data(data_dir / "silver/scout_picks")
    if not ffscout_df.empty:
        logging.info(f"Loaded FFScout data with {len(ffscout_df)} players")
    else:
        logging.warning("No FFScout data found")
        
    # Load SofaScore data
    sofascore_df = load_sofascore_data(data_dir, code_mappings, club_mappings, force_refresh)
    if not sofascore_df.empty:
        logging.info(f"Found {len(sofascore_df)} players in SofaScore data")
    else:
        logging.warning("No SofaScore data found")
    
    # Track statistics
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
    
    # List to collect players needing manual matching
    unmatched_players = []
    
    logging.info("\nPhase 1: Processing exact matches...")
    logging.info("Note: FFScout data only includes likely starters, while Fantrax includes all squad players.")
    logging.info("Many players without FFScout matches is expected and normal.\n")
    
    # Process each Fantrax player
    for i, player in enumerate(fantrax_players, 1):
        # Show progress every 50 players
        if i % 50 == 0:
            logging.info(f"Processed {i}/{stats['total_players']} players...")
        
        # Check if we already have a mapping
        mapping = manager.get_by_fantrax_id(player.id)
        if mapping:
            # Update Fantrax info
            mapping.fantrax_name = player.name
            stats["existing_mappings"] += 1
            # Skip if we already have both FFScout and SofaScore matches
            if mapping.ffscout_name:
                stats["ffscout_matches"] += 1
            if mapping.sofascore_id:
                stats["sofascore_matches"] += 1
            if mapping.ffscout_name and mapping.sofascore_id:
                continue
        else:
            # Create new mapping
            mapping = PlayerMapping(
                fantrax_id=player.id,
                fantrax_name=player.name,
                sofascore_id=None,
                sofascore_name=None,
                ffscout_name=None,
                other_names=[]
            )
            stats["new_mappings"] += 1
            
        # This player needs FFScout matching
        stats["no_ffscout_match"] += 1
        
        # Add name variations
        if player.first_name and player.last_name:
            mapping.other_names.extend([
                f"{player.first_name} {player.last_name}",
                f"{player.last_name}, {player.first_name}",
                player.last_name,
                f"{player.first_name[0]}. {player.last_name}"
            ])
        
        # Try to match with FFScout data
        if not ffscout_df.empty and not mapping.ffscout_name:
            # Create list of (name, team_code) tuples for matching
            ffscout_candidates = []
            for _, row in ffscout_df.iterrows():
                if row["player_full_from_title"]:
                    ffscout_candidates.append((row["player_full_from_title"], row["team_code"]))
                if row["player_display"]:
                    ffscout_candidates.append((row["player_display"], row["team_code"]))
            
            # Try to find all potential matches
            matches = find_matches(
                player.name,
                player.team.lower(),
                ffscout_candidates,
                code_mappings, club_mappings,
                threshold=70  # Lower threshold to see more potential matches
            )
            
            # Process matches
            if matches:
                best_score = matches[0][1]
                if best_score >= 95:
                    # Found a confident match
                    mapping.ffscout_name = matches[0][0]
                    stats["ffscout_matches"] += 1
                    stats["ffscout_exact_matches"] += 1
                    stats["no_ffscout_match"] -= 1  # Remove from unmatched count
                    logging.info(f"Exact FFScout match found: {player.name} ({player.team}) -> {matches[0][0]} ({matches[0][3]}) [score: {matches[0][1]}]")
                else:
                    # No exact match found, collect for manual matching if score >= 75
                    matches_for_review = [m for m in matches if m[1] >= 75]
                    if matches_for_review:
                        unmatched_players.append((player, mapping, ("ffscout", matches_for_review)))
                    else:
                        unmatched_players.append((player, mapping, ("ffscout", [])))
                    
            if mapping.ffscout_name:
                # Add FFScout display name as alternate if different
                display_names = ffscout_df[
                    ffscout_df["player_full_from_title"] == mapping.ffscout_name
                ]["player_display"]
                if not display_names.empty:
                    display_name = display_names.iloc[0]
                    if display_name and display_name != mapping.ffscout_name:
                        mapping.other_names.append(display_name)
                        
        # Try to match with SofaScore data
        if not sofascore_df.empty and not mapping.sofascore_id:
            # Create list of (name, team_code) tuples for matching
            sofascore_candidates = []
            for _, row in sofascore_df.iterrows():
                sofascore_candidates.append((row["player_name"], row["team_code"]))
            
            # Try to find all potential matches
            matches = find_matches(
                player.name,
                player.team.lower(),
                sofascore_candidates,
                code_mappings, club_mappings,
                threshold=70  # Lower threshold to see more potential matches
            )
            
            # Process matches
            if matches:
                best_score = matches[0][1]
                if best_score >= 95:
                    # Found a confident match
                    match_name = matches[0][0]
                    # Get SofaScore ID for the matched name
                    match_id = int(sofascore_df[sofascore_df["player_name"] == match_name]["player_id"].iloc[0])
                    mapping.sofascore_id = match_id
                    mapping.sofascore_name = match_name
                    stats["sofascore_matches"] += 1
                    stats["sofascore_exact_matches"] += 1
                    stats["no_sofascore_match"] -= 1  # Remove from unmatched count
                    logging.info(f"Exact SofaScore match found: {player.name} ({player.team}) -> {match_name} ({matches[0][3]}) [score: {matches[0][1]}, id: {match_id}]")
                else:
                    # No exact match found, collect for manual matching if score >= 75
                    matches_for_review = [m for m in matches if m[1] >= 75]
                    if matches_for_review:
                        unmatched_players.append((player, mapping, ("sofascore", matches_for_review)))
                    else:
                        unmatched_players.append((player, mapping, ("sofascore", [])))
        
        # Remove duplicates from other names
        mapping.other_names = list(set(mapping.other_names))
        
        # Add to manager
        manager.add_mapping(mapping)
        
    # Phase 2: Manual matching for remaining players
    if unmatched_players and interactive:
        logging.info("\nPhase 2: Manual matching for remaining players...")
        logging.info(f"Found {len(unmatched_players)} players without exact matches")
        logging.info("Only showing potential matches with score >= 75")
        
        for player, mapping, (source, matches) in unmatched_players:
            if not matches:  # Skip if no potential matches found in phase 1
                continue
                
            # Use the matches we already found in phase 1
            if matches:
                # Standardize player's team for display
                std_player_team = standardize_team(player.team, code_mappings, club_mappings)
                logging.info(f"\nPotential {source.title()} matches for {player.name} ({std_player_team} - {player.position}):")
                for i, (match_name, score, display_info, std_team) in enumerate(matches[:5], 1):
                    # Always show standardized team code
                    print(f"  {i}. {match_name} ({std_team}) [score: {score}]")  # Keep print for interactive UI
                print("  0. None of these")  # Keep print for interactive UI
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
                                    logging.info(f"Manual FFScout match accepted: {player.name} ({player.team}) -> {match_name} ({matches[choice-1][3]}) [score: {matches[choice-1][1]}]")
                                else:  # sofascore
                                    match_id = int(sofascore_df[sofascore_df["player_name"] == match_name]["player_id"].iloc[0])
                                    mapping.sofascore_id = match_id
                                    mapping.sofascore_name = match_name
                                    stats["sofascore_matches"] += 1
                                    stats["sofascore_manual_matches"] += 1
                                    stats["no_sofascore_match"] -= 1
                                    logging.info(f"Manual SofaScore match accepted: {player.name} ({player.team}) -> {match_name} ({matches[choice-1][3]}) [score: {matches[choice-1][1]}, id: {match_id}]")
                            break
                    except ValueError:
                        pass
                    logging.warning("Invalid choice. Please enter a number between 0 and 5.")
                    print("Invalid choice. Please enter a number between 0 and 5.")  # Keep print for interactive UI
    
    # Log final statistics
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
    logging.info("Note: Low FFScout match rate is expected as FFScout only includes likely starters")
    logging.info("\nSofaScore matches:")
    logging.info(f"Total matches found: {stats['sofascore_matches']}")
    logging.info(f"  - Exact matches: {stats['sofascore_exact_matches']}")
    logging.info(f"  - Manual matches: {stats['sofascore_manual_matches']}")
    logging.info(f"Players without SofaScore match: {stats['no_sofascore_match']}")
    logging.info(f"SofaScore match rate: {stats['sofascore_matches']/stats['total_players']*100:.1f}%")
    logging.info("=" * 50)
    
    # Save all mappings to file
    manager.save_mappings()
    logging.info(f"\nSaved mappings to: {output_file}")

def main():
    # Your Premier League league ID
    LEAGUE_ID = "o90qdw15mc719reh"
    
    parser = argparse.ArgumentParser(description="Update player mappings from various sources")
    parser.add_argument(
        "--data-dir",
        type=str,
        default="/Users/hogan/FantraxAPI/data",
        help="Directory containing data files"
    )
    parser.add_argument(
        "--output",
        type=str,
        default="config/player_mappings.yaml",
        help="Output YAML file for mappings"
    )
    parser.add_argument(
        "--cookie-file",
        type=str,
        default="fantraxloggedin.cookie",
        help="Path to cookie file (default: fantraxloggedin.cookie)"
    )
    parser.add_argument(
        "--noninteractive",
        action="store_true",
        help="Skip interactive prompts for uncertain matches"
    )
    parser.add_argument(
        "--force-refresh",
        action="store_true",
        help="Force refresh of SofaScore data, ignoring cache"
    )
    args = parser.parse_args()
    
    update_mappings(
        LEAGUE_ID,
        Path(args.data_dir),
        Path(args.output),
        args.cookie_file,
        not args.noninteractive,
        force_refresh=args.force_refresh
    )

if __name__ == "__main__":
    main()
