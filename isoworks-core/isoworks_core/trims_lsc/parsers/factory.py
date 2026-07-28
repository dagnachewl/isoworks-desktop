"""
LSC Parser Factory
"""
from __future__ import annotations

from typing import Optional
from .quantulus import QuantulusRegistryParserModern, QuantulusParserStrategy
from .hidex import HidexMatrixParserStrategy
from .aloka import AlokaCsvParserStrategy
from .other import PackardLSCParser, HidexLSCParser, DelimitedParserStrategy
from .base import LSCParserStrategy


def make_lsc_parser(format_id: int, format_name: str, filepath: str) -> LSCParserStrategy:
    """
    Factory function to create appropriate LSC parser.
    
    Args:
        format_id: Database format ID
        format_name: Format name
        filepath: Path to data file
    
    Returns:
        Parser instance
    """
    format_name_lower = (format_name or "").lower()
    
    # Quantulus
    if format_id in (1, 2) or "quantulus" in format_name_lower:
        if "registry" in format_name_lower or format_id == 1:
            return QuantulusRegistryParserModern(filepath)
        else:
            return QuantulusParserStrategy(filepath)
    
    # HIDEX Matrix
    elif format_id == 6:
        return HidexMatrixParserStrategy(filepath)
    
    # Aloka
    elif format_id == 5 or "aloka" in format_name_lower:
        return AlokaCsvParserStrategy(filepath)
    
    # Packard
    elif format_id == 3 or "packard" in format_name_lower:
        return PackardLSCParser(filepath)
    
    # HIDEX List or generic delimited
    elif format_id == 12:
        return HidexLSCParser(filepath)
    
    else:
        return DelimitedParserStrategy(filepath)
