"""
irms/processing/calibration.py — Isotope calibration routines for the irms subpackage.
Provides OLS two-point and multi-point calibration fits that map raw measured
delta values to true VSMOW/VPDB values using known reference standards.
"""

from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Iterable, Tuple, Optional
import pandas as pd
import numpy as np

_ISO_DEFAULTS = ("d18O", "dD", "d17O")

def _detect_true_cols(std: pd.DataFrame, iso: str) -> Optionalstr:
    # Find the 'true' column for an isotope in standards dataframe
    cand = []
    for c in std.columns:
        low = str(c).lower().replace(' ', '').replace('_', '')
        iso_low = iso.lower().replace('o','o')  # keep same
        if low in (f"{iso_low}true", f"true{iso_low}", f"{iso_low}vsmow", f"{iso_low}ref", f"true_{iso_low}"):
            return c
        if low == iso_low:
            cand.append(c)
        if low.startswith("delta") and iso_low in low:
            cand.append(c)
    # prefer an exact 'true' column if present, else fallback to plain iso column
    if cand:
        return cand0
    return None

def _ols_fit(meas: pd.Series, true: pd.Series) -> Optional[Tuple[float,float,float,int]]:
    # Fit y_true = a + b * y_meas
    s = pd.concat([meas, true], axis=1).dropna()
    s.columns = ["meas","true"]
    s = s[ np.isfinite(s["meas"]) & np.isfinite(s["true"]) ]
    n = len(s)
    if n < 2:
        return None
    x = s["meas"].to_numpy(dtype=float)
    y = s["true"].to_numpy(dtype=float)
    # OLS closed form
    x_mean = x.mean(); y_mean = y.mean()
    Sxy = ((x - x_mean)*(y - y_mean)).sum()
    Sxx = ((x - x_mean)**2).sum()
    if Sxx == 0:
        b = 1.0; a = y_mean - b * x_mean
    else:
        b = Sxy / Sxx
        a = y_mean - b * x_mean
    # R^2
    y_hat = a + b * x
    ss_res = ((y - y_hat)**2).sum()
    ss_tot = ((y - y_mean)**2).sum()
    r[2] = 1.0 - ss_res/ss_tot if ss_tot != 0 else 1.0
    return a, b, r[2], n

def calibrate_by_standards(df: pd.DataFrame,
                           standards_df: Optional[pd.DataFrame] = None,
                           id_col: str = "sample_id",
                           role_col: Optionalstr = "role_code",
                           isotopes: Iterablestr = _ISO_DEFAULTS) -> tuple[pd.DataFrame, Dict[str, dict]]:
    """Calibrate measured isotope deltas to true scale using standards.

    Returns (df_with_calibrated, fits) where fits maps isotope -> {a,b,r[2],n}
    and df contains columns like 'd18O_calibrated'. For standards rows, also returns
    '{iso}_true' columns and an 'is_standard' boolean when possible.
    """
    out = df.copy()
    fits: Dict[str, dict] = {}

    # Determine standard rows & true values source
    std_rows = pd.DataFrame()
    if standards_df is not None and id_col in standards_df.columns:
        # Merge true values by id_col
        std_rows = standards_df.copy()
        # try to detect true columns
        true_map = {}
        for iso in isotopes:
            col = _detect_true_cols(std_rows, iso)
            if col:
                true_map[iso] = col
        # rename to canonical *_true
        for iso, col in true_map.items():
            if f"{iso}_true" not in std_rows.columns:
                std_rows = std_rows.rename(columns={col: f"{iso}_true"})
        # keep only relevant cols
        keep = id_col + [c for c in std_rows.columns if c.endswith("_true")]
        std_rows = std_rows[keep].drop_duplicates(subset=id_col)
        # mark standards in the main df
        out = out.merge(std_rows, on=id_col, how="left", suffixes=("",""))
        out["is_standard"] = out[[c for c in out.columns if c.endswith("_true")]].notna().any(axis=1)
    elif role_col and role_col in out.columns:
        # use role code to flag standards; true values may not be present
        std_mask = out[role_col].astype(str).str.upper().isin(["IHSTD","SCTRL","AMLIN","CAL","STD","STD1","STD2"])  # extend as needed
        out["is_standard"] = std_mask
    else:
        out["is_standard"] = False

    # For each isotope, fit on available standards with true values
    for iso in isotopes:
        meas_col = iso
        true_col = f"{iso}_true" if f"{iso}_true" in out.columns else None
        if meas_col not in out.columns:
            continue
        if true_col is None:
            # nothing to fit against; skip
            continue
        used = out[out["is_standard"] & out[true_col].notna() & out[meas_col].notna()]
        if used.empty:
            continue
        fit = _ols_fit(used[meas_col], used[true_col])
        if fit is None:
            continue
        a, b, r2, n = fit
        fits[iso] = {"a": float(a), "b": float(b), "r2": float(r2), "n": int(n)}
        # apply to all rows
        out[f"{iso}_calibrated"] = a + b * out[meas_col].astype(float)

    return out, fits
