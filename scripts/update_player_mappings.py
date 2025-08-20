#!/usr/bin/env python
"""
Script to update player mappings from various sources.
"""
import argparse
import pickle
from pathlib import Path
import pandas as pd
import requests
from thefuzz import fuzz
from unidecode import unidecode
import yaml

from fantraxapi.fantrax import FantraxAPI
from fantraxapi.player_mapping import PlayerMapping, PlayerMappingManager

def load_fantrax_players(league_id: str, session: requests.Session) -> list:
    """Get all players from Fantrax."""
    api = FantraxAPI(league_id, session=session)
    return api.get_all_players()

def load_team_mappings(config_dir: Path) -> dict:
    """Load team code mappings."""
    with open(config_dir / "team_mappings.yaml") as f:
        mappings = yaml.safe_load(f)
    
    # Create reverse lookup (variation -> standard)
    reverse_map = {}
    for standard, variations in mappings.items():
        for var in variations:
            reverse_map[var] = standard
        # Also map standard code to itself
        reverse_map[standard] = standard
    return reverse_map

def load_ffscout_data(data_dir: Path) -> pd.DataFrame:
    """Load the most recent FFScout data."""
    files = list(data_dir.glob("scout_picks_rosters_*.parquet"))
    if not files:
        return pd.DataFrame()
    latest = max(files, key=lambda p: p.stat().st_mtime)
    return pd.read_parquet(latest)

def extract_name_and_info(full_str: str) -> tuple[str, str]:
    """Extract name and team/position info from strings like 'Name (TEAM - POS)'."""
    if not full_str:
        return "", ""
    
    parts = full_str.split("(", 1)
    name = parts[0].strip()
    info = parts[1].strip(")").strip() if len(parts) > 1 else ""
    return name, info

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

