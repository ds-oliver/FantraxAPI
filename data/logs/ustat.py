import ScraperFC as sfc

us = sfc.Understat()
team_data = us.scrape_all_teams_data("2023/2024", "EPL", as_df=False)
print(team_data)