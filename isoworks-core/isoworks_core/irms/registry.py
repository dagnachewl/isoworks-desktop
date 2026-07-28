"""
irms/registry.py — Reader registry for the irms subpackage.
Provides register() (decorator) and get_reader() to maintain a global list of
IRMSReader subclasses and select the right one by file suffix at runtime.
"""
from __future__ import annotations
from pathlib import Path
from typing import Type
from .base_reader import IRMSReader

_READERS: list[Type[IRMSReader]] = []

def register(reader_cls: Type[IRMSReader]):
    if reader_cls not in _READERS:
        _READERS.append(reader_cls)
    return reader_cls

def get_reader(path: str | Path) -> type[IRMSReader] | None:
    p = Path(path)
    for cls in _READERS:
        if cls.supports(p):
            return cls
    return None
