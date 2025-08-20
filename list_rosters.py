#!/usr/bin/env python3
import pickle
from datetime import datetime
from typing import List, Dict, Tuple
import shutil

from fantraxapi import FantraxAPI
from requests import Session


def load_config(config_path: str = "config.ini") -> dict:
    import configparser
    config = configparser.ConfigParser()
    config.read(config_path)
    if "fantrax" not in config:
        raise ValueError("config.ini must have a [fantrax] section")
    return {
        "league_id": config["fantrax"]["league_id"],
        "cookie_path": config["fantrax"]["cookie_path"],
    }


def load_session(cookie_path: str) -> Session:
    session = Session()
    with open(cookie_path, "rb") as f:
        for cookie in pickle.load(f):
            session.cookies.set(cookie["name"], cookie["value"])
    return session


def _segment_columns(headers: List[str], columns: List[List[str]], max_width: int) -> List[Tuple[List[str], List[List[str]]]]:
    # Compute desired widths per column
    desired_widths = []
    for header, col in zip(headers, columns):
        max_cell = max([len(cell) for cell in col] + [len(header)])
        desired_widths.append(min(max(18, max_cell + 2), 36))

    segments = []
    i = 0
    while i < len(headers):
        width_used = 1  # left border
        j = i
        while j < len(headers):
            next_width = desired_widths[j]
            # account for column and separator
            projected = width_used + next_width + 1  # right border if last, vertical sep otherwise similar cost
            # also add one for the vertical separator between columns
            if j > i:
                projected += 1
            if projected > max_width:
                break
            width_used = projected
            j += 1
        if j == i:
            # ensure at least one column per segment
            j = i + 1
        seg_headers = headers[i:j]
        seg_columns = columns[i:j]
        segments.append((seg_headers, seg_columns))
        i = j
    return segments


def _format_grid_box(headers: List[str], columns: List[List[str]], term_width: int) -> str:
    # Normalize column heights for each segment separately
    segments = _segment_columns(headers, columns, term_width)
    rendered_segments = []
    for seg_headers, seg_columns in segments:
        # Determine widths
        col_widths = []
        for header, col in zip(seg_headers, seg_columns):
            max_cell = max([len(cell) for cell in col] + [len(header)])
            col_widths.append(min(max(18, max_cell + 2), 36))

        max_rows = max(len(col) for col in seg_columns) if seg_columns else 0
        seg_columns_norm = []
        for col in seg_columns:
            if len(col) < max_rows:
                seg_columns_norm.append(col + [""] * (max_rows - len(col)))
            else:
                seg_columns_norm.append(col)

        # Build top border
        top = "┌" + "┬".join("─" * w for w in col_widths) + "┐"
        header_cells = [h[:w].center(w) for h, w in zip(seg_headers, col_widths)]
        header_line = "│" + "│".join(header_cells) + "│"
        sep = "├" + "┼".join("─" * w for w in col_widths) + "┤"

        lines = [top, header_line, sep]
        for row_idx in range(max_rows):
            row_cells = []
            for col_idx, col in enumerate(seg_columns_norm):
                width = col_widths[col_idx]
                cell = col[row_idx][:width]
                row_cells.append(cell.ljust(width))
            lines.append("│" + "│".join(row_cells) + "│")
        bottom = "└" + "┴".join("─" * w for w in col_widths) + "┘"
        lines.append(bottom)
        rendered_segments.append("\n".join(lines))

    return "\n\n".join(rendered_segments)


def _rows_to_lines(roster_rows) -> List[str]:
    lines = []
    for row in roster_rows:
        if row.player:
            # Example: "F: Player Name (TEAM)"
            team_short = getattr(row.player, "team_short_name", getattr(row.player, "team_short", ""))
            lines.append(f"{row.pos.short_name}: {row.player.name} {f'({team_short})' if team_short else ''}")
        else:
            lines.append(f"{row.pos.short_name}: Empty")
    return lines


def _detect_divisions(api: FantraxAPI) -> Dict[str, List[str]]:
    """Return mapping of division name -> list of team_ids.

    Falls back to a single group 'All Teams' if divisions cannot be detected.
    """
    try:
        resp = api._request("getStandings")  # Use base standings
    except Exception:
        # If any issue, return single group
        return {"All Teams": [t.team_id for t in api.teams]}

    team_info = resp.get("fantasyTeamInfo", {})
    divisions: Dict[str, List[str]] = {}
    for team_id, info in team_info.items():
        div_name = None
        # Heuristics: look for fields that imply divisions/groups/conferences
        for key, value in info.items():
            lk = key.lower()
            if any(tok in lk for tok in ["division", "div", "conference", "group"]):
                # Value may be a string or an object with name
                if isinstance(value, str):
                    div_name = value
                elif isinstance(value, dict):
                    div_name = value.get("name") or value.get("shortName") or value.get("caption")
                # Prefer a specific key if multiple exist
                if div_name:
                    break
        if not div_name:
            div_name = "All Teams"
        divisions.setdefault(div_name, []).append(team_id)

    # If we ended up with only 'All Teams', keep as is
    if len(divisions) == 1 and "All Teams" in divisions:
        return divisions

    return divisions


def format_side_by_side(api: FantraxAPI, include_bench: bool = False) -> str:
    # Group teams by division
    divisions = _detect_divisions(api)
    rosters_by_team: Dict[str, object] = {}
    for roster in api.league.list_rosters():
        rosters_by_team[roster.team.team_id] = roster

    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    term_width = shutil.get_terminal_size((120, 40)).columns

    output_lines = [f"League Rosters — {ts}"]

    # Render each division separately
    for div_name, team_ids in divisions.items():
        # Order by team name
        team_ids_sorted = sorted(team_ids, key=lambda tid: rosters_by_team[tid].team.name if tid in rosters_by_team else tid)
        headers = []
        starter_columns: List[List[str]] = []
        bench_columns: List[List[str]] = []
        for tid in team_ids_sorted:
            roster = rosters_by_team.get(tid)
            if not roster:
                continue
            headers.append(roster.team.name)
            starter_columns.append(_rows_to_lines(roster.get_starters()))
            if include_bench:
                bench_columns.append(_rows_to_lines(roster.get_bench_players()))

        if not headers:
            continue

        output_lines.append("")
        output_lines.append(f"{div_name}")
        output_lines.append(_format_grid_box(headers, starter_columns, term_width))
        if include_bench:
            output_lines.append("")
            output_lines.append("Bench")
            output_lines.append(_format_grid_box(headers, bench_columns, term_width))

    return "\n".join(output_lines)


def main():
    import argparse
    cfg = load_config()
    session = load_session(cfg["cookie_path"])
    api = FantraxAPI(cfg["league_id"], session=session)

    parser = argparse.ArgumentParser(description="List all teams' rosters side-by-side")
    parser.add_argument("--bench", action="store_true", help="Include bench section")
    args = parser.parse_args()

    print(format_side_by_side(api, include_bench=args.bench))


if __name__ == "__main__":
    main()


