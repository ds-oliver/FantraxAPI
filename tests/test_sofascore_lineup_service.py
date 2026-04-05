"""Tests for services.sofascore_lineup_service helpers."""

from datetime import datetime, timezone

from services.sofascore_lineup_service import parse_kickoff_dt


def test_parse_kickoff_dt_iso_csv_string():
    dt = parse_kickoff_dt("2026-04-11 14:00:00+0000")
    assert dt == datetime(2026, 4, 11, 14, 0, 0, tzinfo=timezone.utc)


def test_parse_kickoff_dt_unix_seconds():
    # 2026-04-11 14:00:00 UTC
    ts = int(datetime(2026, 4, 11, 14, 0, 0, tzinfo=timezone.utc).timestamp())
    dt = parse_kickoff_dt(ts)
    assert dt == datetime(2026, 4, 11, 14, 0, 0, tzinfo=timezone.utc)


def test_parse_kickoff_dt_unix_millis():
    ts_ms = int(datetime(2026, 4, 11, 14, 0, 0, tzinfo=timezone.utc).timestamp() * 1000)
    dt = parse_kickoff_dt(ts_ms)
    assert dt == datetime(2026, 4, 11, 14, 0, 0, tzinfo=timezone.utc)


def test_parse_kickoff_dt_rejects_bool():
    assert parse_kickoff_dt(True) is None
    assert parse_kickoff_dt(False) is None


def test_parse_kickoff_dt_none_and_empty():
    assert parse_kickoff_dt(None) is None
    assert parse_kickoff_dt("") is None
    assert parse_kickoff_dt("   ") is None
