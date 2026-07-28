"""
iso_io/laser_reader.py — Laser isotope instrument file reader for IsoWorks.
Peeks at file headers to identify laser analyser exports (Picarro, OA-ICOS, etc.)
and loads them into a normalised DataFrame via isotope_processor helpers.
"""
# iso_io/laser_reader.py
from __future__ import annotations
from typing import Tuple, List, Optional
import os
import pandas as pd

try:
    from isotope_processor import map_and_clean_raw_data, InstrumentType
    _HAVE_MAP = True
except Exception:
    _HAVE_MAP = False

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

def detect_laser_type(path: str) -> str:
    """Return 'LGR', 'Picarro', or '' if not recognized."""
    cols, _ = _peek_columns(path)
    has = lambda s: any(s in c for c in cols)
    if has("delta 18o/16o") or has("delta d/h") or has("h2o conc"):
        return "LGR"
    if has("d(18_16)mean") or has("d(d_h)mean") or has("h2o_mean"):
        return "Picarro"
    return ""

def _read_csv_any(path: str) -> pd.DataFrame:
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

def load_laser(path: str, label: Optionalstr = None) -> pd.DataFrame:
    """Load LGR or Picarro CSV/TXT and map to canonical columns using isotope_processor."""
    raw = _read_csv_any(path)
    inst = label or detect_laser_type(path)
    if not _HAVE_MAP:
        return raw
    if inst not in ("LGR", "Picarro"):
        inst = "LGR"
    itype = InstrumentType(inst)
    df = map_and_clean_raw_data(raw, itype)
    return df
