# src/scrapers/scout_picks_rosters.py
from __future__ import annotations

import re
import argparse
from datetime import datetime
from pathlib import Path

import pandas as pd
import requests
import requests_cache
from bs4 import BeautifulSoup

ROW_TO_POS = {"row-1": "GK", "row-2": "DEF", "row-3": "MID", "row-4": "FWD"}

def parse_full_name_from_title(title: str | None) -> str | None:
	"""
	Titles look like 'White (Ben)' or 'Raya Martin (David)'.
	Return 'Ben White' or 'David Raya Martin'. If title missing, return None.
	"""
	if not title:
		return None
	m = re.match(r"^\s*(.*?)\s*\((.*?)\)\s*$", title)
	if m:
		surname, given = m.group(1).strip(), m.group(2).strip()
		return f"{given} {surname}".strip()
	# sometimes it's just 'Martinelli' (no parens)
	return title.strip()

def extract_team_block(team_li) -> list[dict]:
	"""
	team_li is <li class='team-news-item' data-team-code='ars'>...</li>
	Return list of player dicts (one per starter).
	"""
	team_code = team_li.get("data-team-code", "").strip()

	# team name from header h2
	h2 = team_li.select_one("div.story-wrap h2")
	team_name = (h2.get_text(strip=True) if h2 else "").strip()

	# next match (optional)
	next_match_el = team_li.select_one(".next-match")
	next_match = next_match_el.get_text(" ", strip=True).replace("Next Match: ", "") if next_match_el else None

	rows = []
	# each row-x is a line on the pitch (1..4)
	for row in team_li.select("div.scout-picks ul[class^='row-']"):
		row_class = next((c for c in row.get("class", []) if c.startswith("row-")), None)
		pos = ROW_TO_POS.get(row_class, None)
		for i, li in enumerate(row.select("li"), start=1):
			display_name = li.select_one("span.player-name")
			display_name = display_name.get_text(strip=True) if display_name else None
			title_full = parse_full_name_from_title(li.get("title"))

			# optional extractions
			img = li.select_one("img.player-image")
			img_url = img.get("src") if img else None
			# extract PL photo id (digits) if present
			pl_photo_id = None
			if img_url:
				m = re.search(r"/([0-9]{5,})\.png", img_url)
				pl_photo_id = m.group(1) if m else None

			rows.append(
				{
					"team_code": team_code,
					"team_name": team_name,
					"next_match": next_match,
					"position": pos,
					"row": row_class,
					"depth_order": i,
					"player_display": display_name,			# shown on card (surname or short name)
					"player_full_from_title": title_full,	# 'First Last' when available
					"pl_photo_id": pl_photo_id,
					"img_url": img_url,
				}
			)
	return rows

def scrape(url: str) -> pd.DataFrame:
	# cache so we don't hammer the site while iterating
	requests_cache.install_cache("scout_picks_cache", expire_after=3600)
	headers = {
		"User-Agent": "Mozilla/5.0 (compatible; roster-scraper/1.0; +github.com/draftalchemy)"
	}
	resp = requests.get(url, headers=headers, timeout=30)
	resp.raise_for_status()

	soup = BeautifulSoup(resp.text, "lxml")

	data = []
	for team_li in soup.select("li.team-news-item[data-team-code]"):
		data.extend(extract_team_block(team_li))

	df = pd.DataFrame(data)

	# basic validation: 11 starters/team if all present
	if not df.empty:
		starters = (
			df.groupby(["team_code", "team_name"])["player_display"]
			.count()
			.reset_index(name="starter_count")
		)
		# you can log or assert here as preferred
		# print(starters.sort_values("starter_count"))
	return df

def save_outputs(df: pd.DataFrame, out_dir: Path) -> None:
	out_dir.mkdir(parents=True, exist_ok=True)
	stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
	csv_path = out_dir / f"scout_picks_rosters_{stamp}.csv"
	pq_path = out_dir / f"scout_picks_rosters_{stamp}.parquet"
	df.to_csv(csv_path, index=False)
	df.to_parquet(pq_path, index=False)
	print(f"Saved:\n- {csv_path}\n- {pq_path}")

def main():
	parser = argparse.ArgumentParser()
	parser.add_argument("--url", required=True, help="URL of the page containing team-news-item blocks")
	parser.add_argument(
		"--out-dir",
		default="data/silver/scout_picks",
		help="Output directory for CSV/Parquet",
	)
	args = parser.parse_args()
	df = scrape(args.url)
	if df.empty:
		raise SystemExit("No roster data found on page.")
	save_outputs(df, Path(args.out_dir))

if __name__ == "__main__":
	main()
