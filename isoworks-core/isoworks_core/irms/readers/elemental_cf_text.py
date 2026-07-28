"""
irms/readers/elemental_cf_text.py — EA continuous-flow text reader for the irms subpackage.
Parses Thermo Elemental Analyser (EA) CSV/TXT exports, detects the EA signature
columns heuristically, and returns a normalised IRMSReadResult.
"""
# This file is part of your project and is intended to be distributed under the GPL-2 license.
# See the GNU General Public License version 2 for details.
from __future__ import annotations
from pathlib import Path
import pandas as pd
from ..base_reader import IRMSReader, IRMSReadResult
from ..registry import register

EA_SIGNATURE_COLS = {
    "Gasconfiguration", "Identifier 1", "Identifier 2", "Method",
    "Peak Nr", "Area 28", "Ampl  28", "BGD 28",
    "rR 45CO2/44CO2", "rR 46CO2/44CO2", "Ampl  45", "Ampl  46",
    "d 15N/14N", "d 13C/12C", "Date.1", "Time.1"
}

def _looks_like_ea_cf(df: pd.DataFrame) -> bool:
    cols = set(df.columns)
    hits = len(cols & EA_SIGNATURE_COLS)
    return hits >= 6  # heuristic threshold

def _parse_timestamp(row) -> pd.Timestamp | None:
    d = str(row.get("Date.1", "")).strip()
    t = str(row.get("Time.1", "")).strip()
    # Try a couple fast formats then fall back
    for fmt in ("%m/%d/%y %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            return pd.to_datetime(f"{d} {t}", format=fmt, errors="coerce")
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
    return pd.to_datetime(f"{d} {t}", errors="coerce")

def _normalize_ea_cf(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    rename = {}
    if "Identifier 1" in out.columns: rename["Identifier 1"] = "sample_id"
    if "Identifier 2" in out.columns: rename["Identifier 2"] = "sample_group"
    if "Method" in out.columns: rename["Method"] = "method"
    if "Gasconfiguration" in out.columns: rename["Gasconfiguration"] = "gas_config"
    if "Ampl  28" in out.columns: rename["Ampl  28"] = "ampl_28"
    if "Area 28" in out.columns: rename["Area 28"] = "area_28"
    if "BGD 28" in out.columns: rename["BGD 28"] = "bgd_28"
    if "Ampl  45" in out.columns: rename["Ampl  45"] = "ampl_45"
    if "Ampl  46" in out.columns: rename["Ampl  46"] = "ampl_46"
    if "rR 45CO2/44CO2" in out.columns: rename["rR 45CO2/44CO2"] = "rR_45_44"
    if "rR 46CO2/44CO2" in out.columns: rename["rR 46CO2/44CO2"] = "rR_46_44"
    if "d 15N/14N" in out.columns: rename["d 15N/14N"] = "d15N"
    if "d 13C/12C" in out.columns: rename["d 13C/12C"] = "d13C"
    out = out.rename(columns=rename)

    if "Date.1" in out.columns or "Time.1" in out.columns:
        out["timestamp"] = out.apply(_parse_timestamp, axis=1)

    def infer_system(row):
        gc = str(row.get("gas_config", "")).upper()
        has_n = pd.notna(row.get("ampl_28")) or "N2" in gc
        has_c = pd.notna(row.get("ampl_45")) or pd.notna(row.get("ampl_46")) or "CO2" in gc
        if has_n and not has_c: return "N"
        if has_c and not has_n: return "C"
        if has_n and has_c:     return "C+N"
        return "unknown"
    out["system"] = out.apply(infer_system, axis=1)

    preferred = ["sample_id","sample_group","method","gas_config","timestamp","system",
                 "ampl_28","area_28","bgd_28","ampl_45","ampl_46","rR_45_44","rR_46_44","d15N","d13C"]
    for c in preferred:
        if c not in out.columns:
            out[c] = pd.NA
    out = out[preferred + [c for c in out.columns if c not in preferred]]
    return out

@register
class ElementalCFTextReader(IRMSReader):
    """Elemental Analyzer (EA) Continuous-Flow IRMS CSV/TXT exports."""
    suffixes = (".csv", ".txt")

    def read(self) -> IRMSReadResult:
        # Try a few separators quickly; EA exports are usually commas
        import pandas as pd
        from ..utils.textio import robust_read_table
        df = robust_read_table(self.path)

        if not _looks_like_ea_cf(df):
            raise ValueError(f"File '{self.path}' does not look like an EA CF-IRMS export.")

        norm = _normalize_ea_cf(df)

        file_info = pd.DataFrame([{
            "file_id": Path(self.path).name,
            "path": str(Path(self.path).resolve()),
            "instrument": "Elemental Analyzer (CF-IRMS)",
        }])
        return IRMSReadResult(file_info=file_info, vendor_table=norm)
