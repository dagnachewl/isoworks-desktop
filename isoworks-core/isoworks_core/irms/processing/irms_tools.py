"""
irms/processing/irms_tools.py — Low-level IRMS data processing tools.
Provides ratio computation, delta-permil conversion, and linearity correction
functions operating on pandas DataFrames of raw IRMS signal data.
"""
# This file is part of your project and is intended to be distributed under the GPL-2 license.
# See the GNU General Public License version 2 for details.

from __future__ import annotations
import numpy as np
import pandas as pd

def compute_ratios(df: pd.DataFrame, num_col: str, den_col: str, out: str = "ratio"):
    out_df = df.copy()
    out_df[out] = out_df[num_col] / out_df[den_col]
    return out_df

def delta_permil(r_sample: pd.Series, r_standard: float) -> pd.Series:
    return (r_sample / r_standard - 1.0) * 1000.0

def linearity_correction(df: pd.DataFrame, ratio_col: str, intensity_col: str, out: str = "ratio_lin"):
    x = df[intensity_col].to_numpy(dtype=float)
    y = df[ratio_col].to_numpy(dtype=float)
    A = np.vstack([np.ones_like(x), x]).T
    coef, _, _, _ = np.linalg.lstsq(A, y, rcond=None)
    corrected = y - coef[1] * (x - x.mean())
    out_df = df.copy()
    out_df[out] = corrected
    return out_df, dict(intercept=float(coef[0]), slope=float(coef[1]))
