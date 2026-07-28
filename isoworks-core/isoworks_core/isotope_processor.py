"""
isotope_processor.py — Core isotope data processing engine for IsoWorks.
Handles loading, cleaning, calibration, drift/memory correction, and
post-processing of stable isotope ratio data (water, EA/IRMS, enrichment).
"""
from __future__ import annotations
import pandas as pd
import numpy as np
import re as re
from enum import Enum
import logging
import numpy as np, pandas as pd

from scipy.optimize import curve_fit
from sklearn.linear_model import LinearRegression

from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional

# --- Flexible standards loader -----------------------------------------------
import json
from pathlib import Path
from typing import Tuple, Dict, Set
from dataclasses import dataclass, field

@dataclass
class IRMSPostConfig:
    # existing fields (kept for compatibility)
    blank_threshold_permil: float = 0.2
    control_z_warn: float = 2.0
    control_z_fail: float = 3.0
    # … your existing fields …
    robust_merge: bool = False          # NEW: accepted name in code
    robust_fits: bool = False           # keep for backward compatibility
    # z_warn, z_fail, outlier_method, outlier_sd, blank_thresholds, etc.

    def use_robust(self) -> bool:
        # Single place of truth
        return bool(self.robust_merge or self.robust_fits)

    # NEW: unified names you asked for
    z_warn: float = 2.0
    z_fail: float = 3.0
    outlier_method: str = "SD"       # e.g. "SD", "MAD", "Huber"
    outlier_sd: float = 2.0
    blank_thresholds: Dict[str, float] = field(default_factory=dict)  # per isotope

    # NEW: preferred peak per isotope (if present in the file)
    ea_peak_preference: Dict[str, int] = field(default_factory=lambda: {
        "d13C": 4, "d15N": 3, "d34S": 1, "d18O": 1, "dD": 1
    })
    
    # Back-compat shims so older code still reads the values
    @property
    def blank_threshold(self) -> float:
        # global fallback if specific iso not set
        return self.blank_threshold_permil

    @property
    def control_z_warn_compat(self) -> float:
        return self.control_z_warn if self.control_z_warn is not None else self.z_warn

    @property
    def control_z_fail_compat(self) -> float:
        return self.control_z_fail if self.control_z_fail is not None else self.z_fail
    def __post_init__(self):
        if self.blank_thresholds is None:
            # sensible defaults per isotope (tweak as you like)
            self.blank_thresholds = {
                "d18O": 0.2,
                "dD":   1.0,
                "d17O": 0.2,
                "d13C": 0.2,
                "d15N": 0.2,
            }

# Canonical EA-capable isotopes (extend as needed)
EA_ISOTOPES = ("d13C", "d15N", "d34S", "d18O", "dD")
            
# Canonical isotope aliases we accept
_ISO_ALIASES = {
    "d2h": "dD", "d_h": "dD", "dd": "dD", "δ2h": "dD", "d 2h/1h": "dD", "d2h/1h": "dD",
    "dh": "dD", "d(d_h)": "dD",
    "d18o": "d18O", "δ18o": "d18O", "d 18o/16o": "d18O", "d(18_16)": "d18O",
    "d17o": "d17O", "δ17o": "d17O",
    "d13c": "d13C", "δ13c": "d13C", "d 13c/12c": "d13C", "d13c/12c": "d13C",
    "d15n": "d15N", "δ15n": "d15N", "d 15n/14n": "d15N", "d15n/14n": "d15N"
}
def _canon_iso(name: str) -> str:
    k = str(name).strip().lower().replace(" ", "")
    return _ISO_ALIASES.get(k, name.strip())

# Role aliases → canonical roles we use in-app
_ROLE_ALIASES = {
    "cal": "calibration", "calib": "calibration", "ihstd": "calibration",
    "ctrl": "control", "sctrl": "control",
    "lin": "linearity", "amlin": "linearity",
    "driftstd": "drift", "drift": "drift",
    "memory": "memory", "mem": "memory",
    "diwsh": "blank", "blank": "blank"
}
def _canon_role(role: str) -> str:
    r = str(role).strip()
    low = r.lower()
    return _ROLE_ALIASES.get(low, r)

def _split_roles(val) -> list[str]:
    if val is None or (isinstance(val, float) and np.isnan(val)): return []
    if isinstance(val, (list, tuple, set)): return [str(x).strip() for x in val if str(x).strip()]
    s = str(val)
    if ";" in s: parts = s.split(";")
    elif "," in s: parts = s.split(",")
    else: parts = s
    return [p.strip() for p in parts if p.strip()]

def _to_long_from_wide(df: pd.DataFrame) -> pd.DataFrame:
    # pick isotopes by *_true columns
    rows = []
    cols = [c for c in df.columns if isinstance(c,str)]
    for c in cols:
        if c.endswith("_true"):
            iso_name = c[:-5]  # strip _true
            unc = f"{iso_name}_uncertainty"
            iso = _canon_iso(iso_name)
            for r in df.to_dict('records'):
                true_val = r.get(c, np.nan)
                if pd.notnull(true_val):
                    rows.append({
                        "sample_id": r.get("sample_id", None),
                        "ref_name": r.get("ref_name", None),
                        "isotope": iso,
                        "true": pd.to_numeric(true_val, errors="coerce"),
                        "uncertainty": pd.to_numeric(r.get(unc, np.nan), errors="coerce") if unc in df.columns else np.nan,
                        "units": r.get("units", None),
                        "scale": r.get("scale", None),
                        "roles": r.get("roles", None)
                    })
    return pd.DataFrame(rows)

def _to_long_from_json(obj) -> pd.DataFrame:
    rows = []
    for entry in obj:
        sid = entry.get("sample_id")
        ref = entry.get("ref_name")
        roles = entry.get("roles", [])
        values = entry.get("values", [])
        for v in values:
            iso = _canon_iso(v.get("isotope"))
            rows.append({
                "sample_id": sid, "ref_name": ref, "isotope": iso,
                "true": v.get("true"), "uncertainty": v.get("uncertainty"),
                "units": v.get("units"), "scale": v.get("scale"),
                "roles": ";".join(roles) if roles else None
            })
    return pd.DataFrame(rows)

def _nan_stats_abs(arr) -> dict:
    """
    Return {'mean_abs': ..., 'median_abs': ..., 'max_abs': ..., 'n': ...}
    without emitting warnings on all-NaN inputs.
    """
    import numpy as np
    a = np.asarray(arr, float)
    m = np.isfinite(a)
    if m.sum() == 0:
        return {"mean_abs": np.nan, "median_abs": np.nan, "max_abs": np.nan, "n": 0}
    aa = np.abs(a[m])
    return {
        "mean_abs": float(np.mean(aa)),
        "median_abs": float(np.median(aa)),
        "max_abs": float(np.max(aa)),
        "n": int(aa.size),
    }

def read_standards_flexible(path: str | Path) -> Tuple[pd.DataFrame, Dict[str, Set[str]]]:
    """
    Returns (standards_long, role_map)
    standards_long columns: sample_id, ref_name, isotope, true, uncertainty, units, scale, roles
    role_map: {role -> {sample_id}}
    """
    p = Path(path)
    text = None
    try:
        if p.suffix.lower() in (".json", ".jsonc"):
            text = p.read_text(encoding="utf-8")
            data = json.loads(text)
            long_df = _to_long_from_json(data)
        else:
            # CSV route
            df = pd.read_csv(p, encoding="utf-8")
            cols = [str(c).strip().lower() for c in df.columns]
            if all(x in cols for x in ["sample_id","isotope","true"]):
                # already long
                long_df = df.rename(columns={c: c.strip() for c in df.columns}).copy()
            else:
                # try wide -> long
                long_df = _to_long_from_wide(df)
    except Exception:
        # Fallback: sniff JSON by content if csv route failed
        if text is None:
            try:
                text = p.read_text(encoding="utf-8")
                data = json.loads(text)
                long_df = _to_long_from_json(data)
            except Exception as e2:
                raise RuntimeError(f"Unable to read standards file: {e2}")

    if long_df is None or long_df.empty:
        raise RuntimeError("Standards file contains no usable entries.")

    # Canonicalize isotopes and clean types
    long_df["isotope"] = long_df["isotope"].map(_canon_iso)
    long_df["true"] = pd.to_numeric(long_df["true"], errors="coerce")
    if "uncertainty" in long_df.columns:
        long_df["uncertainty"] = pd.to_numeric(long_df["uncertainty"], errors="coerce")
    else:
        long_df["uncertainty"] = np.nan
    for c in ["sample_id", "ref_name", "units", "scale"]:
        if c in long_df.columns:
            long_df[c] = long_df[c].astype(str).str.strip()

    # Role map
    role_map: Dict[str, Set[str]] = {}
    if "roles" in long_df.columns:
        for r in long_df.to_dict('records'):
            sid = r.get("sample_id") or r.get("ref_name")
            for raw_role in _split_roles(r.get("roles")):
                role = _canon_role(raw_role)
                role_map.setdefault(role, set()).add(str(sid))

    # Minimal required fields check
    req = {"isotope","true"}
    if not req.issubset(set(map(str, long_df.columns))):
        raise RuntimeError(f"Standards file missing required fields: {req - set(long_df.columns)}")

    return long_df[["sample_id","ref_name","isotope","true","uncertainty","units","scale","roles"]], role_map

# ===== Generic calibration helpers (Laser + IRMS) =====
# Accept common variations for "true" columns coming from LIMS / files (case-insensitive)
_TRUE_ALIASES = {
    "d18o": ["d18o_true", "d18O_true"],
    "dd":   ["dd_true", "dD_true", "d2h_true", "d2H_true"],
    "d17o": ["d17o_true", "d17O_true"],
    "d13c": ["d13c_true", "d13C_true"],
    "d15n": ["d15n_true", "d15N_true"],
}

def _find_true_col(isotope: str, std_df: pd.DataFrame) -> str | None:
    """Return the standards column name containing the 'true' value for this isotope."""
    if std_df is None or getattr(std_df, "empty", True):
        return None
    lower_map = {str(c).lower(): c for c in std_df.columns}
    iso_key = str(isotope).lower()

    candidates = [f"{iso_key}_true"]
    candidates.extend(_TRUE_ALIASES.get(iso_key, []))
    candidates.append(iso_key)  # last-resort: plain isotope column

    for cand in candidates:
        if cand.lower() in lower_map:
            return lower_map[cand.lower()]
    return None

# Prefer corrected columns if present but avoid using *_calibrated as input
_DEFAULT_MEASURED_ORDER = (
    "_memory_drift_corrected",
    "_drift_corrected",
    "_memory_corrected",
    "_measured",
    "_raw",
    "",  # bare e.g., "d18O"
)
# expose the measured-column preference order to callers (GUI)
DEFAULT_MEASURED_ORDER = _DEFAULT_MEASURED_ORDER

def _ensure_sem(df: pd.DataFrame, id_col: str, iso: str, meas_col: str) -> pd.Series:
    
    if id_col not in df.columns or meas_col not in df.columns:
        return pd.Series(np.nan, index=df.index)
    g = df.groupby(id_col)[meas_col].agg(['count', 'std'])
    # SEM = std/sqrt(n); set NaN when n<=1 so we don’t floor to zero
    sem_map = (g['std'] / np.sqrt(g['count'])).where(g['count'] > 1, np.nan)
    return df[id_col].map(sem_map).astype(float)


def _find_measured_col(df: pd.DataFrame, isotope: str, order=_DEFAULT_MEASURED_ORDER) -> str | None:
    """Pick first matching measured column by suffix priority, avoiding *_calibrated."""
    if df is None or getattr(df, "empty", True):
        return None
    cols = set(df.columns)
    base = isotope
    for suf in order:
        name = f"{base}{suf}"
        if name in cols and not name.endswith("_calibrated"):
            return name
    # Loose fallback: first column starting with the isotope base (case-insensitive) not ending with calibrated
    base_l = base.lower()
    for c in df.columns:
        cl = str(c).lower()
        if cl.startswith(base_l) and not cl.endswith("calibrated"):
            return c
    return None

# --- Flexible EA isotope/column detection ---------------------------------

# Regexes for header detection (lowercased, non-alphanumeric stripped)
_ISO_PATTERNS = {
    "d15N": [r"d\s*15n(?:/\s*14n)?", r"d15n", r"δ15n", r"delta\s*15n", r"15n/14n"],
    "d13C": [r"d\s*13c(?:/\s*12c)?", r"d13c", r"δ13c", r"delta\s*13c", r"13c/12c"],
    "d34S": [r"d\s*34s(?:/\s*32s)?", r"d34s", r"δ34s", r"delta\s*34s", r"34s/32s"],
    "d18O": [r"d\s*18o(?:/\s*16o)?", r"d18o", r"δ18o", r"delta\s*18o", r"18o/16o"],
    "dD"  : [r"d\s*(2h|d)(?:/\s*1h)?", r"d2h", r"d d/h", r"δ2h", r"delta\s*d", r"d/h", r"2h/1h"],
}

def _normalize_header(s: str) -> str:
    return re.sub(r"[^a-z0-9/]+", "", str(s).lower())

def _match_iso_column(df: pd.DataFrame, iso: str) -> Optional[str]:
    if df is None or df.empty: return None
    pats = _ISO_PATTERNS.get(iso, [])
    headers_norm = {c: _normalize_header(c) for c in df.columns}
    for col, norm in headers_norm.items():
        for p in pats:
            if re.search(p, norm):
                return col
    # fallback: already canonical column
    if iso in df.columns:
        return iso
    return None

def detect_isotope_and_column(df: pd.DataFrame, forced_iso: Optional[str] = None) -> Tuple[str, Optional[str]]:
    order = (forced_iso,) if forced_iso else EA_ISOTOPES
    for iso in order:
        col = _match_iso_column(df, iso)
        if col: return iso, col
    # default if nothing matched
    return forced_iso or "d15N", None

def detect_all_isotope_columns(df: pd.DataFrame) -> Dict[str, str]:
    """Return {iso: column_name} for all isotopes present."""
    out = {}
    for iso in EA_ISOTOPES:
        col = _match_iso_column(df, iso)
        if col: out[iso] = col
    return out


def cfg_ea_peak(cfg: Optional[IRMSPostConfig], iso: str, options: List[int]) -> Optional[int]:
    if not options: return None
    if isinstance(cfg, IRMSPostConfig):
        pref = cfg.ea_peak_preference.get(iso)
        if pref in options: return pref
    return options[0]

def _ea_ci(df, names):
    if isinstance(names, str): names = names
    lut = {str(c).replace("\xa0"," ").strip().lower(): c for c in df.columns}
    for n in names:
        key = str(n).strip().lower()
        if key in lut: return lut[key]
    return None

def _ea_stringify(s: pd.Series) -> pd.Series:
    def fmt(x):
        if isinstance(x, str): return x
        if pd.isna(x): return ""
        if isinstance(x, (int, np.integer)): return str(int(x))
        if isinstance(x, (float, np.floating)):
            i = int(x); return str(i) if abs(x - i) < 1e-9 else str(x).rstrip("0").rstrip(".")
        return str(x)
    return s.map(fmt)

def _ea_line_col(df: pd.DataFrame):
    for nm in ("Line","line","run_order","Run Order","Time Code","Timecode","timestamp"):
        c = _ea_ci(df, nm)
        if c: return c
    return None

