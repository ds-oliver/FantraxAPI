import pickle
import time
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

def main():
    service = Service(ChromeDriverManager().install())
    options = Options()
    options.add_argument("--window-size=1920,1600")
    # A user-agent is suggested in the wrapper docs
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/97.0.4692.71 Safari/537.36"
    )

    with webdriver.Chrome(service=service, options=options) as driver:
        driver.get("https://www.fantrax.com/login")
        print("A Chrome window opened. Log in to Fantrax. I'll capture cookies in ~30s…")
        time.sleep(30)
        with open("fantraxloggedin.cookie", "wb") as f:
            pickle.dump(driver.get_cookies(), f)
        print("Saved login cookies to fantraxloggedin.cookie")

if __name__ == "__main__":
    main()