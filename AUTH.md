# Streamlit Auth Cookbook for Fantrax (Cookies-Based)

This doc shows two supported ways to authenticate Fantrax users inside a **Streamlit** app and produce a cookie jar that the rest of our tooling (e.g. `automate_Fantrax_lineup_changes_app.py`) can reuse.

Both approaches end by writing a Selenium-style cookie pickle (the format our scripts already expect), e.g. `fantraxloggedin.cookie`.

> ⚠️ **Important**
>
> * Never store plaintext passwords. Prefer one-time submission, and store only cookies with short TTLs.
> * Cookies expire. Your UI should surface when re-login is required.

---

## Contents

- [Streamlit Auth Cookbook for Fantrax (Cookies-Based)](#streamlit-auth-cookbook-for-fantrax-cookies-based)
  - [Contents](#contents)
  - [What we produce](#what-we-produce)
  - [Option A — Programmatic login (no browser)](#option-a--programmatic-login-no-browser)
    - [Streamlit snippet (template)](#streamlit-snippet-template)
  - [Option B — Headless Selenium login (browser automation)](#option-b--headless-selenium-login-browser-automation)
    - [Streamlit snippet](#streamlit-snippet)
  - [Common helpers (save/restore/validate cookies)](#common-helpers-saverestorevalidate-cookies)
  - [Security notes](#security-notes)
  - [Troubleshooting](#troubleshooting)
    - [Which option should I choose?](#which-option-should-i-choose)

---

## What we produce

Both options generate the same artifact for compatibility with existing scripts:

```
/path/to/fantraxloggedin.cookie   # Python pickle of a list[dict] in Selenium cookie format
```

Each cookie dict looks like:

```python
{
  "name": "JSESSIONID",
  "value": "node01abcd...",
  "domain": ".fantrax.com",
  "path": "/",
  "secure": True,
  "httpOnly": True,
}
```

---

## Option A — Programmatic login (no browser)

**Summary:** Use an HTTP session to reproduce the browser’s login flow and capture cookies. This is the best UX (no browser pops), but it’s the most fragile because Fantrax may require JavaScript challenges (e.g., Cloudflare). The snippet below uses `cloudscraper` to handle common anti-bot checks.

**Good for:** Server environments without a GUI, fast logins, CI.

**You’ll need:**

```bash
pip install streamlit cloudscraper beautifulsoup4
```

### Streamlit snippet (template)

> 🔧 You must confirm the **actual login form** endpoint and fields once in DevTools (Network tab when you log in manually). Update `LOGIN_URL`, hidden field names, and headers accordingly. The code is written to make this easy.

```python
# streamlit_fantrax_login_programmatic.py
import io
import pickle
import streamlit as st
import cloudscraper
from bs4 import BeautifulSoup

LOGIN_PAGE = "https://www.fantrax.com/login"         # verify in DevTools
LOGIN_URL  = "https://www.fantrax.com/dologin"       # verify in DevTools (form POST target)

def parse_hidden_inputs(html):
    soup = BeautifulSoup(html, "html.parser")
    data = {}
    for inp in soup.select("form input[type=hidden]"):
        name = inp.get("name")
        val  = inp.get("value", "")
        if name:
            data[name] = val
    return data

def to_selenium_cookie_dicts(requests_session):
    out = []
    for c in requests_session.cookies:
        out.append({
            "name": c.name,
            "value": c.value,
            "domain": c.domain or ".fantrax.com",
            "path": c.path or "/",
            "secure": bool(getattr(c, "secure", True)),
            "httpOnly": True,  # best-effort; requests doesn't expose this bit
        })
    return out

def login_programmatic(username, password):
    # cloudscraper handles many CF challenges
    s = cloudscraper.create_scraper()
    # 1) GET login page to collect hidden inputs/tokens
    r = s.get(LOGIN_PAGE, timeout=30)
    r.raise_for_status()
    form_data = parse_hidden_inputs(r.text)
    # 2) POST credentials + hidden fields (names MUST match DevTools)
    payload = {
        **form_data,
        "username": username,   # update if site uses 'email' or 'login'
        "password": password,
    }
    # Typical headers the form sends; tweak after checking DevTools
    headers = {
        "origin": "https://www.fantrax.com",
        "referer": LOGIN_PAGE,
    }
    p = s.post(LOGIN_URL, data=payload, headers=headers, timeout=30, allow_redirects=True)
    p.raise_for_status()

    # Check that we have an auth cookie
    if not any(k in s.cookies.get_dict() for k in ("JSESSIONID", "cf_clearance", "FX_RM", "ui", "uig")):
        raise RuntimeError("Login appears to have failed (auth cookies not found).")

    return s

st.title("Fantrax Login (Programmatic)")

with st.form("login"):
    user = st.text_input("Fantrax username or email")
    pw   = st.text_input("Fantrax password", type="password")
    submit = st.form_submit_button("Login")

if submit:
    try:
        sess = login_programmatic(user, pw)
        cookies_list = to_selenium_cookie_dicts(sess)
        # Save to a pickle bytes object (you can also write to disk)
        buf = io.BytesIO()
        pickle.dump(cookies_list, buf)
        st.session_state["fantrax_cookie_pickle"] = buf.getvalue()
        st.success("Logged in. Cookies captured.")
        st.download_button(
            "Download cookie file",
            data=st.session_state["fantrax_cookie_pickle"],
            file_name="fantraxloggedin.cookie",
            mime="application/octet-stream",
        )
    except Exception as e:
        st.error(f"Login failed: {e}")
```

**Notes**

* The **two constants** `LOGIN_PAGE` and `LOGIN_URL` must match what you see in DevTools.
* Some sites require **CSRF tokens**; the `parse_hidden_inputs` helper collects them.
* If additional headers (e.g. `sec-ch-ua`, `content-type`) are needed, copy them from DevTools.
* If this still fails, use **Option B**.

---

## Option B — Headless Selenium login (browser automation)

**Summary:** Drive a real (headless) Chrome to perform the login flow, then export cookies. This approach is closest to your existing `bootstrap_cookie.py`, but runs **without showing a window** and can be orchestrated directly from Streamlit.

**Good for:** Reliability when anti-bot/JS challenges break pure `requests`; closer parity with a real user session.

**You’ll need:**

```bash
pip install streamlit selenium webdriver-manager
# Optional but helpful against bot checks:
# pip install undetected-chromedriver
```

> If deploying on Linux, ensure Chrome/Chromium is installed and available on `PATH`.

### Streamlit snippet

```python
# streamlit_fantrax_login_selenium.py
import io, time, pickle
import streamlit as st

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.chrome.options import Options
from webdriver_manager.chrome import ChromeDriverManager

LOGIN_URL = "https://www.fantrax.com/login"  # verify

def build_driver():
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--window-size=1200,900")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    # A basic UA; adjust if necessary
    opts.add_argument("--user-agent=Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/123.0.0.0 Safari/537.36")
    driver = webdriver.Chrome(ChromeDriverManager().install(), options=opts)
    return driver

def to_selenium_cookie_dicts_from_driver(driver):
    out = []
    for c in driver.get_cookies():
        out.append({
            "name": c.get("name"),
            "value": c.get("value"),
            "domain": c.get("domain") or ".fantrax.com",
            "path": c.get("path") or "/",
            "secure": bool(c.get("secure", True)),
            "httpOnly": bool(c.get("httpOnly", True)),
        })
    return out

st.title("Fantrax Login (Headless Selenium)")

with st.form("login"):
    user = st.text_input("Fantrax username or email")
    pw   = st.text_input("Fantrax password", type="password")
    submit = st.form_submit_button("Login")

if submit:
    try:
        driver = build_driver()
        driver.get(LOGIN_URL)

        # TODO: Update the selectors below after inspecting the login page
        # Common patterns: input[name='username'] / input[name='email'] / input[type='password']
        user_el = driver.find_element(By.CSS_SELECTOR, "input[name='username'], input[name='email']")
        pass_el = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
        user_el.clear(); user_el.send_keys(user)
        pass_el.clear(); pass_el.send_keys(pw)

        # Click submit button
        # Update selector as needed (e.g., "button[type=submit]" or form submit)
        driver.find_element(By.CSS_SELECTOR, "button[type='submit'], input[type='submit']").click()

        # Wait for redirect / session establishment
        time.sleep(6)  # replace with WebDriverWait for robustness

        cookies_list = to_selenium_cookie_dicts_from_driver(driver)
        if not cookies_list:
            raise RuntimeError("No cookies captured (login may have failed).")

        buf = io.BytesIO()
        pickle.dump(cookies_list, buf)
        st.session_state["fantrax_cookie_pickle"] = buf.getvalue()
        st.success("Logged in. Cookies captured.")
        st.download_button(
            "Download cookie file",
            data=st.session_state["fantrax_cookie_pickle"],
            file_name="fantraxloggedin.cookie",
            mime="application/octet-stream",
        )
    except Exception as e:
        st.error(f"Login failed: {e}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass
```

**Tips**

* When selectors don’t match, open the login page in your own browser, inspect the form, and adjust the `By.CSS_SELECTOR` strings accordingly.
* Replace `time.sleep` with `WebDriverWait` checking for an element present on the post-login page (e.g., a league link).
* Consider **`undetected-chromedriver`** if Cloudflare blocks headless Chrome.

---

## Common helpers (save/restore/validate cookies)

Use these helpers elsewhere in the repo (or import from your utilities) to keep behavior consistent.

```python
# auth_helpers.py
import pickle, requests, certifi, os

def write_cookie_pickle(cookies_list, path):
    with open(path, "wb") as f:
        pickle.dump(cookies_list, f)

def load_requests_session_from_pickle(cookie_file):
    s = requests.Session()
    # Trust updated CA bundle
    os.environ["SSL_CERT_FILE"] = certifi.where()
    os.environ["REQUESTS_CA_BUNDLE"] = certifi.where()
    with open(cookie_file, "rb") as f:
        cookies = pickle.load(f)
    for c in cookies:
        name, value = c.get("name"), c.get("value")
        if not name or value is None:
            continue
        s.cookies.set(
            name,
            value,
            domain=c.get("domain") or ".fantrax.com",
            path=c.get("path") or "/",
            secure=c.get("secure", True),
            rest={"HttpOnly": c.get("httpOnly", True)},
        )
    s.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.fantrax.com",
        "Referer": "https://www.fantrax.com/",
    })
    return s

def validate_logged_in(sess, league_id=None):
    try:
        url = f"https://www.fantrax.com/fantasy/league/{league_id}/home" if league_id else "https://www.fantrax.com/"
        r = sess.get(url, timeout=20)
        if r.status_code != 200:
            return False
        # crude heuristic
        return "Sign In" not in r.text and "login" not in r.url.lower()
    except Exception:
        return False
```

---

## Security notes

* **Do not persist passwords.** Only use them to obtain cookies, then discard immediately.
* Treat cookie files like secrets. Prefer:

  * encrypting at rest,
  * short lifetimes (force refresh regularly),
  * scoping access per-user.
* Consider **per-user storage** (e.g., S3/GS + KMS) with TTL + revocation.
* If you expose “Download cookie file”, warn users about the risk of sharing it.

---

## Troubleshooting

* **Programmatic login fails**: Confirm the real login POST target and required hidden fields using Chrome DevTools → *Network* tab. Update `LOGIN_URL`, form field names, and headers.
* **Cloudflare blocks**:

  * Try Option B (headless browser).
  * With Selenium, consider `undetected-chromedriver`.
* **No cookies captured**:

  * Ensure you wait for navigation/redirect to complete after submit.
  * Validate by hitting a league page using the session (see `validate_logged_in`).
* **Cookies expire quickly**:

  * Add a “Reconnect” button that reruns the login flow.
  * For long-running jobs, detect 401/redirects and surface a re-auth prompt.

---

### Which option should I choose?

* Start with **Option B (headless Selenium)** if you need it to “just work” against dynamic protections.
* Use **Option A** when you’ve verified the login form is stable and Cloudflare allows programmatic access; it’s faster and simpler operationally.

---

**Drop-in pointer for existing scripts**

Once you’ve captured `fantraxloggedin.cookie` via either option, point your scripts to it (as you do today), e.g.:

```bash
python src/scripts/automate_Fantrax_lineup_changes_app.py \
  --league <LEAGUE_ID> \
  --cookies /path/to/fantraxloggedin.cookie \
  ...
```

That’s it.
