"""
iso_io/irms_reader.py — IRMS file loader for the IsoWorks processor GUI.
Provides IRMSLoadResult and load functions that detect whether a file is a
Thermo dual-inlet or EA continuous-flow export and parse it accordingly.
"""
# iso_io/irms_reader.py
from __future__ import annotations
from dataclasses import dataclass
from typing import List, Optional, Tuple
import os
import pandas as pd
import numpy as np

@dataclass
class IRMSLoadResult:
    vendor_table: pd.DataFrame
    source_type: str  # 'IRMS (Thermo DI)' or 'IRMS (EA)'
    sheets_read: Optional[List[str]] = None
    path: Optionalstr = None

def _peek_columns(path: str) -> Tuple[List[str], str]:
    ext = os.path.splitext(path)[1].lower()
    cols: List[str] = []
    if ext in (".csv", ".txt"):
        trials = [
            dict(sep=",", encoding="utf-8"),
            dict(sep=",", encoding="latin1"),
            dict(sep=";", encoding="utf-8"),
            dict(sep=";", encoding="latin1"),
        ]
        for kw in trials:
            try:
                hdr = pd.read_csv(path, nrows=0, **kw).columns
                cols = [str(c).strip().lower() for c in hdr]
                if cols: break
            except Exception as e:

                logging.warning(f"Exception caught: {e}"); continue
    elif ext in (".xls", ".xlsx"):
        try:
            hdr = pd.read_excel(path, nrows=0).columns
            cols = [str(c).strip().lower() for c in hdr]
        except Exception:
            cols = []
    return cols, ext

def detect_irms_type(path: str) -> str:
    """Return 'IRMS (Thermo DI)', 'IRMS (EA)', or '' if not recognized."""
    cols, ext = _peek_columns(path)
    def has_any(tokens): return any(any(tok in c for c in cols) for tok in tokens)
    ea_tokens = ["d13c", "13c/12c", "d 13c/12c", "d15n", "15n/14n", "d 15n/14n"]
    di_tokens = ["dual inlet", "dual inlet ref", "time code", "d 18o/16o", "d 2h/1h", "d18o", "d2h", "dd/h"]
    scores = {"IRMS (EA)": 3 * int(has_any(ea_tokens)), "IRMS (Thermo DI)": int(has_any(di_tokens))}
    if ext in (".xls", ".xlsx"):
        scores["IRMS (Thermo DI)"] += 1
    if scores["IRMS (EA)"] == 0 and scores["IRMS (Thermo DI)"] == 0:
        return ""
    return max(scores, key=scores.get)

def _extract_sample_id(df: pd.DataFrame) -> pd.Series:
    sid = None
    for c in df.columns:
        cl = str(c).strip().lower()
        if cl in ("identifier 1", "identifier1", "identifier"):
            sid = df[c].astype(str).str.split("/").str[0].str.strip()
            break
    if sid is None:
        obj_cols = [c for c in df.columns if df[c].dtype == object]
        sid = df[obj_cols[0]].astype(str) if obj_cols else pd.Series([np.nan] * len(df))
    return sid

def _extract_timestamp(df: pd.DataFrame) -> Optional[pd.Series]:
    for name in ("Time Code", "Date Analyzed", "Time of analysis", "Date-time Analyzed"):
        for c in df.columns:
            if str(c).strip().lower() == name.lower():
                ts = pd.to_datetime(df[c], errors="coerce")
                return ts
    return None

def _load_csv_any(path: str) -> pd.DataFrame:
    trials = [
        dict(sep=",", encoding="utf-8"),
        dict(sep=",", encoding="latin1"),
        dict(sep=";", encoding="utf-8"),
        dict(sep=";", encoding="latin1"),
    ]
    last_err = None
    for kw in trials:
        try:
            return pd.read_csv(path, **kw)
        except Exception as e:
            last_err = e; continue
    raise last_err if last_err else RuntimeError("Failed to read CSV")

def _load_excel_all_sheets(path: str) -> Tuple[pd.DataFrame, List[str]]:
    xls = pd.ExcelFile(path)
    frames, names = [], []
    for sheet in xls.sheet_names:
        try:
            df = xls.parse(sheet)
            df["sheet"] = sheet
            frames.append(df); names.append(sheet)
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); continue
    data = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()
    return data, names

def _normalize_common_cols(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "sample_id" not in out.columns:
        out["sample_id"] = _extract_sample_id(out)
    if "timestamp" not in out.columns:
        ts = _extract_timestamp(out)
        if ts is not None:
            out["timestamp"] = ts
    return out

def load_irms(path: str, force_type: Optionalstr = None) -> IRMSLoadResult:
    """Load Thermo IRMS DI or EA file, combine Excel sheets, add sample_id/timestamp, return dataclass."""
    cols, ext = _peek_columns(path)
    src = force_type or detect_irms_type(path) or "IRMS (Thermo DI)"  # bias to DI if unknown
    if ext in (".csv", ".txt"):
        data = _load_csv_any(path); sheets = None
    elif ext in (".xls", ".xlsx"):
        data, sheets = _load_excel_all_sheets(path)
    else:
        data = _load_csv_any(path); sheets = None
    data = _normalize_common_cols(data)

    # For DI, try to add canonical d18O/dD/d17O if alt headers exist
    if src == "IRMS (Thermo DI)":
        iso_map = {
            "d18o": "d18O", "d 18o/16o": "d18O",
            "d2h": "dD", "d 2h/1h": "dD", "dd/h": "dD",
            "d17o": "d17O", "d 17o/16o": "d17O",
        }
        low = {str(c).strip().lower(): c for c in data.columns}
        for k, canon in iso_map.items():
            if canon not in data.columns:
                match = next((orig for lowc, orig in low.items() if k in lowc), None)
                if match is not None:
                    data[canon] = pd.to_numeric(data[match], errors="coerce")

    return IRMSLoadResult(vendor_table=data, source_type=src, sheets_read=sheets, path=path)
