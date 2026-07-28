"""
irms/base_reader.py — Abstract base classes for IRMS file readers.
Defines IRMSReadResult (dataclass) and IRMSReader (ABC) that all concrete
reader implementations must subclass and implement a read() method for.
"""
from __future__ import annotations
from dataclasses import dataclass
import pandas as pd
from abc import ABC, abstractmethod
from pathlib import Path

@dataclass
class IRMSReadResult:
    file_info: pd.DataFrame
    vendor_table: pd.DataFrame | None = None
    raw_signals: pd.DataFrame | None = None

class IRMSReader(ABC):
    suffixes: tuple[str, ...] = ()
    def __init__(self, path: str | Path):
        self.path = Path(path)
    @abstractmethod
    def read(self) -> IRMSReadResult: ...
    @classmethod
    def supports(cls, path: str | Path) -> bool:
        return bool(cls.suffixes) and Path(path).suffix.lower() in cls.suffixes
