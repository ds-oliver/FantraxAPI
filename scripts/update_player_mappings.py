#!/usr/bin/env python
"""
Script to update player mappings from various sources.
"""
import argparse
import pickle
from pathlib import Path
import pandas as pd
import requests
import time
from thefuzz import fuzz
from unidecode import unidecode
import yaml

from fantraxapi.fantrax import FantraxAPI
from fantraxapi.player_mapping import PlayerMapping, PlayerMappingManager

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

def load_sofascore_data() -> pd.DataFrame:
    """Load SofaScore player data from their API."""
    # Premier League season ID for 2023-24
    SEASON_ID = 76986  # Updated to correct season ID
    
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
                players.append({
                    "player_id": int(player["player"]["id"]),  # Convert to Python int
                    "player_name": player["player"]["name"],
                    "team_name": player["team"]["name"]
                })
            
            # Check if we've reached the last page
            if page >= data.get("pages", 1):
                break
                
            page += 1
            
            # Add a small delay to avoid rate limiting
            time.sleep(0.5)
        
        print(f"Loaded {len(players)} players from {page} pages")
        return pd.DataFrame(players)
    except Exception as e:
        print(f"Error loading SofaScore data: {e}")
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
    name, _ = extract_name_and_info(name)
    
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
    team = team.strip().lower()
    # Try code mappings first
    std_team = code_mappings.get(team, team)
    # If no match in code mappings, try club mappings
    if std_team == team:
        std_team = club_mappings.get(team, team)
    return std_team.upper()

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
        
        # Apply team mismatch penalty
        if std_team != std_cand_team:
            base_score = base_score * 0.8  # 20% penalty for team mismatch
            
        final_score = int(base_score)
        if final_score >= threshold:
            # Create display info with standardized team code and original position
            display_info = f"{std_cand_team}"
            if cand_pos:
                display_info += f" - {cand_pos}"
            matches.append((candidate, final_score, display_info, std_cand_team))
            
    # Sort by score descending
    matches.sort(key=lambda x: x[1], reverse=True)
    return matches

