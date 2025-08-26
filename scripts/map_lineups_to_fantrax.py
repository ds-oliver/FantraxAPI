# map_lineups_to_fantrax.py
import os, json, csv, unicodedata, argparse
from pathlib import Path
from rapidfuzz import fuzz, process
from fantraxapi import FantraxAPI
import pandas as pd
from typing import Dict, List, Optional, Tuple
import yaml

def canon(s: str) -> str:
	if not s: return ""
	s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
	s = s.lower().replace("-", " ").replace(".", " ")
	# Remove suffixes in order (longest first to avoid partial matches)
	for suf in [" iii", " iv", " ii", " sr", " jr"]:
		s = s.replace(suf, "")
	return " ".join(s.split())

def load_fantrax_players(fx: FantraxAPI) -> pd.DataFrame:
	"""Load all Fantrax players and create canonical names for matching."""
	try:
		players = fx.get_all_players()	# Returns List[Player] objects
		if not players:
			print("No players returned from Fantrax API")
			return pd.DataFrame()
		
		# Convert Player objects to dict format for DataFrame
		player_data = []
		for player in players:
			player_data.append({
				"id": player.id,
				"name": player.name,
				"team": player.team,
				"pos": player.position,
				"positions": player.positions,
				"status": player.status,
				"injury_status": player.injury_status
			})
		
		df = pd.DataFrame(player_data)
		df["name_canon"] = df["name"].map(canon)
		return df
	except Exception as e:
		print(f"Error loading Fantrax players: {e}")
		return pd.DataFrame()

def load_sofa_event_lineup(base: Path, event_id: int, schedule_df: pd.DataFrame = None) -> pd.DataFrame:
	"""Load and parse a SofaScore lineup file into a structured DataFrame with team context."""
	try:
		j = json.loads(Path(base / "lineups" / f"{event_id}.json").read_text())
		
		# Get team context from schedule if available
		team_context = {}
		if schedule_df is not None:
			event_row = schedule_df[schedule_df['event_id'] == event_id]
			if not event_row.empty:
				row = event_row.iloc[0]
				team_context = {
					'home': {
						'name': row.get('home_team', ''),
						'id': row.get('home_team_id', ''),
						'side': 'home'
					},
					'away': {
						'name': row.get('away_team', ''),
						'id': row.get('away_team_id', ''),
						'side': 'away'
					}
				}
		
		rows = []
		for side in ("home","away"):
			for role in ("starters","subs"):
				for p in (j.get(side,{}).get(role,[]) or []):
					team_info = team_context.get(side, {})
					rows.append({
						"event_id": j["event_id"],
						"team_side": side,
						"team_name": team_info.get('name', ''),
						"team_id": team_info.get('id', ''),
						"role": role[:-1],	# remove 's' from 'starters'/'subs'
						"sofa_player_id": p.get("id"),
						"sofa_name": p.get("name"),
						"sofa_team_id": p.get("team_id"),
						"sofa_pos": p.get("position"),
						"jersey_number": p.get("jersey_number"),
						"confirmed": j.get("confirmed", False),
					})
		df = pd.DataFrame(rows)
		if not df.empty:
			df["sofa_name_canon"] = df["sofa_name"].map(canon)
		return df
	except Exception as e:
		print(f"Error loading lineup for event {event_id}: {e}")
		return pd.DataFrame()

def load_schedule_data(schedule_dir: Path, tournament_id: int, season_id: int, mode: str = "last") -> pd.DataFrame:
	"""Load schedule data to get team context for lineups."""
	try:
		# Look for schedule file
		schedule_pattern = f"{tournament_id}_{season_id}_{mode}.csv"
		schedule_files = list(schedule_dir.glob(schedule_pattern))
		
		if not schedule_files:
			print(f"No schedule file found matching pattern: {schedule_pattern}")
			return pd.DataFrame()
		
		schedule_file = schedule_files[0]
		schedule_df = pd.read_csv(schedule_file)
		print(f"✓ Loaded schedule from {schedule_file} with {len(schedule_df)} events")
		return schedule_df
	except Exception as e:
		print(f"Error loading schedule data: {e}")
		return pd.DataFrame()