def prepare_ea_single_isotope(
    df: pd.DataFrame,
    isotope: Optional[str] = None,
    peak: Optional[int] = None,
    cfg: Optional[IRMSPostConfig] = None
):
    """
    - Detect (or honor) isotope among EA_ISOTOPES
    - Build sample_id from Identifier 1 (first token before '/')
    - RAW  = all peaks; ANALYSIS = chosen peak (from arg or cfg or first available)
    Returns: df_raw, df_filt, iso, chosen_peak, peak_options
    """
    out = df.copy()
    out.columns = [str(c).replace("\xa0"," ").strip() for c in out.columns]

    # sample_id
    idc = _ea_ci(out, ["Identifier 1","Identifier"])
    out["sample_id"] = _ea_stringify(out[idc]).str.split("/").str[0].str.strip() if idc else ""

    # Peak to Int64
    pkc = _ea_ci(out, ["Peak Nr","Peak No","Peak"])
    if pkc:
        out["Peak"] = pd.to_numeric(out[pkc], errors="coerce").round().astype("Int64")
        try: out[pkc] = pd.to_numeric(out[pkc], errors="coerce").round().astype("Int64")
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
    else:
        out["Peak"] = pd.Series(np.nan, index=out.index, dtype="float")

    # Line to Int64 if present
    lc = _ea_line_col(out)
    if lc:
        out[lc] = pd.to_numeric(out[lc], errors="coerce").round().astype("Int64")

    # Detect isotope + column
    iso, icol = detect_isotope_and_column(out, forced_iso=isotope)

    # Numeric isotope column & drop empties
    if icol and icol in out.columns:
        outiso = pd.to_numeric(out[icol], errors="coerce")
    elif iso in out.columns:
        outiso = pd.to_numeric(out[iso], errors="coerce")
    else:
        outiso = pd.Series(np.nan, index=out.index, dtype="float")
    out = out[~outiso.isna()].copy()

    # Sort + Analysis id
    if lc:
        out = out.sort_values(lc, kind="mergesort").reset_index(drop=True)
    else:
        out = out.reset_index(drop=True)
    if "Analysis" not in out.columns:
        out["Analysis"] = (out["sample_id"] != out["sample_id"].shift()).cumsum()

    # Peak options & chosen
    options = sorted(set(out["Peak"].dropna().astype(int).tolist())) if "Peak" in out.columns else []
    chosen = int(float(peak)) if (peak is not None and str(peak).strip()) else cfg_ea_peak(cfg, iso, options)

    # RAW = all peaks; ANALYSIS = chosen (if any)
    df_raw  = out.copy()
    df_filt = out.loc[out["Peak"] == chosen].copy() if (chosen is not None) else out.copy()

    # Ensure timestamp for plots
    try:
        df_raw  = ensure_timestamp(df_raw)
        df_filt = ensure_timestamp(df_filt)
    except Exception as e:

        logging.warning(f"Exception caught: {e}")

    # Clean IDs
    df_raw["sample_id"]  = _ea_stringify(df_raw["sample_id"]).str.strip()
    df_filt["sample_id"] = _ea_stringify(df_filt["sample_id"]).str.strip()

    return df_raw, df_filt, iso, chosen, options

# ---- helpers for floors/guards ----

_DEFAULT_U_FLOOR = {
    "d18O": 0.05,   # ‰
    "dD":   0.5,    # ‰
    "d17O": 0.05,   # ‰
    "d13C": 0.05,   # ‰
    "d15N": 0.10,   # ‰
}

def _nanmedian_safe(a, default=np.nan):
    a = np.asarray(a, dtype=float)
    a = a[np.isfinite(a)]
    return (np.nan if a.size == 0 else np.nanmedian(a)) if not np.isfinite(default) else (default if a.size == 0 else np.nanmedian(a))

def _sem_series_by_id(df, id_col, mcol):
    # SEM per sample_id; if only one observation -> NaN
    g = df.groupby(id_col)[mcol]
    try:
        return g.sem(ddof=1)
    except Exception as e:

        logging.warning(f"Exception caught: {e}"); return g.std(ddof=1) / np.sqrt(g.count().clip(lower=1))

def _fallback_u_prior(df, id_col, mcol, iso, standards_df=None):
    """
    Per-row prior u: prefer SEM by id; if NaN, fall back to:
      1) median SEM of standards (if available),
      2) median SEM of all ids with n>=2,
      3) per-isotope default floor.
    Returns a Series aligned to df.index.
    """
    sem_by_id = _sem_series_by_id(df, id_col, mcol)

    # median SEM from standards (if present)
    med_std_sem = np.nan
    if isinstance(standards_df, pd.DataFrame) and id_col in standards_df.columns:
        std_ids = standards_df[id_col].astype(str).str.strip().unique()
        med_std_sem = _nanmedian_safe(sem_by_id.reindex(std_ids).values, default=np.nan)

    # median SEM from all ids with n>=2
    counts = df.groupby(id_col)[mcol].count()
    ids_ge2 = counts[counts >= 2].index
    med_all_sem = _nanmedian_safe(sem_by_id.reindex(ids_ge2).values, default=np.nan)

    floor = med_std_sem
    if not np.isfinite(floor):
        floor = med_all_sem
    if not np.isfinite(floor):
        floor = _DEFAULT_U_FLOOR.get(iso, 0.05)

    u_prior = df[id_col].map(sem_by_id)  # align to rows
    return pd.to_numeric(u_prior, errors="coerce").fillna(float(floor)).abs()

def _sigma2_floor_from_weights(u_meas_std, u_ref_std, iso):
    """
    When the fit residual variance sigma² goes to ~0 (e.g., exactly two perfect points),
    use a floor based on the median of (u_meas^2 + u_ref^2). Falls back to isotope default.
    """
    base = np.square(np.asarray(u_meas_std, float)) + np.square(np.asarray(u_ref_std, float))
    med = _nanmedian_safe(base, default=np.nan)
    if not np.isfinite(med) or med <= 0:
        med = (_DEFAULT_U_FLOOR.get(iso, 0.05))**2
    return float(med)

def apply_calibration_generic(
    df: pd.DataFrame,
    standards_df: pd.DataFrame | None,
    *,
    id_col: str = "sample_id",
    isotopes: tuple[str, ...] | None = None,
    measured_order=_DEFAULT_MEASURED_ORDER,
    extra_stage_unc: dict[str, float] | None = None,  # SD from memory/drift (added pre-cal in quadrature)
) -> tuple[pd.DataFrame, dict]:
    """
    Stage-aware calibration with uncertainty propagation using Weighted Least Squares (WLS).

    Guardrails added so unknowns do not end up with zero uncertainty when SEM is missing
    and the calibration fit is perfect (sigma2≈0).

    Returns (calibrated_df, fits).
    """

    out = df.copy()
    fits: dict[str, dict] = {}
    if out is None or getattr(out, "empty", True):
        logging.warning("Calibration skipped: data frame is empty.")
        return out, fits

    if id_col in out.columns:
        out[id_col] = out[id_col].astype(str).str.strip()
    if isinstance(standards_df, pd.DataFrame) and id_col in standards_df.columns:
        standards_df = standards_df.copy()
        standards_df[id_col] = standards_df[id_col].astype(str).str.strip()

    if isotopes is None:
        candidates = ("d18O", "dD", "d17O", "d13C", "d15N")
        isotopes = tuple(i for i in candidates if any(str(c).startswith(i) for c in out.columns))

    extra_stage_unc = extra_stage_unc or {}
    # is_long = isinstance(standards_df, pd.DataFrame) and {"isotope", "true"}.issubset(set(standards_df.columns))

    # Prefer WIDE targets if any iso has '{iso}_true' or '{iso}' present
    has_wide_targets = (
        isinstance(standards_df, pd.DataFrame)
        and any((f"{i}_true" in standards_df.columns) or (i in standards_df.columns) for i in (isotopes or ()))
    )

    # Only consider LONG if wide targets are not available
    is_long = (not has_wide_targets) and isinstance(standards_df, pd.DataFrame) and {"isotope", "true"}.issubset(set(standards_df.columns))

    for iso in isotopes:
        meas_col = _find_measured_col(out, iso, measured_order)
        if meas_col is None:
            logging.debug(f"Calibration: measured column not found for {iso}; skipping.")
            continue

        # ---- per-row prior u (instrumental) with a sensible floor ----
        if f"{iso}_u" in out.columns:
            u_prior = pd.to_numeric(out[f"{iso}_u"], errors="coerce").abs()
            # If zeros present, still lift with floor:
            u_prior = u_prior.mask(~np.isfinite(u_prior) | (u_prior <= 0),
                                   _fallback_u_prior(out, id_col, meas_col, iso, standards_df))
        else:
            u_prior = _fallback_u_prior(out, id_col, meas_col, iso, standards_df)

        stage_sd = float(extra_stage_unc.get(iso, 0.0) or 0.0)
        u_in2 = (u_prior**2) + stage_sd**2

        # No standards → pass-through with the prior u_in
        if not isinstance(standards_df, pd.DataFrame) or standards_df.empty:
            out[f"{iso}_calibrated"] = pd.to_numeric(out[meas_col], errors="coerce")
            out[f"{iso}_u"] = np.sqrt(u_in2.to_numpy())
            fits[iso] = {
                "slope": 1.0, "intercept": 0.0, "r2": float("nan"), "n": 0,
                "meas_col": meas_col, "true_col": None, "unc_col": None,
                "sd_resid": float("nan"), "cov": None, "extra_stage_sd": stage_sd
            }
            continue

        # ---- Build calibration pairs (mean/SEM per standard) ----
        grp_mean = out.groupby(id_col)[meas_col].mean()
        grp_sem  = _sem_series_by_id(out, id_col, meas_col)

        if is_long:
            s = standards_df[standards_df["isotope"].astype(str).str.strip() == iso]
            # NEW: restrict to calibration if available
            cal_flag_candidates = ("is_calibration_id", "is_calibration", "is_calib_id", "is_calib")
            cal_flag_col = next((c for c in cal_flag_candidates if c in s.columns), None)
            if cal_flag_col is not None:
                s = s.loc[pd.to_numeric(s[cal_flag_col], errors="coerce").fillna(0).astype(int) == 1]
            s = s[[id_col, "true"] + (["uncertainty"] if "uncertainty" in s.columns else [])].dropna(subset=["true"])
            s[id_col] = s[id_col].astype(str).str.split("/").str[0].str.strip()
            s = s.drop_duplicates(subset=id_col, keep="first")
            cal = s.set_index(id_col).join(pd.DataFrame({"meas_mean": grp_mean, "u_meas": grp_sem}))
            u_ref = cal["uncertainty"] if "uncertainty" in cal.columns else pd.Series(index=cal.index, data=np.nan)
            true_col_used, unc_col_used = "true", ("uncertainty" if "uncertainty" in s.columns else None)
        else:
            # WIDE standards: accept either '{iso}_true' (LIMS) or '{iso}' (File).
            iso_true = f"{iso}_true"

            # pick target column
            if iso_true in standards_df.columns:
                true_col_used = iso_true
            elif iso in standards_df.columns:
                true_col_used = iso
            else:
                # No suitable target → pass-through
                out[f"{iso}_calibrated"] = pd.to_numeric(out[meas_col], errors="coerce")
                out[f"{iso}_u"] = np.sqrt(u_in2.to_numpy())
                fits[iso] = {
                    "slope": 1.0, "intercept": 0.0, "r2": float("nan"), "n": 0,
                    "meas_col": meas_col, "true_col": None, "unc_col": None,
                    "sd_resid": float("nan"), "cov": None, "extra_stage_sd": stage_sd
                }
                continue

            # optional uncertainty column
            unc_candidates = (f"{iso}_uncertainty", f"{iso}_u", f"{iso}_sigma", f"{iso}_sd")
            unc_col_used = next((c for c in unc_candidates if c in standards_df.columns), None)

            # restrict to calibration standards if a flag exists
            cal_flag_candidates = ("is_calibration_id", "is_calibration", "is_calib_id", "is_calib")
            cal_flag_col = next((c for c in cal_flag_candidates if c in standards_df.columns), None)
            if cal_flag_col is not None:
                s = standards_df.loc[
                    pd.to_numeric(standards_df[cal_flag_col], errors="coerce").fillna(0).astype(int) == 1,
                    [id_col, true_col_used] + ([unc_col_used] if unc_col_used else [])
                ].copy()
                if s.empty:
                    # fallback if flags are present but nothing selected
                    s = standards_df[[id_col, true_col_used] + (unc_col_used if unc_col_used else [])].copy()
            else:
                s = standards_df[[id_col, true_col_used] + (unc_col_used if unc_col_used else [])].copy()

            if s.empty:
                out[f"{iso}_calibrated"] = pd.to_numeric(out[meas_col], errors="coerce")
                out[f"{iso}_u"] = np.sqrt(u_in2.to_numpy())
                fits[iso] = {
                    "slope": 1.0, "intercept": 0.0, "r2": float("nan"), "n": 0,
                    "meas_col": meas_col, "true_col": true_col_used, "unc_col": unc_col_used,
                    "sd_resid": float("nan"), "cov": None, "extra_stage_sd": stage_sd
                }
                continue

            # normalize + dedupe by id
            s[id_col] = s[id_col].astype(str).str.split("/").str[0].str.strip()
            s = s.dropna(subset=true_col_used).drop_duplicates(subset=id_col, keep="first")

            # join with measured mean/SEM per standard id
            cal = s.set_index(id_col).join(pd.DataFrame({"meas_mean": grp_mean, "u_meas": grp_sem}))
            u_ref = cal[unc_col_used] if (unc_col_used and unc_col_used in cal.columns) \
                   else pd.Series(index=cal.index, data=np.nan)

            logging.info(f"Calibration [{iso}]: target='{true_col_used}', meas='{meas_col}', n={cal[['meas_mean']].dropna().shape[0]}")

        # arrays
        x = pd.to_numeric(cal["meas_mean"], errors="coerce").to_numpy()
        y = pd.to_numeric(cal[true_col_used], errors="coerce").to_numpy()
        u_meas_std = pd.to_numeric(cal["u_meas"], errors="coerce").to_numpy()
        u_ref_std  = pd.to_numeric(u_ref, errors="coerce").to_numpy() if u_ref is not None else np.full_like(u_meas_std, np.nan, dtype=float)

        msk = np.isfinite(x) & np.isfinite(y)
        x, y = x[msk], y[msk]
        u_meas_std = u_meas_std[msk]; u_ref_std = u_ref_std[msk]

        if x.size < 2:
            logging.warning(f"Calibration: not enough standard points for {iso} (n={x.size}). Copying measured.")
            out[f"{iso}_calibrated"] = pd.to_numeric(out[meas_col], errors="coerce")
            out[f"{iso}_u"] = np.sqrt(u_in2.to_numpy())
            fits[iso] = {
                "slope": 1.0, "intercept": 0.0, "r2": float("nan"), "n": int(x.size),
                "meas_col": meas_col, "true_col": true_col_used, "unc_col": unc_col_used,
                "sd_resid": float("nan"), "cov": None, "extra_stage_sd": stage_sd
            }
            continue

        # ---- WLS fit y = a*x + b ----
        var_i = np.square(np.nan_to_num(u_meas_std, nan=0.0)) + np.square(np.nan_to_num(u_ref_std,  nan=0.0)) + 1e-8
        w = 1.0 / var_i
        X = np.column_stack([x, np.ones_like(x)])
        WX = X * w[:, None]
        XtWX = X.T @ WX
        XtWy = X.T @ (w * y)
        try:
            theta = np.linalg.solve(XtWX, XtWy)
            a_slope, b_int = float(theta[0]), float(theta[1])
            yhat = a_slope * x + b_int
            res  = y - yhat
            dof  = max(1, x.size - 2)
            sigma2 = float((w * res**2).sum() / dof)
            cov_theta = sigma2 * np.linalg.inv(XtWX)
        except np.linalg.LinAlgError:
            p = np.polyfit(x, y, deg=1)
            a_slope, b_int = float(p[0]), float(p[1])
            yhat = a_slope * x + b_int
            res  = y - yhat
            dof  = max(1, x.size - 2)
            sigma2 = float((res**2).sum() / dof)
            cov_theta = sigma2 * np.linalg.inv(X.T @ X)

        # r^2 (weighted)
        ybar_w = (w * y).sum() / w.sum()
        ss_res_w = float((w * res**2).sum())
        ss_tot_w = float((w * (y - ybar_w)**2).sum())
        r2 = 1.0 - ss_res_w / ss_tot_w if ss_tot_w > 0 else float("nan")

        # ---- Apply to all rows & propagate ----
        meas_all = pd.to_numeric(out[meas_col], errors="coerce")
        m_all = meas_all.fillna(0.0).to_numpy()
        out[f"{iso}_calibrated"] = a_slope * meas_all + b_int

        Va, Vb, Cab = cov_theta[0,0], cov_theta[1,1], cov_theta[0,1]
        u_param2 = Va * m_all**2 + Vb + 2.0 * Cab * m_all

        base_var = (a_slope**2) * u_in2.to_numpy()

        # If sigma2 ≈ 0 (perfect/2-point fit), use a defensible floor
        if not np.isfinite(sigma2) or sigma2 <= 0:
            sigma2 = _sigma2_floor_from_weights(u_meas_std, u_ref_std, iso)

        out[f"{iso}_u"] = np.sqrt(base_var + u_param2 + sigma2)

        fits[iso] = {
            "slope": a_slope, "intercept": b_int, "r2": float(r2), "n": int(x.size),
            "meas_col": meas_col, "true_col": true_col_used, "unc_col": unc_col_used,
            "sd_resid": float(np.sqrt(sigma2)),
            "cov": cov_theta.tolist(),
            "extra_stage_sd": stage_sd,
        }

    return out, fits



def _default_std_mask(df: pd.DataFrame) -> pd.Series:
    """Heuristic: pick standards by role_code for EA/IRMS."""
    if "role_code" not in df.columns:
        return pd.Series(False, index=df.index)
    keys = {"IHSTD","SCTRL","CTRL","CONTROL","CAL","CALIB","CALSTD","STD","QC","QCTRL"}
    return df["role_code"].astype(str).str.upper().isin(keys)


