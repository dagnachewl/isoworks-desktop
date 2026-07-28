"""
irms/readers/__init__.py — Package init for IRMS reader implementations.

Import order = registry priority (first registered wins for a given extension).
IsoversReader must come first: it handles .dxf/.cf/.did/.caf/.iarc/.scn via R,
but silently steps aside (supports() → False) when R is not available so the
pure-Python fallbacks below remain active.
"""
from .isoverse_reader import IsoversReader           # R-backed; highest priority
from .elemental_cf_text import ElementalCFTextReader # EA CSV/TXT (pure Python)
from .thermo_dual_inlet_csv import ThermoDualInletCSVReader
from .thermo_dual_inlet_xls import ThermoDualInletXLSReader

__all__ = [
    "IsoversReader",
    "ElementalCFTextReader",
    "ThermoDualInletCSVReader",
    "ThermoDualInletXLSReader",
]