def update_mappings(
    league_id: str,
    data_dir: Path,
    output_file: Path,
    cookie_file: str = "fantraxloggedin.cookie",
    interactive: bool = True,
    config_dir: Path = Path("config")
) -> None:
    """
    Update player mappings from various sources.
    
    Args:
        league_id: Fantrax league ID
        data_dir: Directory containing data files
        output_file: Where to save the mappings YAML
        cookie_file: Path to Fantrax cookie file
        interactive: Whether to prompt for confirmation on uncertain matches
    """
    # Initialize session with saved cookies
    session = requests.Session()
    try:
        with open(cookie_file, "rb") as f:
            cookies = pickle.load(f)
            for cookie in cookies:
                session.cookies.set(cookie["name"], cookie["value"])
    except FileNotFoundError:
        print(f"Cookie file not found: {cookie_file}")
        print("Please run bootstrap_cookie.py first")
        return
        
    # Load existing mappings
    manager = PlayerMappingManager(str(output_file))
    
    # Get Fantrax players (source of truth)
    print("Fetching players from Fantrax...")
    fantrax_players = load_fantrax_players(league_id, session)
    print(f"Found {len(fantrax_players)} players in Fantrax")
    
    # Load team mappings
    code_mappings, club_mappings = load_team_mappings(config_dir)
    print("Loaded team mappings")
    
    # Load FFScout data if available
    ffscout_df = load_ffscout_data(data_dir / "silver/scout_picks")
    if not ffscout_df.empty:
        print(f"Loaded FFScout data with {len(ffscout_df)} players")
        
    # Load SofaScore data
    sofascore_df = load_sofascore_data()
    if not sofascore_df.empty:
        print(f"Loaded SofaScore data with {len(sofascore_df)} players")
    
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
    
    print("\nPhase 1: Processing exact matches...")
    print("Note: FFScout data only includes likely starters, while Fantrax includes all squad players.")
    print("Many players without FFScout matches is expected and normal.\n")
    
    # Process each Fantrax player
    for i, player in enumerate(fantrax_players, 1):
        # Show progress every 50 players
        if i % 50 == 0:
            print(f"Processed {i}/{stats['total_players']} players...")
        
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
            # Create list of (name, team_name) tuples for matching
            sofascore_candidates = []
            for _, row in sofascore_df.iterrows():
                sofascore_candidates.append((row["player_name"], row["team_name"]))
            
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
        print("\nPhase 2: Manual matching for remaining players...")
        print(f"Found {len(unmatched_players)} players without exact matches")
        print("Only showing potential matches with score >= 75")
        
        for player, mapping, (source, matches) in unmatched_players:
            if not matches:  # Skip if no potential matches found in phase 1
                continue
                
            # Use the matches we already found in phase 1
            if matches:
                # Standardize player's team for display
                std_player_team = standardize_team(player.team, code_mappings, club_mappings)
                print(f"\nPotential {source.title()} matches for {player.name} ({std_player_team} - {player.position}):")
                for i, (match_name, score, display_info, std_team) in enumerate(matches[:5], 1):
                    print(f"  {i}. {match_name} ({display_info}) [score: {score}]")
                print("  0. None of these")
                if source == "ffscout":
                    print("\nNote: It's normal if none match - FFScout only includes likely starters")
                
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
                                else:  # sofascore
                                    match_id = int(sofascore_df[sofascore_df["player_name"] == match_name]["player_id"].iloc[0])
                                    mapping.sofascore_id = match_id
                                    mapping.sofascore_name = match_name
                                    stats["sofascore_matches"] += 1
                                    stats["sofascore_manual_matches"] += 1
                                    stats["no_sofascore_match"] -= 1
                            break
                    except ValueError:
                        pass
                    print("Invalid choice. Please enter a number between 0 and 5.")
    
    # Print final statistics
    print("\nProcessing complete!")
    print("=" * 50)
    print("Statistics:")
    print(f"Total Fantrax players: {stats['total_players']}")
    print(f"Existing mappings updated: {stats['existing_mappings']}")
    print(f"New mappings created: {stats['new_mappings']}")
    print("\nFFScout matches:")
    print(f"Total matches found: {stats['ffscout_matches']}")
    print(f"  - Exact matches: {stats['ffscout_exact_matches']}")
    print(f"  - Manual matches: {stats['ffscout_manual_matches']}")
    print(f"Players without FFScout match: {stats['no_ffscout_match']}")
    print(f"FFScout match rate: {stats['ffscout_matches']/stats['total_players']*100:.1f}%")
    print("Note: Low FFScout match rate is expected as FFScout only includes likely starters")
    print("\nSofaScore matches:")
    print(f"Total matches found: {stats['sofascore_matches']}")
    print(f"  - Exact matches: {stats['sofascore_exact_matches']}")
    print(f"  - Manual matches: {stats['sofascore_manual_matches']}")
    print(f"Players without SofaScore match: {stats['no_sofascore_match']}")
    print(f"SofaScore match rate: {stats['sofascore_matches']/stats['total_players']*100:.1f}%")
    print("=" * 50)
    
    # Save all mappings to file
    manager.save_mappings()
    print(f"\nSaved mappings to: {output_file}")

def main():
    # Your Premier League league ID
    LEAGUE_ID = "o90qdw15mc719reh"
    
    parser = argparse.ArgumentParser(description="Update player mappings from various sources")
    parser.add_argument(
        "--data-dir",
        type=str,
        default="data",
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
    args = parser.parse_args()
    
    update_mappings(
        LEAGUE_ID,
        Path(args.data_dir),
        Path(args.output),
        args.cookie_file,
        not args.noninteractive
    )

if __name__ == "__main__":
    main()
