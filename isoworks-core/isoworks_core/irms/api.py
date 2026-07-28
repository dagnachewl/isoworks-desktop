"""
irms/api.py — Public API entry point for the irms subpackage.
Exposes the load() function that selects the appropriate IRMSReader from the
registry and returns an IRMSReadResult for any supported instrument file.
"""
from __future__ import annotations
from pathlib import Path
from .registry import get_reader
from .base_reader import IRMSReadResult
from .readers import (  # noqa: F401 — side-effect: registers all readers
    IsoversReader,
    ElementalCFTextReader,
    ThermoDualInletCSVReader,
    ThermoDualInletXLSReader,
)

def load(path: str | Path) -> IRMSReadResult:
    cls = get_reader(path)
    if not cls:
        raise ValueError(f"No IRMS reader available for: {path}")
    return cls(path).read()