def fit_linearity_ea(df: pd.DataFrame, iso: str, amount_col=None, std_mask=None, model="linear", robust=False):
    """
    Fit isotope vs Amount/Area on standards only.
    model: 'linear' or 'quadratic'
    robust: if True, Huber IRLS; else OLS with covariance
    Returns dict with coeffs, cov, sd_resid, r[2], n, A0 (pivot), amount_col, iso.
    """
    if df is None or df.empty or iso not in df.columns:
        return {}

    amount_col = amount_col or detect_amount_column(df)
    if not amount_col or amount_col not in df.columns:
        return {}

    # Standards mask
    if std_mask is None:
        m = _default_std_mask(df) if "_default_std_mask" in globals() else pd.Series(True, index=df.index)
    else:
        m = std_mask

    x = pd.to_numeric(df.loc[m, amount_col], errors="coerce").to_numpy()
    y = pd.to_numeric(df.loc[m, iso], errors="coerce").to_numpy()
    ok = np.isfinite(x) & np.isfinite(y)
    x, y = x[ok], y[ok]
    if x.size < 3:
        return {}

    A0 = float(np.nanmean(x))  # pivot at mean std amount

    cov = None
    if str(model).lower().startswith("quad"):
        # y = a2*x^2 + a1*x + a[0]
        if robust:
            X = np.column_stack([x**2, x, np.ones_like(x)])
            beta, cov, r, _ = _irls_fit(X, y)
            a2, a1, a0 = map(float, beta)
        else:
            try:
                p, cov = np.polyfit(x, y, deg=2, cov=True)
                a2, a1, a0 = float(p[0]), float(p[1]), float(p[2])
            except TypeError:
                a2, a1, a0 = np.polyfit(x, y, deg=2)
        yhat = a2*x**2 + a1*x + a0
        dof = max(1, x.size - 3)
        coeffs = {"a2": a2, "a1": a1, "a0": a0}
    else:
        # y = a*x + b
        if robust:
            X = np.column_stack([x, np.ones_like(x)])
            beta, cov, r, _ = _irls_fit(X, y)
            a, b = float(beta[0]), float(beta[1])
        else:
            try:
                p, cov = np.polyfit(x, y, deg=1, cov=True)
                a, b = float(p[0]), float(p[1])
            except TypeError:
                a, b = np.polyfit(x, y, deg=1)
        yhat = a*x + b
        dof = max(1, x.size - 2)
        coeffs = {"a": a, "b": b}

    ss_res = float(((y - yhat) ** 2).sum())
    ss_tot = float(((y - np.nanmean(y)) ** 2).sum())
    sd_resid = (ss_res / dof) ** 0.5
    r2 = 1.0 - ss_res/ss_tot if ss_tot > 0 else np.nan

    return {
        "model": "quadratic" if str(model).lower().startswith("quad") else "linear",
        "coeffs": coeffs,
        "cov": None if cov is None else np.asarray(cov, float).tolist(),
        "A0": A0,
        "r2": float(r2),
        "sd_resid": float(sd_resid),
        "n": int(x.size),
        "iso": iso,
        "amount_col": amount_col,
    }

# === IRMS diagnostics: linearity & drift fits (pre/post) ======================
_IRMS_STD_KEYS = {"IHSTD","SCTRL","CTRL","CONTROL","CAL","CALIB","CALSTD","STD","QC","QCTRL"}

# --- Detect most likely Peak Nr per isotope from data ---
def detect_ea_peak_map(
    df: pd.DataFrame,
    peak_col: str = "Peak Nr",
    iso_cols: tuple[str, ...] = ("d13C", "d15N"),
) -> dict[str, int]:
    """
    Heuristic: for each isotope column, pick the Peak Nr where that column
    has the most finite (non-NaN) values among standards/samples.
    Returns dict like {'d13C': 1, 'd15N': 3} if discoverable.
    """
    peak_map: dict[str, int] = {}
    if df is None or df.empty or peak_col not in df.columns:
        return peak_map
    try:
        peak_vals = pd.to_numeric(df[peak_col], errors="coerce")
    except Exception as e:

        logging.warning(f"Exception caught: {e}"); return peak_map

    for iso in iso_cols:
        if iso not in df.columns:
            continue
        sub = df[[peak_col, iso]].copy()
        sub["_is_finite"] = pd.to_numeric(sub[iso], errors="coerce").apply(np.isfinite)
        # count finite values per peak
        counts = sub.groupby(peak_col)["_is_finite"].sum().sort_values(ascending=False)
        if not counts.empty and np.isfinite(counts.index[0]):
            try:
                peak_map[iso] = int(counts.index[0])
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
    return peak_map

# --- Apply per-isotope Peak Nr selection (mask other peaks to NaN) ---
def select_ea_peaks(
    df: pd.DataFrame,
    peak_map: dict[str, int] | None,
    peak_col: str = "Peak Nr",
    also_mask_uncertainties: bool = True,
) -> pd.DataFrame:
    """
    For each isotope in peak_map, keep values only on the specified Peak Nr rows;
    set other rows for that isotope to NaN. Leaves other columns intact.
    """
    if df is None or df.empty or not peak_map or peak_col not in df.columns:
        return df

    out = df.copy()
    pk = pd.to_numeric(out[peak_col], errors="coerce")

    for iso, want in (peak_map or {}).items():
        if iso not in out.columns:
            continue
        mask = (pk == want)
        out.loc[~mask, iso] = np.nan
        # optionally mask partner columns (sd/u) for consistency
        if also_mask_uncertainties:
            for suffix in ("_sd", "_sem", "_u"):
                c = f"{iso}{suffix}"
                if c in out.columns:
                    out.loc[~mask, c] = np.nan
    # If a row has both d13C and d15N masked, we still keep it (time/order context).
    return out

# --- Amount/Area auto-detect ---
def detect_amount_column(df):
    """
    Return the best available 'amount-like' column name, or None.
    Priority: Amount, Area, PeakArea (case-insensitive).
    """
    if df is None or getattr(df, "empty", True):
        return None
    cols = {str(c).strip(): c for c in df.columns}
    # Try common variants, in priority order
    candidates = [
        "Amount", "amount", "AMOUNT",
        "Area", "area", "AREA",
        "PeakArea", "peakarea", "PEAKAREA",
        "Peak_Area", "peak_area", "PEAK_AREA"
    ]
    for c in candidates:
        if c in cols:
            return cols[c]
    # Last resort: any numeric column containing 'amount' or 'area'
    for c in df.columns:
        s = str(c).lower()
        if ("amount" in s or "area" in s):
            return c
    return None

# --- Huber IRLS for robust regression (linear/quadratic) ---
def _irls_fit(X, y, huber_k=1.345, max_iter=50, tol=1e-8):
    """
    Iteratively reweighted least squares with Huber loss.
    X: (n,p) design (including 1 column if intercept desired)
    y: (n,) response
    Returns: beta (p,), cov (p,p), residuals (n,), sigma (scalar)
    """
    import numpy as np
    X = np.asarray(X, float)
    y = np.asarray(y, float).reshape(-1)
    n, p = X.shape
    if n < p + 1:
        # fallback OLS
        XtX = X.T @ X
        beta = np.linalg.lstsq(XtX, X.T @ y, rcond=None)[0] if np.linalg.matrix_rank(XtX) == p else np.linalg.lstsq(X, y, rcond=None)[0]
        r = y - X @ beta
        dof = max(1, n - p)
        sigma2 = float((r @ r) / dof)
        cov = sigma2 * np.linalg.pinv(X.T @ X)
        return beta, cov, r, np.sqrt(sigma2)

    w = np.ones(n, float)
    beta = np.linalg.lstsq(X * w[:, None], y * w, rcond=None)[0]
    for _ in range(max_iter):
        r = y - X @ beta
        # scale via MAD (robust) or fallback std
        med = np.median(r)
        mad = np.median(np.abs(r - med))
        s = 1.4826 * mad if mad > 0 else (np.std(r) if np.std(r) > 0 else 1.0)
        u = r / (s + 1e-12)
        # Huber weights: w = 1 if |u|<=k else k/|u|
        w_new = np.where(np.abs(u) <= huber_k, 1.0, huber_k / (np.abs(u) + 1e-12))
        # Weighted least squares step
        XtW = X.T * w_new
        XtWX = XtW @ X
        XtWy = XtW @ y
        try:
            beta_new = np.linalg.solve(XtWX, XtWy)
        except np.linalg.LinAlgError:
            beta_new = np.linalg.lstsq(XtWX, XtWy, rcond=None)[0]
        if np.max(np.abs(beta_new - beta)) < tol:
            beta = beta_new
            w = w_new
            break
        beta = beta_new
        w = w_new

    r = y - X @ beta
    dof = max(1, n - p)
    sigma2 = float((w * r * r).sum() / dof)
    cov = sigma2 * np.linalg.pinv(X.T @ (w[:, None] * X))
    return beta, cov, r, float(np.sqrt(sigma2))

# --- Timestamp coalescer for EA / generic sources -------------------------
def ensure_timestamp(df):
    """
    Ensure a 'timestamp' column exists and is datetime64ns.
    Tries (case-insensitive):
      - 'timestamp' already present (coerce to datetime)
      - combine 'date' + 'time'
      - just 'date' OR just 'time'
      - fallback from an order column ('line'/'run order'/'injection_no'/'analysis')
        → synthesize a monotonic pseudo-time
    """
    import pandas as pd, numpy as np

    if not isinstance(df, pd.DataFrame) or df.empty:
        return df

    out = df.copy()
    cols = {str(c).strip().lower(): c for c in out.columns}

    def find_exact(*names):
        for n in names:
            nn = str(n).strip().lower()
            if nn in cols:
                return cols[nn]
        return None

    def find_contains(*needles):
        for c in out.columns:
            lc = str(c).strip().lower()
            if any(n in lc for n in needles):
                return c
        return None

    # If already present → standardize name + dtype
    tcol = find_exact("timestamp")
    if tcol:
        if tcol != "timestamp":
            out = out.rename(columns={tcol: "timestamp"})
        out["timestamp"] = pd.to_datetime(out["timestamp"], errors="coerce")
        out["timestamp"] = out["timestamp"].ffill().bfill()
        return out

    # Try date + time
    dcol = find_exact("date") or find_contains("date")
    tcol = find_exact("time") or find_contains(" time")  # avoid matching 'time code' first
    if dcol and tcol:
        out["timestamp"] = pd.to_datetime(
            out[dcol].astype(str).str.strip() + " " + out[tcol].astype(str).str.strip(),
            errors="coerce"
        )

    # Try just date or just time (often Excel already has a datetime-ish serial)
    if "timestamp" not in out.columns or out["timestamp"].isna().all():
        if dcol:
            out["timestamp"] = pd.to_datetime(out[dcol], errors="coerce")
        elif tcol:
            out["timestamp"] = pd.to_datetime(out[tcol], errors="coerce")

    # Fallback: synthesize from an order column
    if "timestamp" not in out.columns or out["timestamp"].isna().all():
        order_col = (find_exact("line", "run order", "injection_no", "injection no", "analysis")
                     or find_contains("line", "order", "injection"))
        if order_col:
            order = pd.to_numeric(out[order_col], errors="coerce")
            # stable ordering even with ties/NaN
            rank = order.rank(method="first").fillna(pd.Series(range(len(out)), index=out.index))
        else:
            rank = pd.Series(range(len(out)), index=out.index)
        base = pd.Timestamp("1970-01-01")
        out["timestamp"] = base + pd.to_timedelta(rank, unit="s")

    return out


def _linear_fit(x: np.ndarray, y: np.ndarray) -> dict:
    mask = np.isfinite(x) & np.isfinite(y)
    x, y = x[mask], y[mask]
    if x.size < 2:
        return {"slope": np.nan, "intercept": np.nan, "r2": np.nan, "sd_resid": np.nan, "n": int(x.size)}
    p, _ = np.polyfit(x, y, 1, cov=False)  # y = a*x + b
    a, b = float(p[0]), float(p[1])
    yhat = a*x + b
    ss_res = float(((y - yhat)**2).sum())
    ss_tot = float(((y - y.mean())**2).sum()) if x.size > 1 else np.nan
    r2 = 1 - ss_res/ss_tot if ss_tot and ss_tot > 0 else np.nan
    dof = max(1, x.size - 2)
    sd = float(np.sqrt(ss_res/dof))
    return {"slope": a, "intercept": b, "r2": r2, "sd_resid": sd, "n": int(x.size)}

def compute_linearity_fits(df: pd.DataFrame, isotopes=("d13C","d15N"), amount_col=None, std_mask=None, model="linear", robust=False):
    fits = {}
    if df is None or getattr(df, "empty", True):
        return fits
    amt = amount_col or detect_amount_column(df)
    for iso in isotopes:
        if any(str(c).startswith(iso) for c in df.columns) and amt in df.columns:
            f = fit_linearity_ea(df, iso, amount_col=amt, std_mask=std_mask, model=model, robust=robust)
            if f: fits[iso] = f
    return fits

def compute_drift_fits(df: pd.DataFrame, isotopes=("d13C","d15N"), axis="order", std_mask=None, robust=False):
    fits = {}
    if df is None or getattr(df, "empty", True):
        return fits
    for iso in isotopes:
        if any(str(c).startswith(iso) for c in df.columns):
            f = fit_drift_ea(df, iso, axis=axis, std_mask=std_mask, robust=robust)
            if f: fits[iso] = f
    return fits


def apply_linearity_ea(df: pd.DataFrame, isotope: str, fit: dict) -> tuple[pd.DataFrame, float, str]:
    """
    Apply linearity correction using either:
      - fit["params"] list  -> linear [c0,c1], quadratic [c0,c[1],c2]
      - fit["coeffs"] dict  -> {'a':slope,'b':intercept} or {'c0','c1','c2'}
    y_corr = y - (f(A) - f(A_ref))
    """
    
    out = df.copy()
    meas_col = fit.get("measured_col") or _find_measured_col(out, isotope)
    if meas_col is None:
        return out, float("nan"), meas_col or ""

    # locate amount column (auto-detect as fallback)
    A_col = fit.get("amount_col") or detect_amount_column(out)
    if not A_col or A_col not in out.columns:
        return out, float("nan"), meas_col

    A = pd.to_numeric(out[A_col], errors="coerce")
    A_ref = float(fit.get("A_ref", float(A.median(skipna=True)))) if hasattr(A, "median") else 0.0
    model = str(fit.get("model", "linear")).lower()

    # unify params
    params = fit.get("params")
    coeffs = fit.get("coeffs")
    if params is None and coeffs is not None:
        # build [c0,c[1],c2?] from dict
        if model == "quadratic":
            c0 = float(coeffs.get("c0", coeffs.get("b", 0.0)))
            c1 = float(coeffs.get("c1", coeffs.get("a", 0.0)))
            c2 = float(coeffs.get("c2", 0.0))
            params = [c0, c1, c2]
        else:
            c0 = float(coeffs.get("b", 0.0))
            c1 = float(coeffs.get("a", 0.0))
            params = [c0, c1]

    if params is None:
        return out, float("nan"), meas_col

    def f(a):
        if model == "quadratic" and len(params) >= 3:
            return params[0] + params[1] * a + params[2] * (a * a)
        else:
            return params[0] + params[1] * a

    corr = f(A) - f(A_ref)
    out_col = f"{isotope}_lin_corrected"
    out[out_col] = pd.to_numeric(out[meas_col], errors="coerce") - corr

    return out, float(fit.get("sd_resid", float("nan"))), out_col


def apply_drift_ea(df: pd.DataFrame, isotope: str, fit: dict) -> tuple[pd.DataFrame, float, str]:
    """
    Apply drift correction using either:
      - fit["params"] list  -> [b_intercept, a_slope] on x (order/time)
      - fit["coeffs"] dict  -> {'a':slope,'b':intercept}, with reference x[0] (fit['t_ref'] or fit['x0'])
    y_corr = y - a*(x - x_ref)
    """
    
    out = df.copy()
    meas_col = fit.get("measured_col") or _find_measured_col(out, isotope)
    if meas_col is None:
        return out, float("nan"), meas_col or ""

    # unify a,b and reference x0/t_ref
    a = b = None
    params = fit.get("params")
    coeffs = fit.get("coeffs")
    if params is not None and len(params) >= 2:
        # params ≈ [b, a] from np.linalg.lstsq or polyfit deg=1 order
        b = float(params[0]); a = float(params[1])
    elif isinstance(coeffs, dict):
        a = float(coeffs.get("a", 0.0))
        b = float(coeffs.get("b", 0.0))

    if a is None:
        return out, float("nan"), meas_col

    # choose axis and x reference
    axis = str(fit.get("axis", "order")).lower()
    x_ref = float(fit.get("t_ref", fit.get("x0", 0.0)))

    if axis.startswith("time") and "timestamp" in out.columns:
        t = pd.to_datetime(out["timestamp"], errors="coerce")
        t[0] = t.min()
        x = (t - t[0]).dt.total_seconds()
        delta = a * (x - 0.0)  # because time fit used seconds-from-first
    else:
        if "injection_no" in out.columns:
            x = pd.to_numeric(out["injection_no"], errors="coerce")
        elif "Line" in out.columns:
            x = pd.to_numeric(out["Line"], errors="coerce")
        else:
            x = pd.Series(range(len(out)), index=out.index, dtype=float)
        delta = a * (x - x_ref)

    base = pd.to_numeric(out.get(f"{isotope}_lin_corrected", out[meas_col]), errors="coerce")
    out_col = f"{isotope}_lin_drift_corrected" if f"{isotope}_lin_corrected" in out.columns else f"{isotope}_drift_corrected"
    out[out_col] = base - delta

    return out, float(fit.get("sd_resid", float("nan"))), out_col


