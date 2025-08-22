#!/usr/bin/env python3
import argparse
import sys

try:
    import esd  # EasySoccerData (installed via: pip install "git+https://github.com/manucabral/EasySoccerData.git")
except ModuleNotFoundError:
    print(
        'EasySoccerData not installed. Run:\n  pip install "git+https://github.com/manucabral/EasySoccerData.git"',
        file=sys.stderr,
    )
    sys.exit(1)


def jn(p):  # jersey number helper
    return getattr(p, "jersey_number", None) or getattr(p, "jerseyNumber", None) or "?"

def pos(p):  # position helper
    return getattr(p, "position", None) or "?"

def print_team(title, team):
    print(f"\n{title}")
    print(f"Formation: {getattr(team, 'formation', '') or 'N/A'}")

    players = list(getattr(team, "players", []) or [])
    starters = [pl for pl in players if not getattr(pl, "substitute", False)]
    subs     = [pl for pl in players if getattr(pl, "substitute", False)]

    print("\nStarting XI:")
    for pl in starters[:11]:
        info = getattr(pl, "info", None)
        name = getattr(info, "name", None) or getattr(info, "short_name", None) or "Unknown"
        print(f"- [{jn(info)}] {name} ({pos(info)})")

    if subs:
        print("\nSubstitutes:")
        for pl in subs:
            info = getattr(pl, "info", None)
            name = getattr(info, "name", None) or getattr(info, "short_name", None) or "Unknown"
            print(f"- [{jn(info)}] {name} ({pos(info)})")
    else:
        print("\nSubstitutes: (none)")


def print_lineups_for_event(client, event_id: int):
    lu = client.get_match_lineups(event_id)  # returns Lineups dataclass
    status = "confirmed" if getattr(lu, "confirmed", False) else "preliminary"
    print(f"\nLineup Status: {status}")
    print_team("Home", getattr(lu, "home", None))
    print_team("Away", getattr(lu, "away", None))


def main():
    ap = argparse.ArgumentParser(description="Print SofaScore lineups via EasySoccerData")
    ap.add_argument("--event-id", type=int, help="SofaScore event ID (e.g. 14025088)")
    ap.add_argument("--live", action="store_true", help="Use first live event if no --event-id")
    args = ap.parse_args()

    client = esd.SofascoreClient()

    if args.event_id:
        print_lineups_for_event(client, args.event_id)
        return

    if args.live:
        events = client.get_events(live=True)
        if not events:
            print("No live events found.")
            return
        ev = events[0]
        ev_id = getattr(ev, "id", None) or getattr(ev, "event_id", None)
        home  = getattr(ev, "home_team", None) or getattr(ev, "homeTeam", None)
        away  = getattr(ev, "away_team", None) or getattr(ev, "awayTeam", None)
        print(f"Using live event: {home} vs {away} (ID: {ev_id})")
        print_lineups_for_event(client, int(ev_id))
        return

    ap.error("Provide --event-id or --live")


if __name__ == "__main__":
    main()
