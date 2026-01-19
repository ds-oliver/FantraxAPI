#!/usr/bin/env python3
"""
Capture SofaScore cookies (including cf_clearance) into data/sofascore/cookies.json.

Workflow:
1) Launches Chrome to https://www.sofascore.com/
2) You complete any Cloudflare checks in the opened window.
3) After a short wait, cookies are saved in the format the app expects:
      {"cookies": {"cf_clearance": "...", "__cf_bm": "...", ...}}
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36"
)
TARGET_URL = "https://www.sofascore.com/"


def capture_cookies(wait_seconds: int, headless: bool) -> dict[str, str]:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    # Light anti-bot hardening
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument("--window-size=1280,900")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument(f"--user-agent={UA}")

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    try:
        driver.get(TARGET_URL)
        print(f"Opened {TARGET_URL}")
        if not headless:
            print(
                f"Complete any Cloudflare challenge in the browser window, then wait {wait_seconds}s..."
            )
        else:
            print(f"Waiting {wait_seconds}s for cookies (headless mode)...")
        time.sleep(wait_seconds)

        cookies: dict[str, str] = {}
        for c in driver.get_cookies():
            name = c.get("name")
            val = c.get("value")
            if name and val is not None:
                cookies[name] = val

        if not cookies:
            raise RuntimeError("No cookies captured. Did the page load?")
        return cookies
    finally:
        driver.quit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Capture SofaScore cookies for API use.")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/sofascore/cookies.json"),
        help="Path to write cookies JSON (default: data/sofascore/cookies.json).",
    )
    parser.add_argument(
        "--wait-seconds",
        type=int,
        default=35,
        help="Seconds to wait after page load before saving cookies (default: 35).",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run Chrome headless. Use without this flag if Cloudflare prompts appear.",
    )
    args = parser.parse_args()

    cookies = capture_cookies(wait_seconds=args.wait_seconds, headless=args.headless)
    payload = {"cookies": cookies}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(f"Saved {len(cookies)} cookies -> {args.output}")
    if "cf_clearance" not in cookies:
        print("⚠️ cf_clearance missing; solve Cloudflare in non-headless mode and try again.")
    else:
        print("cf_clearance present. Raw HTTP fetches should now avoid 403s.")


if __name__ == "__main__":
    main()
