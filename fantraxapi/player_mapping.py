"""
Player name mapping functionality to standardize player names across different data sources.

This module provides functionality to:
1. Store and retrieve player name mappings
2. Match player names from external sources to Fantrax IDs
3. Normalize player names for consistent matching
4. Highlight players that are not mapped
"""
from pathlib import Path
from typing import Dict, List, Optional
import re
import yaml
import logging
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
    display_name: Optional[str] = None  # Preferred name for display

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
        
        # Save to file with UTF-8 encoding and no unnecessary escaping
        with open(self.mapping_file, 'w', encoding='utf-8') as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True, default_flow_style=False)
    
    def add_mapping(self, mapping: PlayerMapping) -> None:
        """
        Add a new player mapping.
        
        Args:
            mapping: PlayerMapping instance to add
        """
        # Generate display name if not provided
        if not mapping.display_name:
            # Use the new smart display name selection that analyzes all sources
            mapping.display_name = self._get_best_display_name(mapping)
                
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
    def _get_display_name(name: str) -> str:
        """
        Generate a clean display name from a full name.
        
        This function:
        1. Handles very long names (e.g., Brazilian names)
        2. Preserves accents and special characters
        3. Uses common/preferred names where possible
        
        Examples:
            "Estevao Willian Almeida de Oliveira Gonçalves" -> "Estevao"
            "Kevin De Bruyne" -> "De Bruyne"
            "Son Heung-min" -> "Son"
            "João Pedro" -> "João Pedro"
        """
        # Common preferred names
        preferred_names = {
            "kevin de bruyne": "De Bruyne",
            "son heung-min": "Son",
            "heung-min son": "Son",
            "cristiano ronaldo": "Ronaldo",
            "estevao willian": "Estevao",
            "estevao willian almeida de oliveira goncalves": "Estevao",
        }
        
        # Try preferred names first
        normalized = name.lower().replace("-", " ")
        if normalized in preferred_names:
            return preferred_names[normalized]
            
        # Split into parts
        parts = name.split()
        
        # For very long names (e.g., Brazilian players), use first name
        if len(parts) > 3:
            return parts[0]
            
        # For names with 2-3 parts, use last name unless it's too common
        common_last_names = {"silva", "santos", "oliveira", "rodrigues", "martins"}
        if len(parts) >= 2 and parts[-1].lower() not in common_last_names:
            return parts[-1]
            
        # Default to first name
        return parts[0]

    def _get_best_display_name(self, mapping: PlayerMapping) -> str:
        """
        Analyze all available names from different sources and pick the best display name.
        
        Priority order:
        1. Most common full name across all sources (preferred)
        2. SofaScore name (usually cleanest)
        3. FFScout name (often preferred/display names)
        4. Fantrax name (fallback)
        
        Args:
            mapping: PlayerMapping instance to analyze
            
        Returns:
            Best display name string (preferably the most common full name)
        """
        # Collect all available names
        all_names = []
        if mapping.sofascore_name:
            all_names.append(("sofascore", mapping.sofascore_name))
        if mapping.ffscout_name:
            all_names.append(("ffscout", mapping.ffscout_name))
        if mapping.fantrax_name:
            all_names.append(("fantrax", mapping.fantrax_name))
        all_names.extend([("other", name) for name in mapping.other_names if name])
        
        if not all_names:
            return mapping.fantrax_name or "Unknown"
        
        # Priority 1: Find the most common full name across all sources
        if len(all_names) > 1:
            name_counts = {}
            for source, name in all_names:
                # Normalize name for comparison (remove extra spaces, etc.)
                normalized_name = " ".join(name.split())
                name_counts[normalized_name] = name_counts.get(normalized_name, 0) + 1
            
            # If we have a clear winner (most common name), use it
            if name_counts:
                most_common_name = max(name_counts.items(), key=lambda x: x[1])[0]
                most_common_count = name_counts[most_common_name]
                
                # If the most common name appears more than once, it's our winner
                if most_common_count > 1:
                    return most_common_name
        
        # Priority 2: SofaScore name (usually cleanest and most standardized)
        if mapping.sofascore_name:
            return mapping.sofascore_name
        
        # Priority 3: FFScout name (often preferred/display names)
        if mapping.ffscout_name:
            return mapping.ffscout_name
        
        # Priority 4: Fantrax name (fallback)
        if mapping.fantrax_name:
            return mapping.fantrax_name
        
        # Fallback to first available name
        return all_names[0][1]

    def update_all_display_names(self) -> None:
        """
        Update display names for all existing mappings using the new smart selection logic.
        This is useful after updating mappings to ensure all display names are optimal.
        """
        updated_count = 0
        for mapping in self._mappings.values():
            old_display_name = mapping.display_name
            new_display_name = self._get_best_display_name(mapping)
            
            if new_display_name != old_display_name:
                mapping.display_name = new_display_name
                updated_count += 1
                logging.info(f"Updated display name for {mapping.fantrax_name}: '{old_display_name}' -> '{new_display_name}'")
        
        if updated_count > 0:
            self.save_mappings()
            logging.info(f"Updated {updated_count} display names")
        else:
            logging.info("No display names needed updating")

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