def fit_drift_ea(df: pd.DataFrame, iso: str, axis="order", std_mask=None, robust=False):
    """
    Linear drift: y ~ a*x + b, on standards only.
    axis: 'order' (1..N) or 'time' (seconds since first timestamp)
    robust: if True, Huber IRLS
    """
    
    if df is None or df.empty or iso not in df.columns:
        return {}

    if str(axis).lower().startswith("time"):
        if "timestamp" not in df.columns:
            return {}
        t = pd.to_datetime(df["timestamp"], errors="coerce").astype("int64") / 1e9
        x = t.to_numpy()
        xlabel = "time"
    else:
        x = np.arange(1, len(df)+1, dtype=float)
        xlabel = "order"

    # Standards
    if std_mask is None:
        m = _default_std_mask(df) if "_default_std_mask" in globals() else pd.Series(True, index=df.index)
    else:
        m = std_mask

    xm = pd.to_numeric(pd.Series(x, index=df.index).loc[m], errors="coerce").to_numpy()
    ym = pd.to_numeric(df.loc[m, iso], errors="coerce").to_numpy()
    ok = np.isfinite(xm) & np.isfinite(ym)
    xm, ym = xm[ok], ym[ok]
    if xm.size < 3:
        return {}

    x_ref = float(np.nanmean(xm))
    cov = None
    if robust:
        X = np.column_stack([xm, np.ones_like(xm)])
        beta, cov, _r, _ = _irls_fit(X, ym)
        a, b = float(beta[0]), float(beta[1])
    else:
        try:
            p, cov = np.polyfit(xm, ym, deg=1, cov=True)
            a, b = float(p[0]), float(p[1])
        except TypeError:
            a, b = np.polyfit(xm, ym, deg=1)

    yhat = a*xm + b
    dof = max(1, xm.size - 2)
    ss_res = float(((ym - yhat) ** 2).sum())
    ss_tot = float(((ym - np.nanmean(ym)) ** 2).sum())
    sd_resid = (ss_res / dof) ** 0.5
    r2 = 1.0 - ss_res/ss_tot if ss_tot > 0 else np.nan

    return {
        "model": "linear",
        "coeffs": {"a": a, "b": b},
        "cov": None if cov is None else np.asarray(cov, float).tolist(),
        "x0": x_ref,
        "axis": "time" if xlabel == "time" else "order",
        "r2": float(r2),
        "sd_resid": float(sd_resid),
        "n": int(xm.size),
        "iso": iso
    }

# Canonical isotopes we care about; we’ll only operate on those present
CANON_ISOTOPES = ["d18O", "dD", "d17O", "d13C", "d15N"]

def _present_isotopes(df: pd.DataFrame) -> List[str]:
    if df is None or df.empty:
        return []
    cols = set(df.columns.astype(str))
    out = []
    for iso in CANON_ISOTOPES:
        if iso in cols or f"{iso}_calibrated" in cols:
            out.append(iso)
    return out

def _meas_col_for(df: pd.DataFrame, iso: str) -> str:
    return f"{iso}_calibrated" if f"{iso}_calibrated" in df.columns else iso

def _true_col_for(df: pd.DataFrame, iso: str) -> Optional[str]:
    col = f"{iso}_true"
    return col if col in df.columns else None

def _coerce_numeric(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s, errors="coerce")

def _mad(a: np.ndarray) -> float:
    med = np.nanmedian(a)
    return np.nanmedian(np.abs(a - med))

def _merge_replicates(df: pd.DataFrame, iso: str, config: IRMSPostConfig) -> pd.DataFrame:
    """
    Merge replicates per sample_id for one isotope.
    Returns a DataFrame: sample_id, iso_mean, iso_sd, iso_sem, iso_n
    """
    mcol = _meas_col_for(df, iso)
    if mcol not in df.columns:
        return pd.DataFrame(columns=["sample_id", f"{iso}", f"{iso}_sd", f"{iso}_sem", f"{iso}_n"])

    work = df[["sample_id", mcol]].copy()
    work[mcol] = _coerce_numeric(work[mcol])
    grp = work.groupby("sample_id", dropna=False)

    # if config.robust_merge:
    if bool(getattr(config, "robust_merge", False) or getattr(config, "robust_fits", False)):
        # robust center = median; robust spread ~ 1.4826*MAD
        center = grp[mcol].median()
        spread = 1.4826 * grp[mcol].apply(lambda x: _mad(x.to_numpy()))
        n = grp[mcol].count()
        sem = spread / np.sqrt(n.replace(0, np.nan))
        out = pd.DataFrame({
            "sample_id": center.index,
            f"{iso}": center.values,
            f"{iso}_sd": spread.values,
            f"{iso}_sem": sem.values,
            f"{iso}_n": n.values
        })
    else:
        mean = grp[mcol].mean()
        sd = grp[mcol].std(ddof=1)
        n = grp[mcol].count()
        sem = sd / np.sqrt(n.replace(0, np.nan))
        out = pd.DataFrame({
            "sample_id": mean.index,
            f"{iso}": mean.values,
            f"{iso}_sd": sd.values,
            f"{iso}_sem": sem.values,
            f"{iso}_n": n.values
        })
    return out

def _qc_from_standards(df: pd.DataFrame, iso: str, config: IRMSPostConfig) -> Tuple[pd.DataFrame, Dict[str, float]]:
    """
    Compute residuals & z for standards (rows with true values).
    Returns (qc_df, batch_stats) where:
      - qc_df: sample_id, ref_name, role_code?, measured, true, residual, z
      - batch_stats: dict with sd_resid etc.
    """
    mcol = _meas_col_for(df, iso)
    tcol = _true_col_for(df, iso)
    if (mcol not in df.columns) or (tcol is None) or (tcol not in df.columns):
        return pd.DataFrame(columns=["sample_id", "ref_name", mcol, tcol, "residual", "z"]), {"sd_resid": np.nan}

    sub = df.loc[df[tcol].notna(), ["sample_id", "ref_name"] + ([ "role_code"] if "role_code" in df.columns else []) + [mcol, tcol]].copy()
    sub[mcol] = _coerce_numeric(sub[mcol]); sub[tcol] = _coerce_numeric(sub[tcol])
    sub["residual"] = sub[mcol] - sub[tcol]

    # batch SD of residuals (robust if you want)
    if config.robust_merge:
        sd_resid = 1.4826 * _mad(sub["residual"].to_numpy())
    else:
        sd_resid = float(np.nanstd(sub["residual"].to_numpy(), ddof=1))

    z = sub["residual"] / (sd_resid if sd_resid and np.isfinite(sd_resid) and sd_resid > 0 else np.nan)
    sub["z"] = z

    return sub, {"sd_resid": sd_resid}

def _flag_blanks(results_df: pd.DataFrame, iso: str, config: IRMSPostConfig) -> pd.Series:
    """
    Flag non-zero blanks using ref_name or role_code when recognizable (e.g., BLK, DIW, AIR).
    """
    thr = config.blank_thresholds.get(iso, 0.2)
    col = f"{iso}"
    if col not in results_df.columns:
        return pd.Series(False * len(results_df), index=results_df.index)

    is_blank = pd.Series(False, index=results_df.index)
    # heuristics for blanks: ref_name contains BLK, DIW, AIR, etc.
    if "ref_name" in results_df.columns:
        is_blank = results_df["ref_name"].astype(str).str.upper().str.contains(r"BLK|BLANK|DIW|AIR|EMPTY")
    elif "sample_id" in results_df.columns:
        is_blank = results_df["sample_id"].astype(str).str.upper().str.contains(r"BLK|BLANK|DIW|AIR|EMPTY")

    non_zero_blank = is_blank & results_df[col].abs().gt(thr)
    return non_zero_blank

def _assign_flags(results_df: pd.DataFrame, qc_by_iso: Dict[str, pd.DataFrame], batch_stats: Dict[str, Dict[str, float]], config: IRMSPostConfig) -> pd.DataFrame:
    """
    Create a compact flag string per row summarizing issues per isotope.
    e.g., 'd18O:Z>3; dD:BLK'
    """
    flags = []
    for row in results_df.to_dict('records'):
        per_iso_flags = []
        for iso in _present_isotopes(results_df):
            # blanks
            if row.get(f"{iso}_blank_fail", False):
                per_iso_flags.append(f"{iso}:BLK")
            # z from qc table: if this sample is a standard, qc_by_iso will have z; for unknowns no z
            # we don’t try to compute z for unknowns here
        flags.append("; ".join(per_iso_flags))
    results_df["flags"] = flags
    return results_df

def _ea_gas_suffix_map(df: pd.DataFrame) -> dict[str, str]:
    """Map predominant gas → column suffix, e.g. {'N2': '', 'CO2': '.1'}."""
    out = {}
    for c in df.columns:
        if c.startswith("Gasconfiguration"):
            suf = c[len("Gasconfiguration"):]  # '' or '.1'
            mode = df[c].astype(str).str.strip().mode()
            gas = mode.iloc[0] if not mode.empty else None
            if gas:
                out[gas] = suf
    return out

def pick_ea_peak_cols(df: pd.DataFrame, iso_cols=("d13C","d15N")) -> dict[str, str]:
    """
    Choose the correct Peak Nr column per isotope:
      d13C → gas CO2 → 'Peak Nr' + CO2 suffix (likely '.1')
      d15N → gas N2  → 'Peak Nr' + N2 suffix (likely '')
    """
    iso2gas = {"d13C": "CO2", "d15N": "N2"}
    gas2suf = _ea_gas_suffix_map(df)
    out = {}
    for iso in iso_cols:
        gas = iso2gas.get(iso)
        suf = gas2suf.get(gas, "")
        col = f"Peak Nr{suf}"
        if col in df.columns:
            outiso = col
    return out

def pick_ea_amount_cols(df: pd.DataFrame, iso_cols=("d13C","d15N")) -> dict[str, str]:
    """Same logic for Amount vs Amount.1."""
    iso2gas = {"d13C": "CO2", "d15N": "N2"}
    gas2suf = _ea_gas_suffix_map(df)
    out = {}
    for iso in iso_cols:
        suf = gas2suf.get(iso2gas.get(iso, ""), "")
        col = f"Amount{suf}"
        if col not in df.columns and suf == "":
            col = "Amount"
        if col in df.columns:
            outiso = col
    return out

def select_ea_peaks_flex(
    df: pd.DataFrame,
    peak_map: dict[str, int],
    peak_col_map: dict[str, str],
    *,
    also_mask_uncertainties: bool = True,
) -> pd.DataFrame:
    """
    Keep rows for each isotope only where its OWN peak column equals the chosen value.
    Example: {'d13C':4} + {'d13C':'Peak Nr.1'} will mask d13C everywhere except Peak Nr.1==4.
    """
    if df is None or df.empty or not peak_map or not peak_col_map:
        return df
    out = df.copy()
    for iso, want in peak_map.items():
        col = peak_col_map.get(iso)
        if iso not in out.columns or col not in out.columns:
            continue
        pk = pd.to_numeric(out[col], errors="coerce")
        keep = (pk == float(want))
        out.loc[~keep, iso] = np.nan
        if also_mask_uncertainties:
            for sfx in ("_sd", "_sem", "_u"):
                c = f"{iso}{sfx}"
                if c in out.columns:
                    out.loc[~keep, c] = np.nan
    return out

def compute_blank_diagnostics(results_df: pd.DataFrame, iso_list=None,
                              blank_keywords=("BLK","BLANK","DIW","AIR","EMPTY")) -> pd.DataFrame:
    """
    Summarize blanks at the post-processed (merged) level.
    Returns: rows per (isotope, is_blank) with counts and summary stats.
    """
    if results_df is None or results_df.empty:
        return pd.DataFrame()

    # detect isotopes present
    if not iso_list:
        iso_list = []
        for iso in ["d18O","dD","d17O","d13C","d15N"]:
            if iso in results_df.columns or f"{iso}_calibrated" in results_df.columns:
                iso_list.append(iso)

    # blank flag by ref_name or sample_id
    is_blank = pd.Series(False, index=results_df.index)
    if "ref_name" in results_df.columns:
        s = results_df["ref_name"].astype(str).str.upper()
        for k in blank_keywords:
            is_blank = is_blank | s.str.contains(k)
    elif "sample_id" in results_df.columns:
        s = results_df["sample_id"].astype(str).str.upper()
        for k in blank_keywords:
            is_blank = is_blank | s.str.contains(k)

    rows = []
    for iso in iso_list:
        if iso not in results_df.columns:
            continue
        sub = results_df[iso].copy()
        sub["is_blank"] = is_blank.values
        # stats on absolute values for blanks
        for flag in (True, False):
            part = sub[sub["is_blank"] == flag][iso]
            if part.empty:
                continue
            stats = _nan_stats_abs(part)
            rows.append({
                "isotope": iso,
                "is_blank": flag,
                "n": int(part.shape[0]),
                "mean_abs": stats["mean_abs"], #float(np.nanmean(np.abs(part))),
                "p95_abs": float(np.nanpercentile(np.abs(part), 95)),
                "max_abs": stats["max_abs"] #float(np.nanmax(np.abs(part)))
            })
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(["isotope","is_blank"], ascending=[True, False])
    return out


def compute_carryover_candidates(analysis_df: pd.DataFrame, isotope: str,
                                 time_col="timestamp", id_col="sample_id",
                                 top_n: int = 30) -> pd.DataFrame:
    """
    Very simple carryover heuristic at analysis level:
      - sort by time, compute delta jump between consecutive rows
      - return top-N largest |Δ| for the chosen isotope
    Columns returned: prev_id, prev_val, curr_id, curr_val, jump, time_delta_s
    """
    if analysis_df is None or analysis_df.empty or isotope not in analysis_df.columns:
        return pd.DataFrame()

    df = analysis_df[[id_col, isotope] + (time_col if time_col in analysis_df.columns else [])].copy()
    df = df.dropna(subset=isotope).reset_index(drop=True)

    # sort by time if available
    if time_col in df.columns:
        try:
            df[time_col] = pd.to_datetime(df[time_col], errors="coerce")
            df = df.sort_values(time_col).reset_index(drop=True)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

    prev_val = df[isotope].shift(1)
    prev_id  = df[id_col].shift(1).astype(str)
    jump = (df[isotope] - prev_val).abs()
    if time_col in df.columns:
        time_delta = (df[time_col] - df[time_col].shift(1)).dt.total_seconds()
    else:
        time_delta = pd.Series([np.nan]*len(df))

    out = pd.DataFrame({
        "prev_id": prev_id,
        "prev_val": prev_val,
        "curr_id": df[id_col].astype(str),
        "curr_val": df[isotope],
        "jump": jump,
        "time_delta_s": time_delta
    }).dropna(subset=["prev_id","prev_val","curr_id","curr_val","jump"])

    out = out.sort_values("jump", ascending=False).head(top_n).reset_index(drop=True)
    return out


def compute_control_stats(qc_df: pd.DataFrame) -> pd.DataFrame:
    """
    Summarize QC measurements by (isotope, ref_name):
      - n, mean residual, sd residual, %>|z|>2, %>|z|>3
    Requires qc_df from postprocess_irms (has 'residual','z','ref_name','isotope').
    """
    if qc_df is None or qc_df.empty:
        return pd.DataFrame()

    need = {"isotope","ref_name","residual","z"}
    if not need.issubset(set(map(str, qc_df.columns))):
        return pd.DataFrame()

    df = qc_df.copy()
    g = df.groupby(["isotope","ref_name"], dropna=False)
    out = g.apply(lambda s: pd.Series({
        "n": int(s.shape[0]),
        "mean_residual": float(np.nanmean(s["residual"])),
        "sd_residual": float(np.nanstd(s["residual"], ddof=1)) if s.shape[0] > 1 else np.nan,
        "pct_z_gt2": float(np.mean(np.abs(s["z"]) > 2)*100.0),
        "pct_z_gt3": float(np.mean(np.abs(s["z"]) > 3)*100.0),
    })).reset_index()
    return out

