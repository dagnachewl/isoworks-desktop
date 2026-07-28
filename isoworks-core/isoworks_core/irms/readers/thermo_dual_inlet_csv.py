"""
irms/readers/thermo_dual_inlet_csv.py — Thermo dual-inlet CSV reader for the irms subpackage.
Provides ThermoDualInletCSVReader, which tries multiple encodings and separators
to robustly parse Thermo dual-inlet CSV exports and normalise column names.
"""
from __future__ import annotations
from pathlib import Path
import pandas as pd
from ..base_reader import IRMSReader, IRMSReadResult
from ..registry import register
from ..utils.normalize import normalize_dualinlet_df

@register
class ThermoDualInletCSVReader(IRMSReader):
    suffixes = (".csv",)

    def read(self) -> IRMSReadResult:
        p = Path(self.path)
        # robust CSV read
        encodings = [None, "utf-8", "utf-8-sig", "latin-1", "cp1252", "utf-16"]
        seps = [",", ";", "\t", "|"]
        last = None
        df = None
        for enc in encodings:
            for sep in seps:
                try:
                    df = pd.read_csv(p, sep=sep, encoding=enc, engine="python")
                    if isinstance(df, pd.DataFrame) and df.shape[1] >= 2:
                        raise StopIteration
                except StopIteration:
                    break
                except Exception as e:
                    last = e
                    continue
            if df is not None:
                break
        if df is None:
            df = pd.read_csv(p, engine="python")

        norm = normalize_dualinlet_df(df)
        info = pd.DataFrame([{
            "file_id": p.name,
            "path": str(p.resolve()),
            "instrument": "Thermo Dual Inlet (CSV)",
            "format": "CSV",
            "rows": len(norm),
            "columns": len(norm.columns)
        }])
        return IRMSReadResult(file_info=info, vendor_table=norm)
