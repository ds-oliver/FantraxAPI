#!/usr/bin/env python3
"""
Inspect the contents of fantraxloggedin.cookie and list cookie names/count.
Usage:
    python3 scripts/inspect_fantrax_cookie.py [path_to_cookie]
Default cookie path: fantraxloggedin.cookie
"""
import pickle
import sys
from pathlib import Path


def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("fantraxloggedin.cookie")
    if not path.exists():
        print(f"Cookie file not found: {path}")
        sys.exit(1)
    cookies = pickle.load(path.open("rb"))
    print("count:", len(cookies))
    print("names:", [c.get("name") for c in cookies])


if __name__ == "__main__":
    main()