def get_team_lineup_for_event(event_id: int, team_name: str, lineups_dir: Path, schedule_df: pd.DataFrame) -> pd.DataFrame:
	"""Get lineup for a specific team in a specific event."""
	try:
		# Load the full lineup for the event
		full_lineup = load_sofa_event_lineup(lineups_dir.parent, event_id, schedule_df)
		
		if full_lineup.empty:
			return pd.DataFrame()
		
		# Filter for the specific team
		# Try to match by team name (case-insensitive)
		team_lineup = full_lineup[
			full_lineup['team_name'].str.lower() == team_name.lower()
		]
		
		if team_lineup.empty:
			# If no match by name, try to find by team_id if available
			event_row = schedule_df[schedule_df['event_id'] == event_id]
			if not event_row.empty:
				row = event_row.iloc[0]
				if team_name.lower() in row.get('home_team', '').lower():
					team_lineup = full_lineup[full_lineup['team_side'] == 'home']
				elif team_name.lower() in row.get('away_team', '').lower():
					team_lineup = full_lineup[full_lineup['team_side'] == 'away']
		
		return team_lineup
	except Exception as e:
		print(f"Error getting team lineup for event {event_id}, team {team_name}: {e}")
		return pd.DataFrame()

def load_existing_mappings(mappings_path: Path) -> Tuple[Dict[int, str], Dict[str, List[str]]]:
	"""Load existing SofaScore to Fantrax player ID mappings and create alias mappings."""
	if not mappings_path.exists():
		return {}, {}
	
	try:
		with open(mappings_path, 'r') as f:
			mappings = yaml.safe_load(f)
		
		# Convert to dict: sofascore_id -> fantrax_id
		id_mappings = {}
		for m in mappings:
			if ('sofascore_id' in m and 'fantrax_id' in m and 
				m['sofascore_id'] is not None and m['fantrax_id'] is not None):
				try:
					sofa_id = int(m['sofascore_id'])
					fantrax_id = m['fantrax_id']
					id_mappings[sofa_id] = fantrax_id
				except (ValueError, TypeError):
					# Skip entries with invalid IDs
					continue
		
		# Create alias mappings: canonical_name -> [fantrax_ids]
		alias_mappings = {}
		for m in mappings:
			if ('fantrax_id' in m and 'fantrax_name' in m and 
				m['fantrax_id'] is not None and m['fantrax_name'] is not None):
				fantrax_id = m['fantrax_id']
				fantrax_name = m['fantrax_name']
				
				# Add main name
				canon_name = canon(fantrax_name)
				if canon_name not in alias_mappings:
					alias_mappings[canon_name] = []
				alias_mappings[canon_name].append(fantrax_id)
				
				# Add other names/aliases
				if 'other_names' in m and m['other_names']:
					for alias in m['other_names']:
						if alias:
							canon_alias = canon(alias)
							if canon_alias not in alias_mappings:
								alias_mappings[canon_alias] = []
							alias_mappings[canon_alias].append(fantrax_id)
				
				# Add display name if different
				if 'display_name' in m and m['display_name'] and m['display_name'] != fantrax_name:
					canon_display = canon(m['display_name'])
					if canon_display not in alias_mappings:
						alias_mappings[canon_display] = []
					alias_mappings[canon_display].append(fantrax_id)
		
		return id_mappings, alias_mappings
	except Exception as e:
		print(f"Error loading existing mappings: {e}")
		return {}, {}

