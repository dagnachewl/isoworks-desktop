"""
irms/readers/thermo_dual_inlet_xls.py — Thermo dual-inlet XLS/XLSX reader for the irms subpackage.
Provides ThermoDualInletXLSReader, which reads all sheets from .xls/.xlsx dual-inlet
exports via xlrd/openpyxl, normalises each sheet, and concatenates the results.
"""
from __future__ import annotations
from pathlib import Path
import pandas as pd
from ..base_reader import IRMSReader, IRMSReadResult
from ..registry import register
from ..utils.normalize import normalize_dualinlet_df

@register
class ThermoDualInletXLSReader(IRMSReader):
    suffixes = (".xls", ".xlsx")

    def read(self) -> IRMSReadResult:
        p = Path(self.path)
        # Choose engine
        engine = None
        if p.suffix.lower() == ".xls":
            engine = "xlrd"
        # Read all sheets
        try:
            sheets = pd.read_excel(p, sheet_name=None, engine=engine)
        except Exception as e:
            raise RuntimeError(f"Failed to read Excel '{p.name}'. If this is .xls, install xlrd >= 2.0.1. Original error: {e}")

        norms = []
        for name, df in sheets.items():
            try:
                n = normalize_dualinlet_df(df)
                n.insert(0, "sheet", str(name))
                norms.append(n)
            except Exception as e:

                logging.warning(f"Exception caught: {e}"); continue

        if not norms:
            raise ValueError("No usable Dual Inlet tables found in workbook.")

        norm_all = pd.concat(norms, ignore_index=True, sort=False)

        info = pd.DataFrame([{
            "file_id": p.name,
            "path": str(p.resolve()),
            "instrument": "Thermo Dual Inlet (XLS)",
            "format": "XLSX/XLS (multi-sheet)",
            "sheets": len(sheets),
            "rows": len(norm_all),
            "columns": len(norm_all.columns)
        }])
        return IRMSReadResult(file_info=info, vendor_table=norm_all)
