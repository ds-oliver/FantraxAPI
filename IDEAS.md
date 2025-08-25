# Ideas

## Fantasy Football

- Show pending trades
- Show live FAAB $ amounts of all teams
- submit claims for players on waivers and on free agency and on trades
- scrape live lineups data from SofaScore (if possible) or FotMob, which is more reliable and faster than getting starters from Fantrax via :
  - {
  "msgs": [
    {
      "method": "getPlayerStats",
      "data": {
        "miscDisplayType": "10",
        "pageNumber": "1"
      }
    }
  ],
  "uiv": 3,
  "refUrl": "https://www.fantrax.com/fantasy/league/o90qdw15mc719reh/players;miscDisplayType=1;pageNumber=1",
  "dt": 0,
  "at": 0,
  "av": "0.0",
  "tz": "America/Los_Angeles",
  "v": "167.0.1"
}

- Add Fantrax "Starting" Players view as an alternative starters source to drive lineup automation:
  - Source: [Fantrax Players – Starting view](https://www.fantrax.com/fantasy/league/o90qdw15mc719reh/players;miscDisplayType=10;pageNumber=1;statusOrTeamFilter=ALL)
  - UI API: `getPlayerStats` with `miscDisplayType: "10"`; entries in `statsTable[*].scorer.icons` that include `{ "typeId": "12" }` indicate starters.
  - Use `scorerId`, `name`, `teamId`, and `posShortNames` to map to our roster and make lineup changes.

# Important scripts

python esd_export_schedule_and_lineups_v1.py --tournament-id 17 --output-dir data/sofascore --upcoming --with-lineups

