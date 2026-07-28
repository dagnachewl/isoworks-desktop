"""
irms/readers/thermo_dual_inlet.py — Thermo dual-inlet binary reader for the irms subpackage.
Provides ThermoDualInletReader, which extracts UTF-16LE strings from .did and
.caf binary files produced by Thermo dual-inlet IRMS instruments.
"""

from __future__ import annotations
from pathlib import Path
import pandas as pd
import re
from ..base_reader import IRMSReader, IRMSReadResult
from ..registry import register

def _utf16le_strings(b: bytes, min_chars: int = 4):
    out = []
    curr = []
    for i in range(0, len(b) - 1, 2):
        ch = int.from_bytes(b[i:i+2], "little")
        if 32 <= ch <= 126:
            curr.append(chr(ch))
        else:
            if len(curr) >= min_chars:
                out.append("".join(curr))
            curr = []
    if len(curr) >= min_chars:
        out.append("".join(curr))
    return out

@register
class ThermoDualInletReader(IRMSReader):
    suffixes = (".did", ".caf")
    def read(self) -> IRMSReadResult:
        p = Path(self.path)
        b = p.read_bytes()
        u[16] = _utf16le_strings(b, 4)
        # Simple metrics
        di_blocks = sum(1 for s in u[16] if "DualInlet RawData Sample Block" in s)
        ref_name_count = sum(1 for s in u[16] if s.strip() == "Ref. Name")
        file_info = pd.DataFrame([{
            "file_id": p.name,
            "path": str(p.resolve()),
            "instrument": "Thermo (Dual Inlet)",
            "format": "CAF/DID (binary)",
            "di_blocks": di_blocks,
            "ref_name_count": ref_name_count,
        }])
        # Collect interesting strings
        rows = []
        seen = set()
        for s in u16:
            if (":" in s) or ("DualInlet" in s) or ("Ref. Name" in s) or ("Date" in s):
                k = s.strip()
                if k not in seen:
                    rows.append({"tag": "string", "value": k})
                    seen.add(k)
            if len(rows) >= 500:
                break
        vendor_table = pd.DataFrame(rows) if rows else None
        return IRMSReadResult(file_info=file_info, vendor_table=vendor_table)