def find_matches(
    name: str, 
    team_code: str,
    candidates: list[tuple[str, str]],  # List of (name, team_code) tuples
    team_mappings: dict,
    threshold: int = 75
) -> list[tuple[str, int, str, str]]:
    """
    Find potential matches for a name among candidates.
    
    Args:
        name: Player name to match
        team_code: Player's team code
        candidates: List of (name, team_code) tuples
        team_mappings: Dict mapping team codes to standard codes
        threshold: Minimum match score
    
    Returns:
        List of tuples (candidate_name, score, info, team_code) sorted by score descending
    """
    matches = []
    name_only, name_info = extract_name_and_info(name)
    norm_name = normalize_name(name_only)
    norm_name_no_accents = normalize_name(name_only, remove_accents=True)
    
    # Standardize the player's team code
    std_team = team_mappings.get(team_code.lower(), team_code.lower())
    
    for candidate, cand_team in candidates:
        cand_name, cand_info = extract_name_and_info(candidate)
        norm_cand = normalize_name(cand_name)
        norm_cand_no_accents = normalize_name(cand_name, remove_accents=True)
        
        # Standardize candidate's team code
        std_cand_team = team_mappings.get(cand_team.lower(), cand_team.lower())
        
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
            matches.append((candidate, final_score, cand_info, cand_team))
            
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
    team_mappings = load_team_mappings(config_dir)
    print("Loaded team code mappings")
    
    # Load FFScout data if available
    ffscout_df = load_ffscout_data(data_dir / "silver/scout_picks")
    if not ffscout_df.empty:
        print(f"Loaded FFScout data with {len(ffscout_df)} players")
    
    # Track statistics
    stats = {
        "total_players": len(fantrax_players),
        "existing_mappings": 0,
        "new_mappings": 0,
        "ffscout_matches": 0,
        "no_ffscout_match": 0,
        "exact_matches": 0,
        "manual_matches": 0
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
            # Only continue if we already have an FFScout match
            if mapping.ffscout_name:
                stats["ffscout_matches"] += 1
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
        
        # Add name variations
        if player.first_name and player.last_name:
            mapping.other_names.extend([
                f"{player.first_name} {player.last_name}",
                f"{player.last_name}, {player.first_name}",
                player.last_name,
                f"{player.first_name[0]}. {player.last_name}"
            ])
        
        # Try to match with FFScout data
        if not ffscout_df.empty:
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
                team_mappings,
                threshold=70  # Lower threshold to see more potential matches
            )
            
            # Print all potential matches during phase 1
            if matches:
                best_score = matches[0][1]
                print(f"\nChecking {player.name} ({player.team}):")
                print(f"Found {len(matches)} potential matches:")
                for match_name, score, _, team_code in matches[:5]:
                    std_team = team_mappings.get(team_code.lower(), team_code.lower())
                    print(f"  - {match_name} ({team_code.upper()}) [score: {score}, std team: {std_team.upper()}]")
                
                if best_score >= 95:
                    # Found a confident match
                    print("  -> Auto-matching with highest score")
                    mapping.ffscout_name = matches[0][0]
                    stats["ffscout_matches"] += 1
                    stats["exact_matches"] += 1
                else:
                    # No exact match found, collect for manual matching if score >= 70
                    matches_for_review = [m for m in matches if m[1] >= 70]
                    if matches_for_review:
                        print("  -> Collecting for manual review")
                        unmatched_players.append((player, mapping, matches_for_review))
                    else:
                        unmatched_players.append((player, mapping, []))
                    stats["no_ffscout_match"] += 1
                    
            if mapping.ffscout_name:
                # Add FFScout display name as alternate if different
                display_names = ffscout_df[
                    ffscout_df["player_full_from_title"] == mapping.ffscout_name
                ]["player_display"]
                if not display_names.empty:
                    display_name = display_names.iloc[0]
                    if display_name and display_name != mapping.ffscout_name:
                        mapping.other_names.append(display_name)
        
        # Remove duplicates from other names
        mapping.other_names = list(set(mapping.other_names))
        
        # Add to manager
        manager.add_mapping(mapping)
        
    # Phase 2: Manual matching for remaining players
    if unmatched_players and interactive:
        print("\nPhase 2: Manual matching for remaining players...")
        print(f"Found {len(unmatched_players)} players without exact matches")
        print("Only showing potential matches with score >= 75")
        
        for player, mapping, matches in unmatched_players:
            if not matches:  # Skip if no potential matches found in phase 1
                continue
                
            # Use the matches we already found in phase 1
            if matches:
                print(f"\nPotential FFScout matches for {player.name} ({player.team} - {player.position}):")
                for i, (match_name, score, _, team_code) in enumerate(matches[:5], 1):
                    std_team = team_mappings.get(team_code.lower(), team_code.lower())
                    print(f"  {i}. {match_name} ({team_code.upper()}) [score: {score}, std team: {std_team.upper()}]")
                print("  0. None of these")
                print("\nNote: It's normal if none match - FFScout only includes likely starters")
                
                while True:
                    try:
                        choice = input("Select match number (0-5): ").strip()
                        if not choice:
                            break
                        choice = int(choice)
                        if 0 <= choice <= len(matches[:5]):
                            if choice > 0:
                                mapping.ffscout_name = matches[choice-1][0]
                                stats["ffscout_matches"] += 1
                                stats["manual_matches"] += 1
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
    print(f"FFScout matches found: {stats['ffscout_matches']}")
    print(f"  - Exact matches: {stats['exact_matches']}")
    print(f"  - Manual matches: {stats['manual_matches']}")
    print(f"Players without FFScout match: {stats['no_ffscout_match']}")
    print(f"FFScout match rate: {stats['ffscout_matches']/stats['total_players']*100:.1f}%")
    print("Note: Low FFScout match rate is expected as FFScout only includes likely starters")
    print("=" * 50)
    print(f"\nSaved mappings to: {output_file}")

def main():
    parser = argparse.ArgumentParser(description="Update player mappings from various sources")
    parser.add_argument(
        "--league-id",
        type=str,
        required=True,
        help="Fantrax league ID"
    )
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
        args.league_id,
        Path(args.data_dir),
        Path(args.output),
        args.cookie_file,
        not args.noninteractive
    )

if __name__ == "__main__":
    main()
