"""
Tests for lineup watcher CLI.
"""
import pytest
from pathlib import Path

from apps.lineup_watcher.watch_lineups import parse_args

def test_parse_args_defaults():
	"""Test default CLI arguments."""
	args = parse_args([])
	assert args.window == 90
	assert args.interval == 60
	assert args.output_dir == "data/lineups"

def test_parse_args_custom():
	"""Test custom CLI arguments."""
	args = parse_args([
		"--window", "120",
		"--interval", "30",
		"--output-dir", "custom/path"
	])
	assert args.window == 120
	assert args.interval == 30
	assert args.output_dir == "custom/path"
