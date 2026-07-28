"""
Outlier Detection Methods
=========================

Updated: unified constants for LSC / SIAM / NGAM (protocol_gui.py etc.)

Method consolidation
--------------------
Old SIAM name   →  Canonical name              Old LSC name
"SD"            →  "X_STDV (SD)"           ==  "X_STDV"
"MAD"           →  "Modified Z-Score (MAD)" ==  "Modified Z-Score"
"Huber"         →  "Huber"                      (new, added from SIAM)
All others unchanged.

"""
from __future__ import annotations

import pandas as pd
import numpy as np
from scipy.stats import norm, t


# ──────────────────────────────────────────────────────────────────────────────
# UNIFIED CONSTANTS  (single source of truth for all modules)
# ──────────────────────────────────────────────────────────────────────────────

OUTLIER_METHODS = [
    "None",
    "Chauvenet",                  # Probabilistic criterion — recommended for LSC
    "Modified Z-Score (MAD)",     # MAD-based robust filter — was "MAD" (SIAM) / "Modified Z-Score" (LSC)
    "X_STDV (SD)",                # Classic n·σ filter     — was "SD"  (SIAM) / "X_STDV"           (LSC)
    "Huber",                      # M-estimator, iterative downweighting
    "Grubbs",                     # Single-outlier parametric test
    "Dixon",                      # Q-test, best for small samples (n < 10)
    "GESD",                       # Generalised ESD — multiple outliers
]

OUTLIER_DEFAULTS = {
    "method":     "None",
    "threshold":  2.0,            # σ multiplier, Huber constant, or max-N for GESD
    "iterations": 3,              # for iterative methods (Chauvenet, Grubbs, GESD)
}

OUTLIER_TOOLTIP = (
    "Outlier Detection Methods:\n\n"
    "• None: No detection\n"
    "• Chauvenet: Probabilistic criterion (recommended for LSC)\n"
    "• Modified Z-Score (MAD): MAD-based, very robust to extreme values\n"
    "• X_STDV (SD): Classic σ threshold — set 2.0 (≈2σ), 3.0 (≈3σ), etc.\n"
    "• Huber: M-estimator, iterative downweighting of outlier injections\n"
    "• Grubbs: Single-outlier parametric test\n"
    "• Dixon: Q-test, best for small samples (n < 10)\n"
    "• GESD: Generalised ESD — handles multiple outliers (max count = threshold)"
)

# ──────────────────────────────────────────────────────────────────────────────
# BACK-COMPAT  — maps legacy stored strings → canonical OUTLIER_METHODS name
# ──────────────────────────────────────────────────────────────────────────────

_LEGACY_MAP = {
    "SD":               "X_STDV (SD)",
    "MAD":              "Modified Z-Score (MAD)",
    "X_STDV":           "X_STDV (SD)",
    "Modified Z-Score": "Modified Z-Score (MAD)",
    "nsigma":           "X_STDV (SD)",
    "grubbs":           "Grubbs",
}


def normalize_outlier_method(method: str) -> str:
    """
    Return the canonical OUTLIER_METHODS name for any legacy or current value.
    Call when reading protocol settings / QSettings back from storage.

    Examples
    --------
    normalize_outlier_method("SD")               → "X_STDV (SD)"
    normalize_outlier_method("Modified Z-Score") → "Modified Z-Score (MAD)"
    normalize_outlier_method("Chauvenet")         → "Chauvenet"
    normalize_outlier_method("None")              → "None"
    """
    return _LEGACY_MAP.get(method, method)


# ──────────────────────────────────────────────────────────────────────────────
# DETECTION FUNCTION
# ──────────────────────────────────────────────────────────────────────────────