def suggest_matches(sofa_df: pd.DataFrame, fx_df: pd.DataFrame, 
				   existing_mappings: Dict[int, str], alias_mappings: Dict[str, List[str]]) -> pd.DataFrame:
	"""Generate player matching suggestions using fuzzy matching with priority scoring."""
	out = []
	
	for _, r in sofa_df.iterrows():
		sofa_name_canon = r["sofa_name_canon"]
		
		# Check if we already have a direct ID mapping
		if r["sofa_player_id"] in existing_mappings:
			fantrax_id = existing_mappings[r["sofa_player_id"]]
			fx_row = fx_df[fx_df["id"] == fantrax_id]
			if not fx_row.empty:
				row = fx_row.iloc[0]
				out.append({
					**r.to_dict(),
					"fantrax_id_guess": row["id"],
					"fantrax_name_guess": row["name"],
					"fantrax_team": row.get("team"),
					"fantrax_pos": row.get("pos"),
					"match_score": 100,	 # Perfect match from existing mapping
					"match_source": "existing_mapping",
					"priority": 1  # Highest priority
				})
				continue
		
		# Check if we have alias matches
		if sofa_name_canon in alias_mappings:
			fantrax_ids = alias_mappings[sofa_name_canon]
			# Get the first matching player
			fx_row = fx_df[fx_df["id"] == fantrax_ids[0]]
			if not fx_row.empty:
				row = fx_row.iloc[0]
				out.append({
					**r.to_dict(),
					"fantrax_id_guess": row["id"],
					"fantrax_name_guess": row["name"],
					"fantrax_team": row.get("team"),
					"fantrax_pos": row.get("pos"),
					"match_score": 95,	# High score for alias match
					"match_source": "alias_match",
					"priority": 2  # High priority
				})
				continue
		
		# Fuzzy match on all Fantrax players (no position/team filtering)
		scores = process.extract(sofa_name_canon, fx_df["name_canon"], 
							   scorer=fuzz.token_set_ratio, limit=10)
		
		for name_canon, score, idx in scores:
			row = fx_df.iloc[idx]
			out.append({
				**r.to_dict(),
				"fantrax_id_guess": row["id"],
				"fantrax_name_guess": row["name"],
				"fantrax_team": row.get("team"),
				"fantrax_pos": row.get("pos"),
				"match_score": score,
				"match_source": "fuzzy_match",
				"priority": 3  # Lower priority for fuzzy matches
			})
	
	return pd.DataFrame(out)

def validate_lineup_counts(sofa_df: pd.DataFrame, fx_df: pd.DataFrame) -> Tuple[bool, Dict]:
	"""Validate that we have the same number of players in both systems."""
	if sofa_df.empty:
		return False, {"error": "No SofaScore lineup data"}
	
	if fx_df.empty:
		return False, {"error": "No Fantrax player data"}
	
	# Count players by team and role
	sofa_counts = sofa_df.groupby(['team_side', 'role']).size().to_dict()
	
	# For Fantrax, we'd need to get actual lineup data to compare
	# For now, just check if we have matches for all players
	matched_players = sofa_df[sofa_df['fantrax_id_guess'].notna()]
	unmatched_players = sofa_df[sofa_df['fantrax_id_guess'].isna()]
	
	validation = {
		"total_sofa_players": len(sofa_df),
		"total_matched": len(matched_players),
		"total_unmatched": len(unmatched_players),
		"match_rate": len(matched_players) / len(sofa_df) if len(sofa_df) > 0 else 0,
		"sofa_counts": sofa_counts,
		"unmatched_details": unmatched_players[['sofa_name', 'sofa_pos', 'team_side', 'role']].to_dict('records') if not unmatched_players.empty else []
	}
	
	return len(unmatched_players) == 0, validation

def pick_best_matches(suggestions_df: pd.DataFrame) -> pd.DataFrame:
	"""Pick the best match for each SofaScore player based on priority and score."""
	if suggestions_df.empty:
		return suggestions_df
	
	# Sort by priority (lower is better) then by match_score (higher is better)
	suggestions_df = suggestions_df.sort_values(['priority', 'match_score'], ascending=[True, False])
	
	# Group by sofa_player_id and take the first (best) match
	best_matches = suggestions_df.groupby('sofa_player_id').first().reset_index()
	
	return best_matches