def postprocess_irms(
    df: pd.DataFrame,
    config: Optional[IRMSPostConfig] = None
) -> Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Main entry point (call this after calibration/corrections).
    Expects wide columns where possible; uses *_calibrated if present.

    Returns:
      results_df  : one row per sample_id with merged values, SD, SEM, N, uncertainty
      qc_df       : all standards rows with residuals and z per isotope
      batch_stats : per-isotope summary (sd_resid, n_standards)
    """
    if config is None:
        config = IRMSPostConfig()
    if df is None or df.empty:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    df = df.copy()
    # Ensure sample_id exists and is string
    if "sample_id" in df.columns:
        df["sample_id"] = df["sample_id"].astype(str).str.strip()

    isotopes = _present_isotopes(df)
    if not isotopes:
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    # 1) Merge replicates per isotope
    merged_frames = []
    for iso in isotopes:
        merged_frames.append(_merge_replicates(df, iso, config))
    results_df = None
    for m in merged_frames:
        results_df = m if results_df is None else results_df.merge(m, on="sample_id", how="outer")

    # bring through useful metadata for display (first occurrence)
    meta_cols = [c for c in ["ref_name", "role_code"] if c in df.columns]
    if meta_cols:
        meta = df.groupby("sample_id", as_index=False)[meta_cols].first()
        results_df = results_df.merge(meta, on="sample_id", how="left")

    # 2) QC on standards per isotope
    qc_tables = {}
    batch_summary_rows = []
    for iso in isotopes:
        qc_iso, stats = _qc_from_standards(df, iso, config)
        qc_tables[iso] = qc_iso
        batch_summary_rows.append({
            "isotope": iso,
            "n_standards": int(qc_iso.shape[0]) if qc_iso is not None else 0,
            "sd_resid": stats.get("sd_resid", np.nan)
        })

        # 2a) Attach blank flags to results (per isotope)
        # If ref_name suggests blank AND |delta| > threshold -> fail
        blank_fail = _flag_blanks(results_df, iso, config)
        results_df[f"{iso}_blank_fail"] = blank_fail

        # 2b) Combined uncertainty per isotope: sqrt(SEM^2 + sd_resid^2)
        sem_col = f"{iso}_sem"
        u_col = f"{iso}_u"
        sd_resid = stats.get("sd_resid", np.nan)
        if sem_col in results_df.columns:
            results_df[u_col] = np.sqrt(np.square(results_df[sem_col].astype(float)) + (sd_resid ** 2 if np.isfinite(sd_resid) else 0.0))
        else:
            results_df[u_col] = sd_resid if np.isfinite(sd_resid) else np.nan

    batch_summary = pd.DataFrame(batch_summary_rows)

    # 3) Optional: outlier tagging (replicate-level) — minimalist version using merged SEM/z on standards only
    # You can extend this tomorrow for sophisticated rules.

    # 4) Final flag string
    results_df = _assign_flags(results_df, qc_tables, {k: {"sd_resid": v["sd_resid"] if "sd_resid" in v else np.nan} for k, v in zip(isotopes, batch_summary_rows)}, config)

    # Sort for readability
    if "ref_name" in results_df.columns:
        results_df = results_df.sort_values(["ref_name", "sample_id"])
    else:
        results_df = results_df.sort_values("sample_id")

    # Make sure dtypes are friendly
    for iso in isotopes:
        for col in [f"{iso}", f"{iso}_sd", f"{iso}_sem", f"{iso}_n", f"{iso}_u"]:
            if col in results_df.columns:
                results_df[col] = pd.to_numeric(results_df[col], errors="coerce")

    # Concatenate QC tables for output
    qc_df = pd.concat([qc_tables[iso].assign(isotope=iso) for iso in isotopes if isinstance(qc_tables.get(iso), pd.DataFrame)], ignore_index=True) \
            if any(isinstance(qc_tables.get(iso), pd.DataFrame) for iso in isotopes) else pd.DataFrame()

    return results_df, qc_df, batch_summary

class InstrumentType(Enum):
    LGR = "LGR"
    Picarro = "Picarro"
    IRMS_EA = "IRMS (EA)"
class Config:
    def __init__(self):
        self.outlier_threshold = 2.0
        self.outlier_sd = 2.0
        self.outlier_method = "X_STDV (SD)"
        self.memory_thresholds = {'d18O': 0.15, 'dD': 0.4}
        self.valids = 3
        self.memory_carryover_injections = 4


def map_and_clean_raw_data(data, instrument_type):
    """
    Normalize raw data columns to internal schema for each instrument.
    Guarantees (when inferable): 'sample_id', 'Analysis', and 'timestamp'.
    """

    # ---------- IRMS (EA) short-circuit ----------
    try:
        inst_val = instrument_type.value if hasattr(instrument_type, "value") else str(instrument_type)
    except Exception:
        inst_val = str(instrument_type)

    if inst_val == "IRMS (EA)":
        df = data.copy()
        # Rename raw Isodat delta column names to canonical pipeline names
        _ea_delta_rename = {
            "d 15N/14N": "d15N", "d 13C/12C": "d13C",
            "d 2H/1H":   "dD",   "d 18O/16O": "d18O",
            "d 17O/16O": "d17O",
        }
        rename_map = {col: canon for col, canon in _ea_delta_rename.items() if col in df.columns}
        if rename_map:
            df = df.rename(columns=rename_map)
        # Ensure a sample_id exists
        if 'sample_id' not in df.columns:
            for cand in ["Identifier 1", "Identifier", "Sample", "Name", "ID", "identifier 1", "identifier"]:
                if cand in df.columns:
                    df = df.rename(columns={cand: "sample_id"})
                    break
            if 'sample_id' not in df.columns:
                df["sample_id"] = [f"S{i+1}" for i in range(len(df))]

        # Ensure Analysis exists (simple grouping by sample changes)
        if 'Analysis' not in df.columns:
            df['Analysis'] = (df['sample_id'] != df['sample_id'].shift()).cumsum()

        # Try to build timestamp from Date.1/Time.1 if not present
        if 'timestamp' not in df.columns and ('Date.1' in df.columns or 'Time.1' in df.columns):
            def _parse_row(r):
                d = str(r.get("Date.1","")).strip()
                t = str(r.get("Time.1","")).strip()
                for fmt in ("%m/%d/%y %H:%M:%S","%m/%d/%Y %H:%M:%S"):
                    try:
                        return pd.to_datetime(f"{d} {t}", format=fmt, errors="coerce")
                    except Exception as e:

                        logging.warning(f"Exception caught: {e}")
                return pd.to_datetime(f"{d} {t}", errors="coerce")
            try:
                df["timestamp"] = df.apply(_parse_row, axis=1)
            except Exception as e:

                logging.warning(f"Exception caught: {e}")

        return df

    # ---------- LGR / Picarro path (your working mapping) ----------
    column_map_config = {
        InstrumentType.LGR: {
            'date-time analyzed': 'timestamp',
            'h2o conc': 'water_conc',
            'Amount': 'Amount',
            'delta 18o/16o': 'd18O',
            'delta d/h': 'dD',
            'delta 17o/16o': 'd17O',
            'identifier': 'sample_id',
            'injection no': 'injection_no',
            'analysis': 'Analysis',
            'ignore': 'ignore'
        },
        InstrumentType.Picarro: {
            'time code': 'timestamp',
            'h2o_mean': 'water_conc',
            'Amount': 'Amount',
            'd(18_16)mean': 'd18O',
            'd(d_h)mean': 'dD',
            'd(17_16)mean': 'd17O',
            'identifier 1': 'sample_id',
            'inj nr': 'injection_no',
            'analysis': 'Analysis',
            'ignore': 'ignore'
        }
    }

    instrument_map = {k.lower(): v for k, v in column_map_config[instrument_type].items()}

    df = data.copy()
    df.columns = df.columns.str.lower().str.strip()

    rename_dict = {col: instrument_map[col] for col in df.columns if col in instrument_map}
    df.rename(columns=rename_dict, inplace=True)

    numeric_cols = ['water_conc', 'd18O', 'dD', 'd17O', 'injection_no', 'Amount']
    for col in numeric_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')

    if 'sample_id' in df.columns:
        df['sample_id'] = df['sample_id'].astype(str).str.split('/').str[0].str.strip()
    else:
        raise KeyError("'Identifier' column not found in data file.")

    if 'timestamp' in df.columns:
        df['timestamp'] = pd.to_datetime(df['timestamp'], errors='coerce')
        df['timestamp'].ffill(inplace=True)
    else:
        raise KeyError("'Date-time Analyzed' or 'time code' column not found.")

    if 'Analysis' not in df.columns:
        if 'sample_id' in df.columns:
            df['Analysis'] = (df['sample_id'] != df['sample_id'].shift()).cumsum()
        else:
            raise KeyError("Cannot create 'Analysis' column because 'sample_id' is missing.")

    return df
   
def fit_two_pool_memory_factors(data, isotope_col, memory_ids, config):

    if not memory_ids:
        logging.warning("No memory standards selected for carryover characterization.")
        return None, None

    # Restrict to first block and selected memory standards
    df = data[data.get('block_no', 1) == 1].copy()
    df = df[df['sample_id'].astype(str).isin([str(x) for x in memory_ids])]

    # Build consecutive (prev → curr) pairs on the Analysis id (may be non-numeric!).
    # sort=False preserves the chronological order of first appearance (run_order sort
    # was applied to the full DataFrame before this function is called).
    analysis_summary = df.groupby('Analysis', as_index=False, sort=False).first().copy()
    analysis_summary['prev_sample_id'] = analysis_summary['sample_id'].shift(1)
    analysis_summary['prev_analysis_id'] = analysis_summary['Analysis'].shift(1)
    valid_pairs = analysis_summary[
        (analysis_summary['sample_id'] != analysis_summary['prev_sample_id'])
        & analysis_summary['prev_analysis_id'].notna()
    ]
    if valid_pairs.empty:
        logging.warning("No consecutive, different memory standards found in the first block.")
        return None, None

    # Stable average of the last N valids per Analysis
    try:
        n_tail = int(getattr(config, "valids", 3))
        if n_tail <= 0:
            n_tail = 3
    except Exception:
        n_tail = 3

    df[isotope_col] = pd.to_numeric(df[isotope_col], errors="coerce")
    df['stable_avg'] = df.groupby('Analysis', sort=False)[isotope_col].transform(lambda x: x.tail(n_tail).mean())
    stable_vals = df.groupby('Analysis', as_index=True, sort=False)['stable_avg'].first()

    # Build per-block fractions (curr normalized by prev & curr stable levels)
    all_fractions = []
    for row in valid_pairs.to_dict('records'):
        curr_id = row['Analysis']
        prev_id = row['prev_analysis_id']
        try:
            prev_stable = float(stable_vals.loc[prev_id])
            curr_stable = float(stable_vals.loc[curr_id])
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); continue
        # skip zero contrast
        denom = (prev_stable - curr_stable)
        if not np.isfinite(denom) or abs(denom) < 1e-12:
            continue

        curr_block = df[df['Analysis'] == curr_id].copy()
        # Coerce injection_no to numeric (robust to strings)
        curr_block['injection_no'] = pd.to_numeric(curr_block.get('injection_no', np.arange(1, len(curr_block)+1)),
                                                   errors='coerce')
        curr_block = curr_block.dropna(subset=['injection_no', isotope_col])
        if curr_block.empty:
            continue

        block_fractions = (curr_block[isotope_col] - curr_stable) / denom
        block_fractions.index = curr_block['injection_no'].round().astype(int)
        all_fractions.append(block_fractions)

    if not all_fractions:
        logging.warning("No valid high-contrast pairs found for memory characterization.")
        return None, None

    # Average fraction per injection_no across blocks
    fractions_df = pd.concat(all_fractions, axis=1)
    avg_factors = fractions_df.mean(axis=1)

    # Prepare fit data (numeric, sorted, unique injection_no)
    fit_data = avg_factors.dropna().reset_index()
    fit_data.columns = ['injection_no', 'memory_fraction']
    fit_data['injection_no'] = pd.to_numeric(fit_data['injection_no'], errors='coerce').round().astype('Int64')
    fit_data['memory_fraction'] = pd.to_numeric(fit_data['memory_fraction'], errors='coerce')
    fit_data = fit_data.dropna(subset=['injection_no', 'memory_fraction'])
    fit_data = fit_data.drop_duplicates(subset=['injection_no']).sort_values('injection_no')
    fit_data['injection_no'] = fit_data['injection_no'].astype(int)

    if fit_data.empty:
        logging.warning(f"Fit data for memory factors of {isotope_col} is empty.")
        return None, None

    try:
        # Model: two-pool with (x-1) shift to align first inj ~ 1
        def two_pool_exp(x, c0, b, alpha, beta):
            x = np.asarray(x, float)
            x0 = x - 1.0
            return c0 * (b * np.exp(-alpha * x0) + (1.0 - b) * np.exp(-beta * x0))

        X = fit_data['injection_no'].to_numpy(dtype=float)
        Y = fit_data['memory_fraction'].to_numpy(dtype=float)

        # Bounds & initial guess (keep your original spirit, with a bit more safety)
        c0_0 = float(Y[0]) if np.isfinite(Y[0]) else float(np.nanmax(Y))
        if not np.isfinite(c0_0) or c0_0 <= 0:
            c0_0 = max(1e-3, float(np.nanmedian(Y)))
        b0 = 0.5
        alpha0, beta0 = 0.5, 0.1  # your previous defaults

        p0 = [c0_0, b0, alpha0, beta0]
        bounds = ([0.0, 0.0, 0.0, 0.0], [1.2, 1.0, 10.0, 10.0])  # allow c0 up to a bit >1

        popt, _ = curve_fit(two_pool_exp, X, Y, p0=p0, bounds=bounds, maxfev=10000)
        c0, b, alpha, beta = [float(v) for v in popt]

        # Build discrete factors for integer injection range actually present
        x_min_i = int(np.floor(X.min()))
        x_max_i = int(np.ceil(X.max()))
        injection_range = range(x_min_i, x_max_i + 1)
        factors = {inj: float(two_pool_exp(inj, c0, b, alpha, beta)) for inj in injection_range}

        # Smooth curve for plots
        x_fit = np.linspace(x_min_i, x_max_i, 200)
        x0_fit = x_fit - 1.0
        y_fit_fast = c0 * b * np.exp(-alpha * x0_fit)
        y_fit_slow = c0 * (1.0 - b) * np.exp(-beta * x0_fit)
        y_fit_total = y_fit_fast + y_fit_slow

        fit_plot_data = {
            'x_raw': [int(v) for v in X],
            'y_raw': Y,
            'x_fit': x_fit,
            'y_fit_fast': y_fit_fast,
            'y_fit_slow': y_fit_slow,
            'y_fit_total': y_fit_total,
            'model': '2-pool',
            'params': {'c0': c0, 'b': b, 'alpha': alpha, 'beta': beta},
        }

        logging.info(
            f"Fitted 2-pool factors for {isotope_col}: c0={c0:.2f}, b={b:.2f}, alpha={alpha:.2f}, beta={beta:.2f}"
        )
        return factors, fit_plot_data

    except (RuntimeError, ValueError) as e:
        logging.warning(f"Could not fit two-pool memory factors for {isotope_col}: {e}.")
        return None, None
       
def fit_one_pool_memory_factors(data, isotope_col, memory_ids, config):
    """
    Single-pool carryover factors from memory standards.
    Returns:
      factors_map: {injection_no -> fraction}
      fit_plot_data: dict with x_raw, y_raw, x_fit, y_fit_total and placeholders y_fit_fast/y_fit_slow.
    """

    if not memory_ids:
        logging.warning("No memory standards selected for 1-pool carryover characterization.")
        return None, None

    df = data.copy()
    if "Analysis" not in df.columns or "injection_no" not in df.columns:
        logging.warning("Required columns missing for 1-pool memory fit (needs 'Analysis' and 'injection_no').")
        return None, None

    # Use block 1 and selected memory standards (mirror 2R prefiltering)
    if "block_no" in df.columns:
        df = df[df["block_no"] == 1]
    df = df[df["sample_id"].isin(memory_ids)]

    if df.empty:
        logging.warning("No rows for 1-pool memory fit after filtering by block/sample_id.")
        return None, None

    # Stable value per analysis using last config.valids injections
    valids = getattr(config, "valids", 1)
    df["stable_avg"] = df.groupby("Analysis")[isotope_col].transform(lambda s: s.tail(valids).mean())
    stable_vals = df.groupby("Analysis", sort=False)["stable_avg"].first()

    # Build consecutive pairs with different memory standards.
    # sort=False preserves chronological order (run_order sort applied upstream).
    summary = df.groupby("Analysis", sort=False).first().reset_index()
    summary["prev_analysis_id"] = summary["Analysis"].shift(1)
    summary["prev_sample_id"] = summary["sample_id"].shift(1)
    pairs = summary[(summary["sample_id"] != summary["prev_sample_id"]) & summary["prev_analysis_id"].notna()]
    if pairs.empty:
        logging.warning("No valid consecutive different memory pairs found for 1-pool fit.")
        return None, None

    # For each pair, compute per-injection fraction and average across pairs
    all_fracs = []
    for r in pairs.to_dict('records'):
        a_now = r["Analysis"]; a_prev = r["prev_analysis_id"]
        sub = df[df["Analysis"] == a_now]
        try:
            prev_stable = float(stable_vals.loc[a_prev])
            curr_stable = float(stable_vals.loc[a_now])
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); continue
        denom = (prev_stable - curr_stable)
        if denom == 0 or not np.isfinite(denom):
            continue
        frac = (sub[isotope_col] - curr_stable) / denom
        logging.info(f"{isotope_col} memory frac {frac}")
        frac.index = sub["injection_no"]
        all_fracs.append(frac)

    if not all_fracs:
        logging.warning("No usable fractions for 1-pool memory fit.")
        return None, None

    fr_df = pd.concat(all_fracs, axis=1)
    avg = fr_df.mean(axis=1).dropna()
    if avg.empty:
        logging.warning("Average fractions empty for 1-pool memory fit.")
        return None, None

    x = avg.index.to_numpy(dtype=float)
    y = avg.to_numpy(dtype=float)

    # 1-pool model: c0 * exp(-k * (inj - 1))
    def one_pool(x, c0, k):
        x = np.asarray(x, float)
        return c0 * np.exp(-k * (x - 1.0))

    # initial guesses & bounds
    c0_0 = float(np.clip(y[0], 0.0, 1.0))
    k_0 = 0.5
    bounds = ([0.0, 0.0], [1.0, 10.0])

    try:
        popt, _ = curve_fit(one_pool, x, y, p0=[c0_0, k_0], bounds=bounds, maxfev=4000)
        c0, k = float(popt[0]), float(popt[1])
    except Exception as e:
        logging.warning(f"1-pool fit failed: {e}")
        return None, None

    xi = np.linspace(float(x.min()), float(x.max()), 80)
    yi = one_pool(xi, c0, k)

    inj_min, inj_max = int(np.floor(x.min())), int(np.ceil(x.max()))
    factors = {int(inj): float(one_pool(inj, c0, k)) for inj in range(inj_min, inj_max + 1)}

    fit_plot_data = {
        "x_raw": x,
        "y_raw": y,
        "x_fit": xi,
        "y_fit_total": yi,
        "y_fit_fast": yi,                  # placeholder for 2R-style plotter
        "y_fit_slow": np.zeros_like(yi),   # placeholder for 2R-style plotter
        "model": "1-pool",
        "params": {"c0": c0, "k": k},
    }
    return factors, fit_plot_data


def apply_carryover_correction(data, isotope_col, memory_factors, config):
    df = data.copy(); output_col = f'{isotope_col}_memory_corrected'
    df[output_col] = df[isotope_col]
    df['stable_avg'] = df.groupby('Analysis')[isotope_col].transform(lambda x: x.tail(config.valids).mean())
    # prev_stable_avg: shift by 1 row; NaN at first row of run → no correction needed there
    df['prev_stable_avg'] = df['stable_avg'].shift(1).fillna(df['stable_avg'])
    max_inj_per_analysis = df.groupby('Analysis')['injection_no'].transform('max')
    to_correct = df['injection_no'] < max_inj_per_analysis
    mem_fracs = df['injection_no'].map(memory_factors)
    correction_term = mem_fracs * (df['stable_avg'] - df['prev_stable_avg'])
    valid = to_correct & correction_term.notna()
    df.loc[valid, output_col] = df.loc[valid, isotope_col] + correction_term.loc[valid]
    return df.drop(columns=['stable_avg', 'prev_stable_avg'])

def fit_drift_parameters(data, drift_sample_id, isotope_col):
    """
    Linear drift fit on the chosen drift standard:
      y = a * seconds + b
    Returns a dict containing BOTH legacy keys ('popt','pcov') and flattened keys:
      'slope','intercept','slope_unc','intercept_unc','r2','n','x','y','pred','data'
    """
    # import numpy as np
    # import pandas as pd
    # from scipy.optimize import curve_fit

    if not drift_sample_id:
        return None

    if not isinstance(data, pd.DataFrame) or data.empty:
        return None

    # ensure we have timestamp & isotope
    if "timestamp" not in data.columns or isotope_col not in data.columns or "sample_id" not in data.columns:
        return None

    # restrict to the drift owner’s rows
    drift_data = data.copy()
    # guard: timestamp must be datetime64
    if not np.issubdtype(drift_data["timestamp"].dtype, np.datetime64):
        try:
            drift_data["timestamp"] = pd.to_datetime(drift_data["timestamp"], errors="coerce")
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); return None

    drift_data = drift_data.dropna(subset=["timestamp", isotope_col])
    if drift_data.empty:
        return None

    # select only the chosen drift standard
    drift_data = drift_data[drift_data["sample_id"].astype(str).str.strip() == str(drift_sample_id).strip()].copy()
    if len(drift_data) < 2:
        return None

    # x = elapsed seconds
    t0 = drift_data["timestamp"].min()
    drift_data["seconds"] = (drift_data["timestamp"] - t0).dt.total_seconds()

    x = drift_data["seconds"].to_numpy(dtype=float)
    y = pd.to_numeric(drift_data[isotope_col], errors="coerce").to_numpy(dtype=float)
    msk = np.isfinite(x) & np.isfinite(y)
    x = x[msk]; y = y[msk]
    if x.size < 2:
        return None

    # linear model
    def lin(x, a, b):  # y = a x + b
        return a * x + b

    try:
        popt, pcov = curve_fit(lin, x, y)
    except Exception as e:

        logging.warning(f"Exception caught: {e}"); return None

    a, b = float(popt[0]), float(popt[1])

    # uncertainties (1σ) from covariance
    se_a = float(np.sqrt(pcov[0, 0])) if pcov.shape == (2, 2) and pcov[0, 0] >= 0 else None
    se_b = float(np.sqrt(pcov[1, 1])) if pcov.shape == (2, 2) and pcov[1, 1] >= 0 else None

    # R^2
    yhat = lin(x, a, b)
    ss_res = float(np.sum((y - yhat) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = float(1.0 - ss_res / ss_tot) if ss_tot > 0 else float("nan")

    # pack both legacy and flat keys
    out = {
        # legacy payload
        "popt": np.array([a, b], dtype=float),
        "pcov": pcov,
        "data": drift_data.loc[msk, ["timestamp", "seconds", isotope_col]].copy(),

        # flat fields expected by DB write & quick plotting
        "slope": a,
        "intercept": b,
        "slope_unc": se_a,
        "intercept_unc": se_b,
        "r2": r2,
        "n": int(x.size),
        "x": x,
        "y": y,
        "pred": yhat,
    }
    return out

def apply_drift_correction(data, input_col, output_col, fit_params):
    if fit_params is None: data[output_col] = data[input_col]; return data
    popt, pcov = fit_params['popt'], fit_params['pcov']
    data['seconds'] = (data['timestamp'] - data['timestamp'].min()).dt.total_seconds()
    center_time = data['seconds'].median()
    drift_at_time = popt[0] * data['seconds'] + popt[1]
    drift_at_center = popt[0] * center_time + popt[1]
    data[output_col] = data[input_col] - (drift_at_time - drift_at_center)
    return data

def _ensure_memfit_container(mem_fits: dict, iso: str) -> dict:
    """
    Ensure memory_fits[iso] exists and is a dict keyed by analysis_id.
    Returns the per-iso dict so caller can assign e.g. dict_0 = {...}.
    """
    if mem_fits is None:
        raise ValueError("mem_fits must be a dict, got None")
    if iso not in mem_fits or not isinstance(mem_fits[iso], dict):
        mem_fits[iso] = {}
    return mem_fits[iso]

def process_isotope_data(data, standards_data, config, corrections_to_apply, roles, methods, isotopes_to_process):
    """
    Laser pipeline (LGR/Picarro):
      - ignore & outliers
      - memory correction (injection-level)
      - aggregate to analysis-level (means, counts, etc.)
      - linearity & drift corrections (analysis-level)
      - PRIOR uncertainty for each isotope:
          u_prior = sqrt( SEM^2 + stage_sd^2 ), where
              SEM from replicate (post-correction) values per analysis
              stage_sd from residual SDs of memory/drift fits if available
      - NO calibration here (generic calibration will be applied after Worker)
        -> {iso}_calibrated is temporarily set to drift_corrected so UI remains populated.
    Returns: injection_level_data, analysis_level_data, validation_results(empty here), memory_fits, drift_fits
    """

    def _sem_by_analysis(inj_df: pd.DataFrame, value_col: str) -> pd.Series:
        """Compute SEM per Analysis from injection-level values in `value_col`."""
        if value_col not in inj_df.columns or 'Analysis' not in inj_df.columns:
            return pd.Series(dtype=float)
        grp = inj_df.groupby('Analysis', dropna=False)[value_col]
        # ddof=1 for sd; SEM = sd / sqrt(n). Handle n<2 gracefully.
        n = grp.count()
        sd = grp.std(ddof=1)
        sem = sd / np.sqrt(n.replace(0, np.nan))
        return sem

    def _stage_sd_from_fits(mem_fits: dict, dr_fits: dict, iso: str) -> float:
        """Quadrature of residual SDs from memory and drift fits if available."""
        s2 = 0.0
        try:
            if isinstance(mem_fits, dict) and isinstance(mem_fits.get(iso), dict):
                s2 += float(mem_fits[iso].get("sd_resid", 0.0))**2
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        try:
            if isinstance(dr_fits, dict) and isinstance(dr_fits.get(iso), dict):
                s2 += float(dr_fits[iso].get("sd_resid", 0.0))**2
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        return float(np.sqrt(s2))

    # ---- Begin original flow ----
    result = data.copy()
    linearity_id = roles.get('linearity')
    drift_id     = roles.get('drift')
    calibration_ids = roles.get('calibration', [])
    memory_ids      = roles.get('memory', [])

    isotopes_present = [iso for iso in isotopes_to_process if iso in result.columns]
    if not isotopes_present:
        raise ValueError("None of the selected isotopes were found in the data file.")

    # Respect 'ignore' flag
    if 'ignore' in result.columns:
        result.loc[result['ignore'] != 0, isotopes_present] = np.nan

    # Outlier handling
    if 'outlier' in corrections_to_apply:
        result = detect_outliers(result, linearity_id, config.outlier_sd, config.outlier_method)
    else:
        result['is_outlier'] = False
    result.loc[result['is_outlier'], isotopes_present] = np.nan

    memory_fits = {}
    first_analysis_id = result['Analysis'].min()
    memory_factors: dict[str, dict[int, float]] = {}
    
    # --- Memory correction (injection-level) ---
    for isotope in isotopes_present:
        input_col  = isotope
        output_col = f'{isotope}_memory_corrected'
        method = methods.get("memory")
        if method == "Fast/Slow Pool Carryover":
            factors, fit_plot_data = fit_two_pool_memory_factors(result, input_col, memory_ids, config)
            if factors:
                memory_factors[isotope] = {int(k): float(v) for k, v in factors.items()}
                result = apply_carryover_correction(result, input_col, factors, config)
                # Store fit visuals; sd_resid if available inside factors or fit_plot_data
                _ensure_memfit_container(memory_fits, isotope)
                if fit_plot_data is not None:
                    memory_fits[isotope][0] = {**fit_plot_data, "model": "two-pool"}
                # Try to add sd_resid if your fitter returns it
                try:
                    if isinstance(factors, dict) and 'sd_resid' in factors:
                        memory_fits[isotope]['sd_resid'] = float(factors['sd_resid'])
                except Exception as e:
                    logging.warning(f"Exception caught: {e}")
            else:
                result[output_col] = result[input_col]
        
        elif method in ["Single Pool Carryover", "1-Reservoir Carryover", "1-Reservoir (Single Pool)", "Single Pool", "1-Reservoir", "1R"]:
            factors, fit_plot_data = fit_one_pool_memory_factors(result, input_col, memory_ids, config)
            if factors:
                memory_factors[isotope] = {int(k): float(v) for k, v in factors.items()}
                result = apply_carryover_correction(result, input_col, factors, config)
                _ensure_memfit_container(memory_fits, isotope)
                if fit_plot_data is not None:
                    memory_fits[isotope][0] = {**fit_plot_data, "model": "one-pool"}
            else:
                result[output_col] = result[input_col]
        elif method in ["Exponential", "Asymptotic"]:
            result, fits = apply_intra_sample_memory_correction(result, input_col, config, method)
            # If your 'fits' include sd_resid per iso, retain it:
            if isinstance(fits, dict):
                try:
                    if isotope in fits:
                        # ensure we carry sd_resid if present
                        mf = dict(fits[isotope]) if isinstance(fits[isotope], dict) else {}
                        memory_fits[isotope] = mf
                except Exception as e:

                    logging.warning(f"Exception caught: {e}")
        else:
            # No memory correction
            result[output_col] = result[input_col]

        # First Analysis: keep original raw (your existing rule)
        mask_first = (result['Analysis'] == first_analysis_id)
        result.loc[mask_first, output_col] = result.loc[mask_first, input_col]

    # Snapshot injection-level (with memory corrections)
    injection_level_data = result.copy()

    # --- Aggregate to analysis-level ---
    metadata_cols = [c for c in ['Analysis', 'sample_id', 'role', 'timestamp'] if c in result.columns]
    analysis_metadata = result.groupby('Analysis', dropna=False).first().reset_index()[metadata_cols]

    agg_dict = {col: 'mean' for col in result.select_dtypes(include=np.number).columns if col != 'Analysis'}
    agg_dict['is_outlier']   = 'sum'
    agg_dict['injection_no'] = 'count'
    if 'ignore' in result.columns:
        agg_dict['ignore'] = 'sum'

    processed_analyses = result.groupby('Analysis', dropna=False).agg(agg_dict).reset_index()
    processed_analyses.rename(columns={'is_outlier': 'outlier_count',
                                       'injection_no': 'injection_count',
                                       'ignore': 'ignored_count'}, inplace=True)
    analysis_level_data = pd.merge(processed_analyses, analysis_metadata, on='Analysis', suffixes=('', '_meta'))

    # --- Preserve sample_name from injection-level into analysis-level ---
    try:
        inj = injection_level_data  # your variable that holds per-injection data
        ana = analysis_level_data
        if isinstance(inj, pd.DataFrame) and isinstance(ana, pd.DataFrame):
            # pick the analysis key used across your code
            key = None
            for k in ("Analysis", "InstrumentAnalysisID", "AnalysisID"):
                if k in inj.columns and k in ana.columns:
                    key = k
                    break

            # normalize column name if LIMS used sName
            if "sName" in inj.columns and "sample_name" not in inj.columns:
                inj = inj.rename(columns={"sName": "sample_name"})

            if key and "sample_name" in inj.columns:
                name_map = (inj[[key, "sample_name"]]
                            .dropna(subset=key)
                            .astype({key: str})
                            .drop_duplicates(subset=key))
                anakey = anakey.astype(str)
                analysis_level_data = ana.merge(name_map, on=key, how="left")
    except Exception:
        # non-fatal if mapping fails
        pass
    # --- END preserve sample_name ---

    # --- Linearity & Drift (analysis-level) ---
    drift_fits = {}
    for isotope in isotopes_present:
        mem_out   = f'{isotope}_memory_corrected'
        lin_out   = f'{isotope}_linearity_corrected'
        drift_out = f'{isotope}_drift_corrected'
        cal_out   = f'{isotope}_calibrated'  # placeholder for UI (real calibration is applied later)

        # Linearity
        if methods.get("linearity") != "None":
            if linearity_id and linearity_id in analysis_level_data['sample_id'].values:
                popt, pcov = fit_linearity_parameters(analysis_level_data, linearity_id, mem_out)
                analysis_level_data = apply_linearity_correction(analysis_level_data, mem_out, lin_out, popt, pcov)
            else:
                logging.warning(
                    f"Linearity correction requested for {isotope} but sample_id "
                    f"{linearity_id!r} not found in data — skipping linearity correction."
                )
                analysis_level_data[lin_out] = analysis_level_data[mem_out]
        else:
            analysis_level_data[lin_out] = analysis_level_data[mem_out]

        # Drift
        fit_params = None
        if methods.get("drift") != "None":
            fit_params = fit_drift_parameters(analysis_level_data, drift_id, lin_out)
            analysis_level_data = apply_drift_correction(analysis_level_data, lin_out, drift_out, fit_params)
            if fit_params:
                # Keep drift standard data for plotting, and sd_resid if your fitter provides it
                ff = dict(fit_params)
                try:
                    ff['data'] = analysis_level_data[analysis_level_data['sample_id'] == drift_id].copy()
                except Exception as e:

                    logging.warning(f"Exception caught: {e}")
                drift_fits[isotope] = ff
        else:
            analysis_level_data[drift_out] = analysis_level_data[lin_out]

        # Placeholder calibrated = drift_out (real calibration will overwrite later)
        analysis_level_data[cal_out] = analysis_level_data[drift_out]

    # ---- PRIOR uncertainty (per analysis & isotope) --------------------
    # Compute SEM from injection-level values of the column we are going to calibrate from.
    for isotope in isotopes_present:
        # pick best available post-correction column (in order)
        best_meas = None
        for c in (f'{isotope}_drift_corrected', f'{isotope}_linearity_corrected', f'{isotope}_memory_corrected', isotope):
            if c in injection_level_data.columns:
                best_meas = c
                break
        if best_meas is None:
            continue

        sem_by_A = _sem_by_analysis(injection_level_data, best_meas)  # Series indexed by Analysis
        # Map SEM into analysis_level_data
        analysis_level_data[f"{isotope}_sem"] = analysis_level_data['Analysis'].map(sem_by_A)

        # Stage SD from fits (if available)
        stage_sd = _stage_sd_from_fits(memory_fits, drift_fits, isotope)

        # u_prior = sqrt( SEM^2 + stage_sd^2 )
        sem2 = pd.to_numeric(analysis_level_data.get(f"{isotope}_sem"), errors='coerce')**2
        analysis_level_data[f"{isotope}_u"] = np.sqrt(sem2.fillna(0.0) + stage_sd**2)

    # ---- Derived parameters (pre-calibration placeholder) --------------
    # (These will be overwritten/updated after generic calibration runs in GUI)
    if ('d_excess' not in analysis_level_data.columns and
        'dD_calibrated' in analysis_level_data.columns and
        'd18O_calibrated' in analysis_level_data.columns):
        analysis_level_data['d_excess'] = analysis_level_data['dD_calibrated'] - 8 * analysis_level_data['d18O_calibrated']

    if ('17O_Excess' not in analysis_level_data.columns and
        'd17O_calibrated' in analysis_level_data.columns and
        'd18O_calibrated' in analysis_level_data.columns):
        d17O = pd.to_numeric(analysis_level_data['d17O_calibrated'], errors='coerce')
        d18O = pd.to_numeric(analysis_level_data['d18O_calibrated'], errors='coerce')
        analysis_level_data['17O_Excess'] = (np.log(d17O / 1000 + 1) - 0.528 * np.log(d18O / 1000 + 1)) * 1e6

    # ---- Convenience columns for plotting/status -----------------------
    if 'timestamp' in analysis_level_data.columns:
        analysis_level_data.sort_values('timestamp', inplace=True)
    analysis_level_data['cumulative_injection'] = range(1, len(analysis_level_data) + 1)
    analysis_level_data['mwl_applied'] = methods.get('mwl', 'None')

    # ---- Formatting (keep your existing helper) ------------------------
    injection_level_data = format_results(injection_level_data)
    analysis_level_data  = format_results(analysis_level_data)

    # No validation here; GUI’s post step handles it after calibration
    return injection_level_data, analysis_level_data, {}, memory_fits, drift_fits, memory_factors


def find_largest_diff_consecutive_pairs(df, column_name):
    """
    Identifies the two consecutive pairs with the largest absolute difference in a DataFrame column.
    
    Parameters:
    df (pd.DataFrame): Input DataFrame
    column_name (str): Name of the column to analyze
    
    Returns:
    list: List of tuples [(index1, index2, value1, value2, diff), ...] for the pair with largest difference
          or empty list if no consecutive pairs exist
    """
    if column_name not in df.columns:
        raise ValueError(f"Column {column_name} not found in DataFrame")
    # Drop rows where the specified column has NaN values
    df_clean = df.dropna(subset=column_name).reset_index(drop=True)
    # Calculate absolute differences between consecutive elements
    differences = []
    for i in range(len(df_clean) - 1):
        if len(df_clean) < 2:
            return np.array([])        
        diff = abs(df_clean[column_name].iloc[i] - df_clean[column_name].iloc[i + 1])
        differences.append((df_clean['sample_id'].iloci, df_clean['sample_id'].iloc[i + 1], diff))
    # Convert to numpy array and sort by difference (last column) in descending order
    differences = np.array(differences)
    if differences.size == 0:
        return np.array([])
    
    # Sort by difference (index 1) and take the first row
    sorted_indices = np.argsort(differences[:, 2])[::-1]
    return differences[sorted_indices[:1]]

def assign_roles(data_df, standards_df, use_hierarchy=True):
    data_df['sample_id'] = data_df['sample_id'].astype(str).str.strip()
    standards_df['sample_id'] = standards_df['sample_id'].astype(str).str.strip()
    standard_ids = set(standards_df['sample_id'])
    data_df['role'] = np.where(data_df['sample_id'].isin(standard_ids), 'Standard', 'Sample')
    if use_hierarchy:
        role_map = {'Drift': ['is_drift_id', 'is_drift'], 'Linearity': ['is_linearity_id', 'is_linearity'],
                    'Validation': ['is_control_id', 'is_control'], 'Calibration': ['is_calibration_id', 'is_calibrationid'],
                    'Memory': ['is_memory_id', 'is_memory']}
        for role_name, aliases in role_map.items():
            for role_col in aliases:
                if role_col in standards_df.columns:
                    role_ids = set(standards_df.loc[pd.to_numeric(standards_df[role_col], errors='coerce').isin([1, -1]), 'sample_id'])
                    if role_ids:
                        matches = data_df['sample_id'].isin(role_ids)
                        data_df.loc[matches, 'role'] = role_name
                        break
    return data_df

def assign_block_numbers(data_df):
    """ --- FINAL, DEFINITIVE IMPLEMENTATION of block logic --- """
    df = data_df.copy()
    df['is_standard_block'] = df['role'].ne('Sample')
    df['block_start'] = df['is_standard_block'] & ~df['is_standard_block'].shift(1, fill_value=False)
    standard_block_numbers = df['block_start'].cumsum()
    df['block_no'] = standard_block_numbers.where(df['is_standard_block']).ffill().fillna(1).astype(int)
    logging.info(f"Assigned block numbers. Found {df['block_no'].max()} blocks.")
    return df.drop(columns=['is_standard_block', 'block_start'])

def prepare_raw_data_for_plotting(raw_data, standards_data, instrument):
    standards_df = standards_data.copy(); standards_df.columns = [col.lower().strip() for col in standards_df.columns]
    if 'sample_id' not in standards_df.columns: raise KeyError("Could not find 'sample_id' column in standards file.")
    # mapped_data = map_and_clean_raw_data(raw_data, instrument)
    prepared_data = assign_roles(raw_data, standards_df, use_hierarchy=False)
    prepared_data = assign_block_numbers(prepared_data) # Assign blocks after roles
    return prepared_data

def load_and_prepare_data(data_file, standards_data, instrument):
    raw_data = pd.read_csv(data_file); standards_df = standards_data.copy()
    standards_df.columns = [col.lower().strip() for col in standards_df.columns]
    if 'sample_id' not in standards_df.columns: raise KeyError("Could not find 'sample_id' column in standards file.")
    mapped_data = map_and_clean_raw_data(raw_data, InstrumentType(instrument))
    prepared_data = assign_roles(mapped_data, standards_df, use_hierarchy=True)
    prepared_data = assign_block_numbers(prepared_data) # Assign blocks after roles
    return prepared_data, standards_df

def detect_outliers(data, linearity_id, threshold=2.0, method="X_STDV (SD)"):
    if 'water_conc' not in data.columns or data['water_conc'].isna().all():
        logging.info(f"Outlier not done on {linearity_id}")
        data['is_outlier'] = False
        return data
    from outliers import normalize_outlier_method
    method = normalize_outlier_method(method)
    non_lin = data[data['sample_id'] != linearity_id]['water_conc'].dropna()
    if non_lin.empty:
        data['is_outlier'] = False
        return data
    if method == 'Modified Z-Score (MAD)':
        med = non_lin.median()
        mad = (non_lin - med).abs().median()
        if mad > 0:
            data['is_outlier'] = 0.6745 * (data['water_conc'] - med).abs() / mad > threshold
        else:
            data['is_outlier'] = False
    elif method == 'Huber':
        k, mean, std = threshold, non_lin.mean(), non_lin.std()
        if std > 0:
            for _ in range(20):
                r = (non_lin - mean) / std
                w = np.where(np.abs(r) <= k, 1.0, k / np.abs(r))
                new_mean = np.average(non_lin, weights=w)
                new_std  = np.sqrt(np.average((non_lin - new_mean) ** 2, weights=w))
                if np.abs(new_mean - mean) < 1e-8 * std:
                    break
                mean, std = new_mean, max(new_std, 1e-12)
            data['is_outlier'] = (data['water_conc'] - mean).abs() / max(std, 1e-12) > k
        else:
            data['is_outlier'] = False
    else:
        # X_STDV (SD) — default
        mean_val = non_lin.mean()
        std_val  = non_lin.std()
        data['is_outlier'] = (np.abs(data['water_conc'] - mean_val) > threshold * std_val)
    data.loc[data['sample_id'] == linearity_id, 'is_outlier'] = False
    return data

def identify_linearity_sample(data, h2o_variation_threshold):
    # Normalize column names for robust access
    normalized_columns = [col.strip().lower() for col in data.columns]  
    # Find actual column names
    amount_col = next((col for col in data.columns if col.strip().lower() == 'amount'), None)
    analysis_col = next((col for col in data.columns if col.strip().lower() == 'instrumentanalysisid'), None)
    sample_id_col = next((col for col in data.columns if col.strip().lower() == 'sample_id'), None)
    if analysis_col is None:
        analysis_col = next((col for col in data.columns if col.strip().lower() == 'analysis'), None)
    # Check required columns
    if amount_col is None:
        print("Warning: 'Amount' column not found for linearity sample identification")
        return None
    if analysis_col is None or sample_id_col is None:
        print("Warning: Required columns ('Analysis', 'sample_id') not found for linearity sample")
        return None
    # Group data
    if analysis_col in data.columns and not data[analysis_col].isna().all():
        grouped_data = data.groupby(analysis_col)
    else:
        grouped_data = data.groupby(sample_id_col)
    try:
        # Calculate variance of Amount within each group
        h2o_variance = grouped_data[amount_col].var().reset_index()
        h2o_variance = h2o_variance.dropna()
        if h2o_variance.empty:
            print("Warning: No valid Amount variance calculated")
            return None
        # Filter analyses with variance above threshold
        high_variance = h2o_variance[h2o_variance[amount_col] > h2o_variation_threshold]
        if high_variance.empty:
            print("Warning: No analysis found with Amount variance above threshold")
            return None
        # Use the first matching analysis
        max_variance_analysis = high_variance.iloc[0][analysis_col]
        # Get sample IDs for that analysis
        sample_ids = data[data[analysis_col] == max_variance_analysis][sample_id_col].unique()
        if len(sample_ids) == 0:
            print(f"Warning: No sample_id found for Analysis {max_variance_analysis}")
            return None
        elif len(sample_ids) > 1:
            print(f"Warning: Multiple sample_ids for Analysis {max_variance_analysis}: {sample_ids}")

        return sample_ids.tolist()

    except Exception as e:
        print(f"Error identifying linearity sample: {str(e)}")
        return None
    
def fit_linearity_parameters(data, linearity_sample_id, isotope_col):
    if not linearity_sample_id: 
        return None, None
    linearity_data = data[data['sample_id'] == linearity_sample_id].copy().dropna(subset=['water_conc', isotope_col])

    # Require N > p (2 parameters)
    if len(linearity_data) < 3: 
        logging.warning(f"Linearity fit skipped: Not enough data points (n={len(linearity_data)}).")
        return None, None

    # Check for degenerate data (all X values are the same)
    if linearity_data['water_conc'].nunique() < 2:
        logging.warning(f"Linearity fit skipped: Not enough unique 'water_conc' values.")
        return None, None

    try: 
        return curve_fit(lambda x, a, b: a * x + b, linearity_data['water_conc'], linearity_data[isotope_col])
    except RuntimeError: 
        logging.warning(f"Linearity fit failed for {isotope_col}"); 
        return None, None    

def apply_linearity_correction(data, input_col, output_col, popt, pcov):
    if popt is None: data[output_col] = data[input_col]; return data
    ref_conc = data['water_conc'].median()
    predicted = popt[0] * data['water_conc'] + popt[1]
    ref_value = popt[0] * ref_conc + popt[1]
    data[output_col] = data[input_col] - (predicted - ref_value)
    return data

def format_results(df):
    for col in df.columns:
        if ('_calibrated' in col or '_corrected' in col) and 'uncertainty' not in col:
            df[col] = df[col].round(2)
        elif '_uncertainty' in col:
            df[col] = df[col].round(4)
    return df

def _collect_validation_ids(standards_df: pd.DataFrame | None,
                             roles_from_gui: dict | None,
                             id_col: str = "sample_id") -> set[str]:
    ids: set[str] = set()

    # From GUI roles (your get_roles_from_table output)
    if isinstance(roles_from_gui, dict):
        for key in ("validation", "validations", "control", "controls",
                    "is_validation_id", "is_control_id"):
            v = roles_from_gui.get(key)
            if v is None:
                continue
            if isinstance(v, (list, tuple, set)):
                ids.update(str(x).split("/")[0].strip() for x in v)
            else:
                ids.add(str(v).split("/")[0].strip())

    # From standards_df (flexible or wide)
    if isinstance(standards_df, pd.DataFrame) and not standards_df.empty:
        cols_lower = {str(c).lower(): c for c in standards_df.columns}

        # Long: parse 'roles' text
        roles_col = cols_lower.get("roles")
        if roles_col:
            m = standards_df[standards_df[roles_col].astype(str)
                             .str.contains(r"\b(validation|control)\b", case=False, na=False)]
            if id_col in m.columns:
                ids.update(str(x).split("/")[0].strip() for x in m[id_col].astype(str))

        # Wide: any is_* checkboxes
        for box in ("is_validation_id", "is_control_id"):
            c = cols_lower.get(box)
            if c:
                m = standards_df[pd.to_numeric(standards_df[c], errors="coerce") == 1]
                if id_col in m.columns:
                    ids.update(str(x).split("/")[0].strip() for x in m[id_col].astype(str))

    return ids

def compute_validation_results(
    analysis_df: pd.DataFrame,
    standards_df: pd.DataFrame | None,
    *,
    roles_from_gui: dict | None = None,
    id_col: str = "sample_id",
    isotopes: tuple[str, ...] = ("d18O", "dD", "d17O"),
    z_threshold: float = 2.0
) -> tuple[pd.DataFrame, dict]:
    """
    Compare calibrated results for control samples to their certified values and compute z-scores.

    z = (calibrated - true) / sqrt( u_cal^2 + u_ref^2 )

    Returns:
      (validation_rows_df, summary_dict)
        validation_rows_df: one row per control sample with columns for each isotope:
            {iso}_calibrated, {iso}_true, {iso}_u_cal, {iso}_u_ref, {iso}_resid, {iso}_z, {iso}_z_fail
        summary_dict: {iso: {"n":..., "max_abs_z":..., "n_fail":...}}
    """

    if analysis_df is None or analysis_df.empty:
        return pd.DataFrame(), {}

    # Determine which samples are controls
    control_ids = _collect_validation_ids(standards_df, roles_from_gui, id_col=id_col)

    if not control_ids:
        return pd.DataFrame(), {}

    # Decide which isotopes to evaluate (present & calibrated)
    iso_list = tuple(i for i in isotopes if f"{i}_calibrated" in analysis_df.columns)
    if not iso_list:
        return pd.DataFrame(), {}

    # Build true + uncertainty table (wide) from standards_df
    true_map = {}
    if isinstance(standards_df, pd.DataFrame) and not standards_df.empty:
        # Try wide: {iso}_true / {iso}_uncertainty
        wide_cols = set(map(str, standards_df.columns))
        has_wide = any(f"{i}_true" in wide_cols for i in iso_list)
        # if has_wide:
            # t = standards_df[[c for c in id_col + [f"{i}_true" for i in iso_list if f"{i}_true" in wide_cols] +
            #                   [f"{i}_uncertainty" for i in iso_list if f"{i}_uncertainty" in wide_cols]
            #                  if c in standards_df.columns]].copy()
            # tid_col = tid_col.astype(str).str.split("/").str[0].str.strip()
            # true_map = t.set_index(id_col).to_dict("index")
        if has_wide:
            # Wide case: columns like d18O_true, dD_true (and optional *_uncertainty)
            wide_cols = list(standards_df.columns)
            cols = (
                [id_col]
                + [f"{i}_true" for i in iso_list if f"{i}_true" in wide_cols]
                + [f"{i}_uncertainty" for i in iso_list if f"{i}_uncertainty" in wide_cols]
            )
            t = standards_df.loc[:, [c for c in cols if c in standards_df.columns]].copy()
            if t.empty:
                true_map = {}
            else:
                # normalize IDs (handle "A/B" -> "A")
                t[id_col] = (
                    t[id_col]
                    .astype(str)
                    .str.split("/")
                    .str[0]
                    .str.strip()
                )
                # drop duplicate sample_id rows (keep first, stable)
                before = len(t)
                t = t.loc[~t[id_col].isna()]
                t = t.drop_duplicates(subset=id_col, keep="first")
                if len(t) < before:
                    logging.warning(
                        f"Validation: dropped {before - len(t)} duplicate standard rows in wide table for unique {id_col}."
                    )
                true_map = t.set_index(id_col).to_dict("index")

        else:
            # Long case: columns isotope, true (optional uncertainty)
            need_cols = {"isotope", "true"}
            if need_cols.issubset(set(standards_df.columns)):
                long = standards_df[
                    [id_col, "isotope", "true"]
                    + (["uncertainty"] if "uncertainty" in standards_df.columns else [])
                ].copy()
                if long.empty:
                    true_map = {}
                else:
                    # normalize IDs and isotope, keep first (stable) per (id, isotope)
                    longid_col = (
                        longid_col
                        .astype(str)
                        .str.split("/")
                        .str[0]
                        .str.strip()
                    )
                    long["isotope"] = long["isotope"].astype(str).str.strip()
                    long = long.dropna(subset=["true"])
                    before = len(long)
                    long = long.drop_duplicates(subset=[id_col, "isotope"], keep="first")
                    if len(long) < before:
                        logging.warning(
                            f"Validation: dropped {before - len(long)} duplicate rows in long standards for unique (id,isotope)."
                        )

                    # pivot in-memory (does NOT modify standards_df) to get one row per id
                    pivot_true = long.pivot_table(
                        index=id_col, columns="isotope", values="true", aggfunc="first"
                    )
                    if "uncertainty" in long.columns:
                        pivot_unc = long.pivot_table(
                            index=id_col, columns="isotope", values="uncertainty", aggfunc="first"
                        )
                    else:
                        pivot_unc = None

                    tv = pivot_true.add_suffix("_true")
                    if pivot_unc is not None:
                        uv = pivot_unc.add_suffix("_uncertainty")
                        tdf = pd.concat([tv, uv], axis=1)
                    else:
                        tdf = tv

                    # index is unique after pivot; safe to_dict('index')
                    true_map = tdf.to_dict("index")
            else:
                true_map = {}

    # Extract only control rows from analysis_df
    val = analysis_df[analysis_df[id_col].astype(str).str.split("/").str[0].str.strip().isin(control_ids)].copy()
    if val.empty:
        return pd.DataFrame(), {}

    # For each isotope compute residuals and z
    summary = {}
    for iso in iso_list:
        cal_col = f"{iso}_calibrated"
        ucal_col = f"{iso}_u" if f"{iso}_u" in analysis_df.columns else None
        tcol = f"{iso}_true"
        uref_col = f"{iso}_uncertainty"

        # Attach true + u_ref
        val[f"{tcol}"] = val[id_col].astype(str).str.split("/").str[0].str.strip().map(
            lambda sid: (true_map.get(sid, {}) or {}).get(tcol, np.nan)
        )
        val[f"{uref_col}"] = val[id_col].astype(str).str.split("/").str[0].str.strip().map(
            lambda sid: (true_map.get(sid, {}) or {}).get(uref_col, np.nan)
        )

        # Residual
        val[f"{iso}_resid"] = pd.to_numeric(val[cal_col], errors="coerce") - pd.to_numeric(val[tcol], errors="coerce")

        # Uncertainty components
        u_cal = pd.to_numeric(val[ucal_col], errors="coerce") if ucal_col else np.nan
        u_ref = pd.to_numeric(val[f"{uref_col}"], errors="coerce")
        u_tot = np.sqrt(np.square(u_cal) + np.square(u_ref))
        val[f"{iso}_z"] = val[f"{iso}_resid"] / u_tot

        # Fail flag
        val[f"{iso}_z_fail"] = (val[f"{iso}_z"].abs() > z_threshold).astype(int)

        # Summary
        series = val[f"{iso}_z"].dropna()
        summary[iso] = {
            "n": int(series.size),
            "max_abs_z": float(series.abs().max()) if series.size else float("nan"),
            "n_fail": int(val[f"{iso}_z_fail"].sum(skipna=True)),
        }

    return val, summary

def _pack_memory_fit(model: str, iso: str, params: dict, x, y, yhat, memory_ids):
    import pandas as pd
    # normalize to Series to simplify plotting later
    xs = pd.Series(x) if not hasattr(x, "index") else x
    ys = pd.Series(y) if not hasattr(y, "index") else y
    yh = pd.Series(yhat) if not hasattr(yhat, "index") else yhat
    return {
        "model": str(model).lower(),
        "isotope": iso,
        "params": dict(params or {}),
        "x": xs,
        "y": ys,
        "pred": yh,
        "memory_ids": list(memory_ids or []),
    }

# ============================================================================
# Recovery helpers for multi-worksheet EA/DI (append-only)
# ============================================================================
import pandas as _pd
import tempfile as _tempfile
import os as _os

def _infer_iso_from_sheet_name(sheet_name: str):
    s = str(sheet_name).strip().lower().replace(" ", "")
    # Basic matches
    if s.startswith("d13c") or s in ("c13","13c","carbon13"): return "d13C"
    if s.startswith("d15n") or s in ("n15","15n","nitrogen15"): return "d15N"
    if s.startswith("d18o") or s in ("o18","18o","oxygen18"): return "d18O"
    if s in ("dd","d2h","2h","h2","hydrogen2"): return "dD"
    if s.startswith("d34s") or s in ("s34","34s","sulfur34"): return "d34S"
    if s.startswith("d17o") or s in ("o17","17o","oxygen17"): return "d17O"
    # Greek delta variants
    if s.startswith("δ13c") or s.startswith("delta13c") or s.startswith("del13c"): return "d13C"
    if s.startswith("δ15n") or s.startswith("delta15n") or s.startswith("del15n"): return "d15N"
    if s.startswith("δ18o") or s.startswith("delta18o") or s.startswith("del18o"): return "d18O"
    if s.startswith("δd")   or s.startswith("δ2h") or s.startswith("delta2h") or s.startswith("deltad"): return "dD"
    if s.startswith("δ34s") or s.startswith("delta34s") or s.startswith("del34s"): return "d34S"
    if s.startswith("δ17o") or s.startswith("delta17o") or s.startswith("del17o"): return "d17O"
    return None

def _xlsx_sheets_to_temp_csvs(xlsx_path: str, wanted_isotopes=None):
    """Convert each worksheet to a temp CSV and return a list of (iso, csv_path).
    - First try _infer_iso_from_sheet_name(sheet).
    - If it fails, try detect_isotope_and_column(df) if available in this module.
    """
    xls = _pd.ExcelFile(xlsx_path)
    out = []
    want = tuple(wanted_isotopes) if wanted_isotopes else None
    for sheet in xls.sheet_names:
        try:
            df = xls.parse(sheet_name=sheet)
        except Exception as e:

            logging.warning(f"Exception caught: {e}"); continue
        iso = _infer_iso_from_sheet_name(sheet)
        if not iso:
            try:
                # Reuse project helper if present
                if 'detect_isotope_and_column' in globals():
                    det = detect_isotope_and_column(df)  # type: ignore[name-defined]
                    if isinstance(det, tuple) and det: iso = det[0]
                    elif isinstance(det, dict): iso = det.get('iso') or det.get('isotope') or det.get('iso_name')
                    elif isinstance(det, str): iso = det
            except Exception:
                iso = None
        if not iso:
            continue
        if want and iso not in want:
            continue
        tmp = _tempfile.NamedTemporaryFile(prefix=f"{sheet}_", suffix=".csv", delete=False)
        tmp_path = tmp.name; tmp.close()
        try:
            df.to_csv(tmp_path, index=False)
        except Exception:
            try: _os.remove(tmp_path)
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
            continue
        out.append((iso, tmp_path))
    return out

def process_multi_isotope_xlsx(data_xlsx, standards_data, instrument, config, corrections, roles, methods, isotopes=None, include_ignored=False):
    """Loop *.xlsx and reuse single-isotope pipeline once per sheet.
    We pass through 'instrument' as a label string because load_and_prepare_data
    internally constructs InstrumentType(instrument).
    """
    inst = str(getattr(instrument, 'value', instrument) or '')
    want = tuple(isotopes) if isotopes else None
    items = _xlsx_sheets_to_temp_csvs(data_xlsx, wanted_isotopes=want)

    results = {}
    temp_paths = []
    try:
        for iso, csv_path in items:
            temp_paths.append(csv_path)
            if not iso: 
                continue
            prepared_data, prepared_standards = load_and_prepare_data(csv_path, standards_data, inst)  # type: ignore[name-defined]
            corr_no_cal = [c for c in (corrections or []) if str(c).lower() != 'calibration']
            inj, ana, val, mem, drf = process_isotope_data(  # type: ignore[name-defined]
                prepared_data, prepared_standards, config, corr_no_cal, roles, methods, (iso,)
            )
            results[str(iso)] = {
                'injection': inj,
                'analysis': ana,
                'validation': val,
                'memory_fits': mem or {},
                'drift_fits': drf or {},
                'calibration_fits': {},  # keep empty; GUI may apply its own calibration view
            }
        return results
    finally:
        for pth in temp_paths:
            try: _os.remove(pth)
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
            
def _sem_by_analysis(inj_df, value_col: str) -> pd.Series:
    """
    SEM per Analysis for an injection-level column.
    Returns a Series indexed by Analysis.
    """
    import numpy as np
    import pandas as pd
    if not isinstance(inj_df, pd.DataFrame) or "Analysis" not in inj_df.columns or value_col not in inj_df.columns:
        return pd.Series(dtype=float)
    g = inj_df[["Analysis", value_col]].dropna()
    if g.empty:
        return pd.Series(dtype=float)
    grp = g.groupby("Analysis", dropna=False)[value_col]
    sem = grp.std(ddof=1) / np.sqrt(grp.count().clip(lower=1))
    return sem


def _stage_sd_from_fits(mem_fits: dict, dr_fits: dict, iso: str) -> float:
    """
    Combine memory-fit and drift-fit residual SDs (if available) for an isotope.
    Looks for convenient fields first, then falls back to residuals computed
    from y_raw vs prediction in each fit object. Returns NaN if nothing found.
    """
    import numpy as np

    def _sd_from_memory(mem_obj) -> float:
        if not isinstance(mem_obj, dict):
            return float("nan")
        # Prefer baked-in 'sd_resid'
        v = mem_obj.get("sd_resid")
        if v is not None:
            try:
                v = float(v)
                if v == v:
                    return v
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
        # Otherwise compute from per-analysis entries
        sds = []
        for k, fit in mem_obj.items():
            if not isinstance(fit, dict):
                continue
            y  = fit.get("y_raw") or fit.get("y")
            yh = fit.get("y_fit") or fit.get("y_fit_total") or fit.get("pred")
            if y is None or yh is None:
                continue
            try:
                ya = np.asarray(y, dtype=float)
                yha = np.asarray(yh, dtype=float)
                m = min(ya.size, yha.size)
                if m > 1:
                    r = ya[:m] - yha[:m]
                    s = float(np.nanstd(r, ddof=1))
                    if s == s:
                        sds.append(s)
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
        return (float(np.nanmedian(sds)) if sds else float("nan"))

    def _sd_from_drift(drobj) -> float:
        if not isinstance(drobj, dict):
            return float("nan")
        for key in ("sd_resid", "sd", "sigma", "stderr"):
            v = drobj.get(key)
            if v is not None:
                try:
                    v = float(v)
                    if v == v:
                        return v
                except Exception as e:

                    logging.warning(f"Exception caught: {e}")
        # If residuals were stashed:
        for key in ("resid", "residuals"):
            r = drobj.get(key)
            if r is not None:
                try:
                    r = np.asarray(r, dtype=float)
                    if r.size > 1:
                        s = float(np.nanstd(r, ddof=1))
                        if s == s:
                            return s
                except Exception as e:

                    logging.warning(f"Exception caught: {e}")
        return float("nan")

    mem_sd = float("nan")
    if isinstance(mem_fits, dict):
        obj = mem_fits.get(iso)
        if obj is not None:
            mem_sd = _sd_from_memory(obj)

    dr_sd = float("nan")
    if isinstance(dr_fits, dict):
        obj = dr_fits.get(iso)
        if obj is not None:
            dr_sd = _sd_from_drift(obj)

    # Quadrature, skipping NaNs where possible
    parts = [x for x in (mem_sd, dr_sd) if x == x]
    if not parts:
        return float("nan")
    return float(np.sqrt(sum(p*p for p in parts)))


def apply_intra_sample_memory_correction(data, isotope_col, config, method):
    """
    Per-analysis (intra-sample) memory correction using an Exponential or Asymptotic model.

    Returns:
        (df_with_col, fits_dict)
    where:
        df_with_col[f"{isotope_col}_memory_corrected"] is written for all rows
        fits_dict = {
            "<iso>": {
                <analysis_id>: {"x_raw":..., "y_raw":..., "x_fit":..., "y_fit":...},
                "sd_resid": <float, optional overall residual SD>
            }
        }
    """

    df = data.copy()
    out_col = f"{isotope_col}_memory_corrected"
    iso_name = isotope_col

    if "Analysis" not in df.columns or "injection_no" not in df.columns:
        df[out_col] = df.get(isotope_col, np.nan)
        return df, {}

    if "is_outlier" in df.columns:
        ok_mask = (df["is_outlier"] == False)
    else:
        ok_mask = pd.Series(True, index=df.index)

    def _exp_fun(x, a, k, c):   # decays toward c
        return a * np.exp(-k * (x - 1.0)) + c

    def _asymp_fun(x, a, b):    # a/x + b
        return (a / x) + b

    fits_per_analysis = {}
    resid_sds = []

    for aid, g in df.groupby("Analysis", dropna=False):
        g = g.loc[ok_mask.reindex(g.index, fill_value=True)].dropna(subset=["injection_no", isotope_col])
        if g.empty:
            continue

        X = pd.to_numeric(g["injection_no"], errors="coerce").dropna().astype(float)
        Y = pd.to_numeric(g[isotope_col], errors="coerce").reindex(X.index).astype(float)
        if X.empty or Y.empty:
            continue

        valids = int(getattr(config, "valids", 5) or 5)
        corrected_val = Y.tail(valids).mean()

        fit_info = None
        try:
            m = str(method).lower()
            if m.startswith("exp"):
                y0, yend = float(Y.iloc[0]), float(Y.iloc[-1])
                a0 = y0 - yend; k0 = 1.0; c0 = yend
                bounds = ([-abs(a0)*10, 0.0, min(Y.min(), c0-abs(a0)*10)],
                          [ abs(a0)*10, 10.0, max(Y.max(), c0+abs(a0)*10)])
                popt, _ = curve_fit(_exp_fun, X.values, Y.values, p0=[a0, k0, c0], bounds=bounds, maxfev=2000)
                corrected_val = float(popt[2])
                x_fit = np.linspace(float(X.min()), float(X.max()), 80)
                y_fit = _exp_fun(x_fit, *popt)
                resid = Y.values - _exp_fun(X.values, *popt)

            elif m.startswith("asym"):
                a0 = float(max((Y.max() - Y.min()), 0.1)); b0 = float(Y.tail(valids).mean())
                popt, _ = curve_fit(_asymp_fun, X.values, Y.values, p0=[a0, b0], maxfev=2000)
                corrected_val = float(popt[1])
                x_fit = np.linspace(float(X.min()), float(X.max()), 80)
                y_fit = _asymp_fun(x_fit, *popt)
                resid = Y.values - _asymp_fun(X.values, *popt)

            else:
                x_fit = y_fit = resid = None

            if x_fit is not None:
                fit_info = {"x_raw": X.values, "y_raw": Y.values, "x_fit": x_fit, "y_fit": y_fit}
                if resid is not None and len(resid) > 1:
                    s = float(np.nanstd(resid, ddof=1))
                    if s == s:
                        resid_sds.append(s)
        except Exception:
            fit_info = None

        df.loc[df["Analysis"] == aid, out_col] = corrected_val

        if fit_info is not None:
            try:
                aid_key = int(aid)
            except Exception:
                aid_key = aid
            fits_per_analysis[aid_key] = fit_info

    if out_col not in df.columns:
        df[out_col] = df.get(isotope_col, np.nan)

    fits = {iso_name: fits_per_analysis}
    if resid_sds:
        fits[iso_name]["sd_resid"] = float(np.nanmedian(resid_sds))

    return df, fits



