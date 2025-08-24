# utils/cookie_import.py
from __future__ import annotations
import json, pickle
from typing import Any, Dict, List, IO

Cookie = Dict[str, Any]
Artifacts = Dict[str, Any]

def _normalize_cookie_list(lst: List[Dict[str, Any]]) -> List[Cookie]:
    out: List[Cookie] = []
    for c in lst:
        out.append({
            "name": c.get("name"),
            "value": c.get("value"),
            "domain": c.get("domain") or ".fantrax.com",
            "path": c.get("path") or "/",
            "secure": bool(c.get("secure", True)),
            "httpOnly": bool(c.get("httpOnly", True)),
        })
    return out

def read_auth_file(f: IO[bytes]) -> Artifacts:
    """
    Accepts:
      - Selenium pickle: list[dict{name,value,...}]
      - Cookie-Editor/EditThisCookie JSON: list[dict{name,value,...}]
      - Your artifact dict: {"cookies":[...], "storage": {...}} in pickle or JSON
    Returns a normalized artifacts dict: {"cookies":[...], "storage": {...}}
    """
    # Try pickle
    try:
        obj = pickle.load(f)
        if isinstance(obj, dict) and "cookies" in obj:
            obj["cookies"] = _normalize_cookie_list(obj.get("cookies") or [])
            obj["storage"] = obj.get("storage") or {"local": {}, "session": {}}
            return obj
        if isinstance(obj, list):
            return {"cookies": _normalize_cookie_list(obj), "storage": {"local": {}, "session": {}}}
    except Exception:
        f.seek(0)

    # Try JSON
    try:
        obj = json.loads(f.read().decode("utf-8"))
        if isinstance(obj, dict) and "cookies" in obj:
            obj["cookies"] = _normalize_cookie_list(obj.get("cookies") or [])
            obj["storage"] = obj.get("storage") or {"local": {}, "session": {}}
            return obj
        if isinstance(obj, list):
            return {"cookies": _normalize_cookie_list(obj), "storage": {"local": {}, "session": {}}}
    except Exception:
        pass

    raise ValueError("Unsupported file format. Provide a Selenium cookie pickle, Cookie-Editor JSON, or an artifact bundle.")
