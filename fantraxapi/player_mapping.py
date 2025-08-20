"""
Player name mapping functionality to standardize player names across different data sources.

This module provides functionality to:
1. Store and retrieve player name mappings
2. Match player names from external sources to Fantrax IDs
3. Normalize player names for consistent matching
"""
from pathlib import Path
from typing import Dict, List, Optional
import re
import yaml
from pydantic import BaseModel, Field
from unidecode import unidecode

class PlayerMapping(BaseModel):
    """Player mapping entry."""
    fantrax_id: str
    fantrax_name: str
    sofascore_id: Optional[int] = None
    sofascore_name: Optional[str] = None
    ffscout_name: Optional[str] = None
    other_names: List[str] = Field(default_factory=list)

class PlayerMappingManager:
    """Manages player name mappings across different sources."""
    
    def __init__(self, mapping_file: str = "config/player_mappings.yaml"):
        """
        Initialize the mapping manager.
        
        Args:
            mapping_file: Path to YAML file containing player mappings
        """
        self.mapping_file = Path(mapping_file)
        self._mappings: Dict[str, PlayerMapping] = {}
        self._load_mappings()
    
    def _load_mappings(self) -> None:
        """Load mappings from file."""
        if not self.mapping_file.exists():
            return
            
        with open(self.mapping_file) as f:
            data = yaml.safe_load(f)
            
        if not data:
            return
            
        for entry in data:
            mapping = PlayerMapping(**entry)
            self._mappings[mapping.fantrax_id] = mapping
            
    def save_mappings(self) -> None:
        """Save current mappings to file."""
        # Create parent directories if needed
        self.mapping_file.parent.mkdir(parents=True, exist_ok=True)
        
        # Convert to list of dicts for YAML
        data = [m.model_dump() for m in self._mappings.values()]
        
        # Save to file
        with open(self.mapping_file, 'w') as f:
            yaml.safe_dump(data, f, sort_keys=False)
    
    def add_mapping(self, mapping: PlayerMapping) -> None:
        """
        Add a new player mapping.
        
        Args:
            mapping: PlayerMapping instance to add
        """
        self._mappings[mapping.fantrax_id] = mapping
        self.save_mappings()
    
    def get_by_fantrax_id(self, fantrax_id: str) -> Optional[PlayerMapping]:
        """Get mapping by Fantrax ID."""
        return self._mappings.get(fantrax_id)
    
    def get_by_sofascore_id(self, sofascore_id: int) -> Optional[PlayerMapping]:
        """Get mapping by SofaScore ID."""
        for mapping in self._mappings.values():
            if mapping.sofascore_id == sofascore_id:
                return mapping
        return None
    
    def get_by_name(self, name: str) -> Optional[PlayerMapping]:
        """
        Get mapping by any known name for the player.
        
        Args:
            name: Player name to look up
                Can be Fantrax name, SofaScore name, FFScout name, or any other known name
        """
        normalized_name = self._normalize_name(name)
        
        for mapping in self._mappings.values():
            # Check all possible name fields
            check_names = [
                mapping.fantrax_name,
                mapping.sofascore_name,
                mapping.ffscout_name,
                *mapping.other_names
            ]
            
            # Remove None values
            check_names = [n for n in check_names if n]
            
            # Normalize and compare
            if any(self._normalize_name(n) == normalized_name for n in check_names):
                return mapping
                
        return None
    
    @staticmethod
    def _normalize_name(name: str) -> str:
        """
        Normalize a player name for comparison.
        
        This implements comprehensive normalization:
        - Convert to lowercase
        - Remove accents (é -> e, etc.)
        - Remove special characters (-, ', etc.)
        - Remove extra whitespace
        - Handle common name variations
        
        Args:
            name: Player name to normalize
        
        Returns:
            Normalized name string
        """
        if not name:
            return ""
            
        # Convert to lowercase
        name = name.lower()
        
        # Remove accents
        name = unidecode(name)
        
        # Handle special cases
        name = (name
            # Handle hyphenated names
            .replace('-', ' ')
            # Handle apostrophes
            .replace("'", '')
            # Remove periods from initials
            .replace('.', ' ')
            # Remove any other special characters
            .replace('_', ' ')
        )
        
        # Remove extra whitespace and trim
        name = " ".join(name.split())
        
        # Handle common name variations
        variations = {
            # Common abbreviations
            'kdb': 'kevin de bruyne',
            # Common name order variations
            'heung min son': 'son heung min',
            'min son heung': 'son heung min',
            # Common partial names
            'de bruyne': 'kevin de bruyne',
            # Add more variations as needed
        }
        
        # Check if this is a known variation
        name = variations.get(name, name)
        
        return name
