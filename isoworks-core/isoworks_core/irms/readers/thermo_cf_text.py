"""
irms/readers/thermo_cf_text.py — Thermo continuous-flow text/DXF reader for the irms subpackage.
Provides ThermoCFTextReader, which parses .dxf, .csv, and .txt continuous-flow
exports from Thermo instruments and normalises column names into a standard schema.
"""
# This file is part of your project and is intended to be distributed under the GPL-2 license.
# See the GNU General Public License version 2 for details.

from __future__ import annotations
from pathlib import Path
import pandas as pd
from ..base_reader import IRMSReader, IRMSReadResult
from ..registry import register
from ..utils.textio import robust_read_table

@register
class ThermoCFTextReader(IRMSReader):
    """Continuous-flow text/CSV/DXF-style exports from Thermo (and similar)."""
    suffixes = (".dxf", ".csv", ".txt")

    def read(self) -> IRMSReadResult:
        df = robust_read_table(self.path)
        cols_lower = {c: c.lower().strip() for c in df.columns}
        rename = {}
        for orig, low in cols_lower.items():
            if low in ("time", "time_s", "seconds", "sec"):
                renameorig = "time_s"
            elif "mass" in low or "cup" in low:
                renameorig = "mass"
            elif "int" in low or "signal" in low or "amp" in low:
                renameorig = "intensity"
            elif "sample" in low or "name" in low:
                renameorig = "sample_id"
            elif "method" in low:
                renameorig = "method"
        if rename:
            df = df.rename(columns=rename)

        file_info = pd.DataFrame([{
            "file_id": Path(self.path).name,
            "path": str(Path(self.path).resolve()),
            "instrument": "Thermo (CF/Export)",
        }])
        return IRMSReadResult(file_info=file_info, vendor_table=df)