def create_review_csv(suggestions_df: pd.DataFrame, output_path: Path):
	"""Create a review CSV with top 3 candidates per player plus best matches sheet."""
	if suggestions_df.empty:
		return
	
	# Create directory if it doesn't exist
	output_path.parent.mkdir(parents=True, exist_ok=True)
	
	# Get top 3 candidates per player for review
	top_candidates = []
	for sofa_id in suggestions_df['sofa_player_id'].unique():
		player_suggestions = suggestions_df[suggestions_df['sofa_player_id'] == sofa_id].copy()
		player_suggestions = player_suggestions.sort_values(['priority', 'match_score'], ascending=[True, False])
		
		# Get top 3
		top_3 = player_suggestions.head(3)
		
		for i, (_, row) in enumerate(top_3.iterrows()):
			top_candidates.append({
				'sofa_player_id': row['sofa_player_id'],
				'sofa_name': row['sofa_name'],
				'sofa_pos': row['sofa_pos'],
				'team_side': row['team_side'],
				'role': row['role'],
				'candidate_rank': i + 1,
				'fantrax_id': row['fantrax_id_guess'],
				'fantrax_name': row['fantrax_name_guess'],
				'fantrax_team': row['fantrax_team'],
				'fantrax_pos': row['fantrax_pos'],
				'match_score': row['match_score'],
				'match_source': row['match_source'],
				'priority': row['priority']
			})
	
	# Create review CSV
	review_df = pd.DataFrame(top_candidates)
	review_csv_path = output_path / 'lineup_mapping_review.csv'
	review_df.to_csv(review_csv_path, index=False)
	
	# Create best matches sheet
	best_matches = pick_best_matches(suggestions_df)
	best_csv_path = output_path / 'lineup_mapping_best.csv'
	best_matches.to_csv(best_csv_path, index=False)
	
	print(f"✓ Review CSV saved to: {review_csv_path}")
	print(f"✓ Best matches CSV saved to: {best_csv_path}")
	
	return review_df, best_matches

def create_lineup_change_structure(matched_df: pd.DataFrame) -> Dict:
	"""Convert matched lineup data to a structure suitable for Fantrax lineup changes."""
	if matched_df.empty:
		return {}
	
	# Group by team side and role
	lineup_structure = {
		"event_id": matched_df.iloc[0]["event_id"],
		"confirmed": matched_df.iloc[0]["confirmed"],
		"home": {"starters": [], "subs": []},
		"away": {"starters": [], "subs": []}
	}
	
	for _, row in matched_df.iterrows():
		if pd.isna(row["fantrax_id_guess"]):
			continue  # Skip unmatched players
			
		player_info = {
			"fantrax_id": row["fantrax_id_guess"],
			"fantrax_name": row["fantrax_name_guess"],
			"position": row["sofa_pos"],
			"jersey_number": row["jersey_number"],
			"match_score": row["match_score"],
			"match_source": row["match_source"]
		}
		
		side = row["team_side"]
		role = row["role"]
		
		if role == "starter":
			lineup_structure[side]["starters"].append(player_info)
		elif role == "sub":
			lineup_structure[side]["subs"].append(player_info)
	
	return lineup_structure

def create_fantrax_lineup_structure(matched_df: pd.DataFrame, team_name: str) -> Dict:
	"""Create a Fantrax-ready lineup structure for a specific team."""
	if matched_df.empty:
		return {}
	
	# Filter for the specific team
	team_lineup = matched_df[
		matched_df['team_name'].str.lower() == team_name.lower()
	]
	
	if team_lineup.empty:
		return {}
	
	# Create Fantrax lineup structure
	fantrax_lineup = {
		"event_id": team_lineup.iloc[0]["event_id"],
		"team_name": team_name,
		"confirmed": team_lineup.iloc[0]["confirmed"],
		"starters": [],
		"subs": [],
		"unmatched_players": []
	}
	
	for _, row in team_lineup.iterrows():
		if pd.isna(row["fantrax_id_guess"]):
			# Track unmatched players
			fantrax_lineup["unmatched_players"].append({
				"sofa_name": row["sofa_name"],
				"sofa_pos": row["sofa_pos"],
				"role": row["role"]
			})
			continue
			
		player_info = {
			"fantrax_id": row["fantrax_id_guess"],
			"fantrax_name": row["fantrax_name_guess"],
			"sofa_name": row["sofa_name"],
			"sofa_pos": row["sofa_pos"],
			"jersey_number": row["jersey_number"],
			"match_score": row["match_score"],
			"match_source": row["match_source"]
		}
		
		role = row["role"]
		if role == "starter":
			fantrax_lineup["starters"].append(player_info)
		elif role == "sub":
			fantrax_lineup["subs"].append(player_info)
	
	return fantrax_lineup

