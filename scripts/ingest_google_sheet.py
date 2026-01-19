"""
Fetch projections from a Google Sheet using a service account and write to disk.

Usage:
  python scripts/ingest_google_sheet.py --sheet-url <URL> \
      --worksheet "Sheet1" \
      --out data/derived/projections.parquet

The service account key is expected at:
  - $SERVICE_ACCOUNT_JSON (env var), or
  - config/service_account.json (relative to repo root)

Make sure the sheet is shared with the service account's client_email.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Optional

import gspread
import pandas as pd


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_KEY_PATH = REPO_ROOT / "config" / "service_account.json"
DEFAULT_OUTPUT = REPO_ROOT / "data" / "derived" / "projections.parquet"


def _key_path() -> Path:
    env_path = os.environ.get("SERVICE_ACCOUNT_JSON")
    if env_path:
        return Path(env_path).expanduser().resolve()
    return DEFAULT_KEY_PATH


def load_sheet(sheet_url: str, worksheet: Optional[str] = None) -> pd.DataFrame:
    key_path = _key_path()
    if not key_path.exists():
        raise FileNotFoundError(f"Service account key not found at {key_path}")

    try:
        client = gspread.service_account(filename=str(key_path))
    except Exception as exc:
        raise RuntimeError(f"Failed to authenticate with key {key_path}: {exc}") from exc

    try:
        sh = client.open_by_url(sheet_url)
    except Exception as exc:
        raise RuntimeError(
            "Unable to open sheet. Ensure the sheet is shared with the service "
            f"account email and the URL is correct. Error: {exc}"
        ) from exc

    ws = sh.worksheet(worksheet) if worksheet else sh.sheet1
    records = ws.get_all_records()
    return pd.DataFrame(records)


def write_outputs(df: pd.DataFrame, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.suffix.lower() in {".parquet"}:
        df.to_parquet(out_path, index=False)
    elif out_path.suffix.lower() in {".csv"}:
        df.to_csv(out_path, index=False)
    elif out_path.suffix.lower() in {".json"}:
        df.to_json(out_path, orient="records")
    else:
        # default to parquet if unknown extension
        df.to_parquet(out_path, index=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest Google Sheet projections.")
    parser.add_argument("--sheet-url", required=True, help="Full Google Sheet URL.")
    parser.add_argument("--worksheet", default=None, help="Worksheet name (default: first sheet).")
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUTPUT),
        help="Output path (parquet|csv|json). Default: data/derived/projections.parquet",
    )
    args = parser.parse_args()

    df = load_sheet(args.sheet_url, worksheet=args.worksheet)
    write_outputs(df, Path(args.out))
    print(f"Wrote {len(df)} rows to {args.out}")


if __name__ == "__main__":
    main()
