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

- [ ] Scrape the data from the website
- [ ] Analyze the data
- [ ] Make predictions
- [ ] Make recommendations
- [ ] Make a dashboard
- [ ] Make a dashboard