def main():
	parser = argparse.ArgumentParser(description="Map SofaScore lineups to Fantrax players")
	parser.add_argument("--event-id", type=int, help="Specific event ID to process")
	parser.add_argument("--lineups-dir", type=Path, default=Path("data/sofascore/lineups"), 
					   help="Directory containing lineup JSON files")
	parser.add_argument("--schedules-dir", type=Path, default=Path("data/sofascore/schedules"),
					   help="Directory containing schedule CSV files")
	parser.add_argument("--mappings-file", type=Path, default=Path("config/player_mappings.yaml"),
					   help="Path to player mappings YAML file")
	parser.add_argument("--output-dir", type=Path, default=Path("data/mapped_lineups"),
					   help="Output directory for mapped lineups")
	parser.add_argument("--min-match-score", type=int, default=80,
					   help="Minimum fuzzy match score to consider a match valid")
	parser.add_argument("--validate-only", action="store_true",
					   help="Only validate existing mappings, don't create new ones")
	parser.add_argument("--fantrax-config", type=Path, default=Path("config.ini"),
					   help="Path to Fantrax config file")
	parser.add_argument("--tournament-id", type=int, default=17,
					   help="SofaScore tournament ID")
	parser.add_argument("--season-id", type=int, default=None,
					   help="SofaScore season ID")
	parser.add_argument("--mode", type=str, default="last", choices=["last", "upcoming"],
					   help="Schedule mode: 'last' (finished) or 'upcoming'")
	parser.add_argument("--team-name", type=str, default=None,
					   help="Specific team name to get lineup for")
	
	args = parser.parse_args()
	
	# Initialize Fantrax client
	try:
		# FantraxAPI requires league_id, we'll need to get this from config or args
		# For now, let's try to read from config.ini
		import configparser
		config = configparser.ConfigParser()
		if args.fantrax_config.exists():
			config.read(args.fantrax_config)
			league_id = config.get('fantrax', 'league_id', fallback=None)
			if not league_id:
				print("✗ No league_id found in config.ini")
				return
		else:
			print(f"✗ Config file not found: {args.fantrax_config}")
			return
		
		fx = FantraxAPI(league_id=league_id)
		print("✓ Fantrax client initialized")
	except Exception as e:
		print(f"✗ Failed to initialize Fantrax client: {e}")
		return
	
	# Load Fantrax players
	print("Loading Fantrax players...")
	fx_df = load_fantrax_players(fx)
	if fx_df.empty:
		print("✗ No Fantrax players loaded")
		return
	print(f"✓ Loaded {len(fx_df)} Fantrax players")
	
	# Load existing mappings
	existing_mappings, alias_mappings = load_existing_mappings(args.mappings_file)
	print(f"✓ Loaded {len(existing_mappings)} existing player ID mappings")
	print(f"✓ Loaded {len(alias_mappings)} alias mappings")
	
	# Load schedule data for team context
	schedule_df = pd.DataFrame()
	if args.schedules_dir.exists():
		schedule_df = load_schedule_data(args.schedules_dir, args.tournament_id, args.season_id, args.mode)
	
	# Process lineups
	if args.event_id:
		# Process specific event
		event_ids = [args.event_id]
	else:
		# Process all lineup files
		event_ids = [int(f.stem) for f in args.lineups_dir.glob("*.json")]
	
	print(f"Processing {len(event_ids)} events...")
	
	all_results = []
	validation_summary = []
	
	for event_id in event_ids:
		print(f"\nProcessing event {event_id}...")
		
		# Load SofaScore lineup with team context
		sofa_df = load_sofa_event_lineup(args.lineups_dir.parent, event_id, schedule_df)
		if sofa_df.empty:
			print(f"  ✗ No lineup data for event {event_id}")
			continue
		
		# Check if lineup has actual players
		if len(sofa_df) == 0:
			print(f"  ⚠ Empty lineup for event {event_id} (no players)")
			continue
		
		print(f"  ✓ Loaded {len(sofa_df)} players from SofaScore")
		
		# Generate matches
		matches_df = suggest_matches(sofa_df, fx_df, existing_mappings, alias_mappings)
		
		# Create review CSV with all suggestions
		if not args.validate_only:
			create_review_csv(matches_df, args.output_dir)
		
		# Pick best matches per player
		best_matches = pick_best_matches(matches_df)
		
		# Filter by minimum match score
		good_matches = best_matches[best_matches['match_score'] >= args.min_match_score]
		poor_matches = best_matches[best_matches['match_score'] < args.min_match_score]
		
		print(f"  ✓ {len(good_matches)} good matches (score >= {args.min_match_score})")
		if len(poor_matches) > 0:
			print(f"  ⚠ {len(poor_matches)} poor matches (score < {args.min_match_score})")
		
		# Validate lineup counts using best matches (no duplicates)
		is_valid, validation_info = validate_lineup_counts(sofa_df, good_matches)
		
		if is_valid:
			print(f"  ✓ Lineup validation passed")
		else:
			print(f"  ✗ Lineup validation failed: {validation_info.get('error', 'Unknown error')}")
		
		# Create lineup structures
		full_lineup_structure = create_lineup_change_structure(good_matches)
		
		# If specific team requested, create team-specific lineup
		team_lineup = None
		if args.team_name:
			team_lineup = create_fantrax_lineup_structure(good_matches, args.team_name)
			if team_lineup:
				print(f"  ✓ Created lineup for team: {args.team_name}")
				print(f"	 Starters: {len(team_lineup.get('starters', []))}")
				print(f"	 Subs: {len(team_lineup.get('subs', []))}")
				print(f"	 Unmatched: {len(team_lineup.get('unmatched_players', []))}")
		
		# Save results
		if not args.validate_only:
			# Save full lineup
			output_file = args.output_dir / f"{event_id}_mapped.json"
			output_file.parent.mkdir(parents=True, exist_ok=True)
			
			with open(output_file, "w") as f:
				json.dump(full_lineup_structure, f, indent=2, ensure_ascii=False)
			
			print(f"  ✓ Saved full mapped lineup to {output_file}")
			
			# Save team-specific lineup if requested
			if args.team_name and team_lineup:
				team_output_file = args.output_dir / f"{event_id}_{args.team_name.replace(' ', '_')}_lineup.json"
				with open(team_output_file, "w") as f:
					json.dump(team_lineup, f, indent=2, ensure_ascii=False)
				print(f"  ✓ Saved team lineup to {team_output_file}")
		
		# Store results for summary
		all_results.append({
			"event_id": event_id,
			"total_players": len(sofa_df),
			"matched_players": len(good_matches),
			"unmatched_players": len(sofa_df) - len(good_matches),
			"match_rate": len(good_matches) / len(sofa_df) if len(sofa_df) > 0 else 0,
			"is_valid": is_valid
		})
		
		validation_summary.append(validation_info)
	
	# Print summary
	print(f"\n{'='*50}")
	print("PROCESSING SUMMARY")
	print(f"{'='*50}")
	
	total_events = len(all_results)
	valid_events = sum(1 for r in all_results if r['is_valid'])
	total_players = sum(r['total_players'] for r in all_results)
	total_matched = sum(r['matched_players'] for r in all_results)
	
	print(f"Total events processed: {total_events}")
	print(f"Valid lineups: {valid_events}/{total_events}")
	print(f"Total players: {total_players}")
	print(f"Successfully matched: {total_matched}/{total_players} ({total_matched/total_players*100:.1f}%)")
	
	if not args.validate_only:
		print(f"\nMapped lineups saved to: {args.output_dir}")

if __name__ == "__main__":
	main()