def detect_outliers(df: pd.DataFrame, method: str, threshold: float,
                    target_col: str = 'CPM') -> pd.DataFrame:
    """
    Detect outliers using the specified method.

    Accepts both legacy names (e.g. "SD", "MAD", "X_STDV", "Modified Z-Score")
    and canonical names (e.g. "X_STDV (SD)", "Modified Z-Score (MAD)").

    Parameters
    ----------
    df         : DataFrame with measurement data
    method     : Detection method — see OUTLIER_METHODS for the full list
    threshold  : Method-specific parameter (σ multiplier, Huber constant,
                 or max-outlier count for GESD)
    target_col : Column to analyse for outliers

    Returns
    -------
    DataFrame with an 'IsOutlier' boolean column added/updated.
    """
    df = df.copy()
    df['IsOutlier'] = False

    # Normalise to canonical name so both old and new strings work
    method = normalize_outlier_method(method)

    if method == 'None' or target_col not in df.columns:
        return df

    for pos, group in df.groupby('Position'):
        if len(group) < 3:
            continue

        vals = pd.to_numeric(group[target_col], errors='coerce').dropna().values
        if len(vals) < 3:
            continue

        outlier_mask  = np.zeros(len(group), dtype=bool)
        valid_indices = group[target_col].notna().values

        # ── Chauvenet's Criterion ─────────────────────────────────────────────
        if method == 'Chauvenet':
            mean = np.mean(vals)
            std  = np.std(vals, ddof=1)
            if std > 0:
                n              = len(vals)
                prob_threshold = 1.0 / (2.0 * n)
                z_threshold    = abs(norm.ppf(prob_threshold))
                z_scores       = np.abs(
                    (group.loc[group[target_col].notna(), target_col].values - mean) / std
                )
                outlier_mask[valid_indices] = z_scores > z_threshold

        # ── Modified Z-Score (MAD) ────────────────────────────────────────────
        elif method == 'Modified Z-Score (MAD)':
            med = np.median(vals)
            mad = np.median(np.abs(vals - med))
            if mad > 0:
                z = 0.6745 * np.abs(
                    group.loc[group[target_col].notna(), target_col].values - med
                ) / mad
                outlier_mask[valid_indices] = z > threshold

        # ── X_STDV (SD) ───────────────────────────────────────────────────────
        elif method == 'X_STDV (SD)':
            mean = np.mean(vals)
            std  = np.std(vals, ddof=1)
            if std > 0:
                z = np.abs(
                    group.loc[group[target_col].notna(), target_col].values - mean
                ) / std
                outlier_mask[valid_indices] = z > threshold

        # ── Huber M-estimator ─────────────────────────────────────────────────
        elif method == 'Huber':
            # Iterative reweighted least squares (IRLS) with Huber loss.
            # threshold acts as the Huber tuning constant k (typically 1.5–2.0).
            k    = threshold
            mean = np.mean(vals)
            std  = np.std(vals, ddof=1)
            if std > 0:
                for _ in range(20):          # iterate to convergence
                    r       = (vals - mean) / std
                    w       = np.where(np.abs(r) <= k, 1.0, k / np.abs(r))
                    new_mean = np.average(vals, weights=w)
                    new_std  = np.sqrt(np.average((vals - new_mean) ** 2, weights=w))
                    if np.abs(new_mean - mean) < 1e-8 * std:
                        break
                    mean, std = new_mean, max(new_std, 1e-12)
                r_final = np.abs(
                    group.loc[group[target_col].notna(), target_col].values - mean
                ) / max(std, 1e-12)
                outlier_mask[valid_indices] = r_final > k

        # ── Grubbs' Test ──────────────────────────────────────────────────────
        elif method == 'Grubbs':
            n    = len(vals)
            mean = np.mean(vals)
            std  = np.std(vals, ddof=1)
            if std > 0:
                G          = np.max(np.abs(vals - mean)) / std
                t_dist     = t.ppf(1 - 0.05 / (2 * n), n - 2)
                G_critical = (
                    (n - 1) / np.sqrt(n)
                ) * np.sqrt(t_dist ** 2 / (n - 2 + t_dist ** 2))
                if G > G_critical:
                    extreme_idx            = np.argmax(np.abs(vals - mean))
                    temp_mask              = np.zeros(len(vals), dtype=bool)
                    temp_mask[extreme_idx] = True
                    outlier_mask[valid_indices] = temp_mask

        # ── Dixon's Q-test ────────────────────────────────────────────────────
        elif method == 'Dixon':
            sorted_vals = np.sort(vals)
            n = len(sorted_vals)
            if n >= 3:
                span = sorted_vals[-1] - sorted_vals[0]
                if span > 0:
                    Q_low  = (sorted_vals[1]  - sorted_vals[0])  / span
                    Q_high = (sorted_vals[-1] - sorted_vals[-2]) / span
                    Q_critical = {
                        3: 0.941, 4: 0.765, 5: 0.642, 6: 0.560,
                        7: 0.507, 8: 0.468, 9: 0.437, 10: 0.412,
                    }.get(n, 0.41)
                    if Q_low  > Q_critical:
                        outlier_mask[group[target_col] == sorted_vals[0]]  = True
                    if Q_high > Q_critical:
                        outlier_mask[group[target_col] == sorted_vals[-1]] = True

        # ── GESD ──────────────────────────────────────────────────────────────
        elif method == 'GESD':
            max_outliers   = int(round(threshold)) if threshold >= 1 else int(len(vals) * 0.1)
            remaining_vals = vals.copy()
            remaining_idx  = np.arange(len(vals))
            detected       = []

            for i in range(max_outliers):
                if len(remaining_vals) < 3:
                    break
                n    = len(remaining_vals)
                mean = np.mean(remaining_vals)
                std  = np.std(remaining_vals, ddof=1)
                if std == 0:
                    break
                R         = np.abs(remaining_vals - mean) / std
                max_R_idx = np.argmax(R)
                max_R     = R[max_R_idx]
                alpha     = 0.05 / (2 * (n - i))
                t_val     = t.ppf(1 - alpha, n - 2)
                lambda_i  = ((n - 1) * t_val) / np.sqrt((n - 2 + t_val ** 2) * n)
                if max_R > lambda_i:
                    detected.append(remaining_idx[max_R_idx])
                    remaining_vals = np.delete(remaining_vals, max_R_idx)
                    remaining_idx  = np.delete(remaining_idx,  max_R_idx)
                else:
                    break

            if detected:
                temp_mask = np.zeros(len(vals), dtype=bool)
                for idx in detected:
                    temp_mask[idx] = True
                outlier_mask[valid_indices] = temp_mask

        df.loc[group.index[outlier_mask], 'IsOutlier'] = True

    return df