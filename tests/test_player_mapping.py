"""
Unit tests for player mapping helper utilities.
"""

import yaml

from fantraxapi.player_mapping import PlayerMappingManager


def test_get_by_fantrax_id_handles_zero_padding(tmp_path):
	"""
	Ensure we can find mappings even if the Fantrax ID loses a leading zero.
	"""
	mapping_file = tmp_path / "player_mappings.yaml"
	data = [
		{
			"fantrax_id": "04fk1",
			"fantrax_name": "Pascal Gross",
			"sofascore_id": 48480,
			"display_name": "Pascal Gross",
		}
	]
	mapping_file.write_text(yaml.safe_dump(data), encoding="utf-8")

	manager = PlayerMappingManager(mapping_file=str(mapping_file))

	canonical = manager.get_by_fantrax_id("4fk1")
	assert canonical is not None
	assert canonical.fantrax_id == "04fk1"

	assert manager.get_by_fantrax_id("04fk1") is canonical


def test_get_by_fantrax_id_is_case_insensitive(tmp_path):
	mapping_file = tmp_path / "player_mappings.yaml"
	data = [
		{
			"fantrax_id": "04FK1",
			"fantrax_name": "Pascal Gross",
			"sofascore_id": 48480,
			"display_name": "Pascal Gross",
		}
	]
	mapping_file.write_text(yaml.safe_dump(data), encoding="utf-8")

	manager = PlayerMappingManager(mapping_file=str(mapping_file))

	assert manager.get_by_fantrax_id("04fk1") is not None
	assert manager.get_by_fantrax_id("04FK1") is not None
	assert manager.get_by_fantrax_id("04Fk1") is not None
