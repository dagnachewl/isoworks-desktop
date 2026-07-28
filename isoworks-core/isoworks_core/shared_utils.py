"""
shared_utils.py — Shared utilities and UI helpers for the IsoWorks application.
Provides IconCache (SVG icon rendering), ModuleSpec, ComingSoonWidget, and
common helper functions used across GUI modules (privilege checks, status updates).
"""
# shared_utils.py
import logging
import getpass
import numpy as np
import math
from datetime import datetime
from typing import Optional, Callable
from sqlalchemy import text
from .db_core import db_manager
import logging



# Headless core shared utilities - PyQt5 GUI dependencies removed
        
# --- Constants (Ported from VBA / Config) ---
HALF_LIFE_TRITIUM = 4500.0  # Days
HALF_LIFE_TRITIUM_UNC = 8.0 # Days
TU_TO_DPM = 7.15            # Approx factor (Tritium Units to DPM/kg)
TU_TO_DPM_UNC = 0.1

# Unit IDs (Matching Access)
UNIT_TU = 1
UNIT_DPM = 2
UNIT_BQ = 3
UNIT_PCI = 4
UNIT_CPM = 5

def normalize_login_name(login_name: str) -> str:
    """
    Remove domain prefix from Windows login.
    "DOMAIN\\username" → "username"
    """
    if not login_name:
        return ""
    
    # Strip domain prefix if present
    if '\\' in login_name:
        username = login_name.split('\\')[-1]
    else:
        username = login_name
    
    # Lowercase for case-insensitive comparison
    return username.lower().strip()

_SUPER_ROLES: frozenset[str] = frozenset({"Super_Admin"})


def check_employee_privilege(login_name: str, role_name: str = "AccessDistillation") -> bool:
    """
    Privilege check against the unified RolePrivilege model.

    Resolution order:
      1. User holds a Super_Admin role              → True (all-access wildcard)
      2. role_name exactly matches one of the user's role names → True
      3. RolePrivilege table has (RoleID, role_name) for any of the user's roles → True
    """
    normalized_login = normalize_login_name(login_name)

    try:
        with db_manager.get_connection() as conn:
            user_roles = {
                r.RoleName
                for r in conn.execute(text("""
                    SELECT r.RoleName
                    FROM Employee_Role er
                    JOIN Employee e ON er.EmployeeID = e.EmployeeID
                    JOIN Role r ON er.RoleID = r.RoleID
                    WHERE LOWER(e.SystemLoginName) = :login
                """), {"login": normalized_login}).fetchall()
            }

            # 1. Super_Admin wildcard
            if user_roles & _SUPER_ROLES:
                return True

            # 2. Exact role-name match
            if role_name in user_roles:
                return True

            # 3. RolePrivilege lookup
            hit = conn.execute(text("""
                SELECT 1
                FROM Employee_Role er
                JOIN Employee e ON er.EmployeeID = e.EmployeeID
                JOIN RolePrivilege rp ON er.RoleID = rp.RoleID
                WHERE LOWER(e.SystemLoginName) = :login
                  AND LOWER(rp.PrivilegeName)  = :priv
            """), {"login": normalized_login, "priv": role_name.lower()}).fetchone()
            return hit is not None

    except Exception as exc:
        logging.error("Privilege check failed for '%s': %s", normalized_login, exc)
        return False

def get_current_user_id():
    """Returns the system username of the currently logged-in user."""
    return normalize_login_name(getpass.getuser())


def get_display_name(conn, table, id_value, last_name_col='LastName', first_middle_col='FirstMiddleName', id_col='EmployeeID'):
    """
    Fetches and formats a display name from the given table using the provided ID.
    Returns 'LASTNAME, FirstMiddleName' (LASTNAME uppercased), or '' if not found/ID is None.
    """
    if id_value is None:
        return ''
    sql = f"SELECT {last_name_col}, {first_middle_col} FROM {table} WHERE {id_col} = :id"
    row = conn.execute(text(sql), {'id': id_value}).fetchone()
    
    if not row:
        return ''
    last = (getattr(row, last_name_col, None) or '').strip().upper()
    first = (getattr(row, first_middle_col, None) or '').strip()
    if last and first:
        return f"{last}, {first}"
    elif last:
        return last
    elif first:
        return first
    return ''

def get_global_value(key, default=None):
    with db_manager.get_connection() as conn:
        row = conn.execute(text('SELECT TokenValue FROM GlobalValue WHERE Token = :n'), {'n': key}).fetchone()
        return (row.TokenValue if row and row.TokenValue is not None else default)


def set_global_value(key, value, description=None):
    with db_manager.get_connection() as conn:
        exists = conn.execute(text('SELECT 1 FROM GlobalValue WHERE Token = :n'), {'n': key}).scalar()
        if exists:
            conn.execute(text('UPDATE GlobalValue SET TokenValue = :v WHERE Token = :n'), {'v': value, 'n': key})
        else:
            conn.execute(text('INSERT INTO GlobalValue (Token, TokenValue, Description) VALUES (:n, :v, :d)'), {'n': key, 'v': value, 'd': description or ''})
        conn.commit()

# =============================
# Helpers: Equipment & Global Values
# =============================
def get_equipment_info(run_id):
    with db_manager.get_connection() as conn:
        row = conn.execute(text(
            'SELECT e.EquipmentID, e.EquipmentName, e.ModelName '
            'FROM TRIMS.LSCRun r JOIN Equipment e ON r.EquipmentID = e.EquipmentID '
            'WHERE r.RunID = :rid'
        ), {'rid': run_id}).fetchone()
        if not row:
            return None, None, None
        return int(row.EquipmentID), row.EquipmentName, row.ModelName
    
def calculate_decay_factor(old_date, new_date, old_unc_days=0.0, new_unc_days=0.0):
    """
    Computes exponential decay factor: exp(-lambda * delta_t).
    Returns (Factor, Uncertainty).
    """
    if not old_date or not new_date:
        return 1.0, 0.0
    
    # Ensure datetime objects
    if isinstance(old_date, str): old_date = datetime.fromisoformat(str(old_date))
    if isinstance(new_date, str): new_date = datetime.fromisoformat(str(new_date))
    
    delta = new_date - old_date
    delta_days = delta.days + (delta.seconds / 86400.0)
    
    if delta_days == 0:
        return 1.0, 0.0
    
    # Lambda = ln(2) / HalfLife
    decay_const = math.log(2) / HALF_LIFE_TRITIUM
    factor = math.exp(-decay_const * delta_days)
    
    # Uncertainty Propagation
    # f = exp(-ln(2)*t/T)
    # Relative Unc = ln(2) * (t/T) * sqrt( (dt/t)^2 + (dT/T)^2 )
    
    delta_days_unc = math.sqrt(old_unc_days**2 + new_unc_days**2)
    
    term_t = (delta_days_unc / delta_days) ** 2 if delta_days != 0 else 0
    term_T = (HALF_LIFE_TRITIUM_UNC / HALF_LIFE_TRITIUM) ** 2
    
    exponent = decay_const * delta_days
    rel_unc = exponent * math.sqrt(term_t + term_T)
    
    factor_unc = factor * rel_unc
    return factor, factor_unc

def get_standard_activity(conn, sample_id, count_date, target_unit_str="DPM",
                          measurable_id=3, DecayCorrected=True, Amount=10, AmountUnc=0.001 ):
    """
    Fetches reference activity from DB, converts unit, and applies decay correction.
    target_unit_str: "DPM", "Bq", "TU"
    Returns (Activity, Uncertainty, UnitID, IsDecayCorrected)
    """
    try:
        # Map str unit to ID
        unit_map = {"DPM": 2, "BQ": 3, "TU": 1, "PCI": 4}
        target_unit = unit_map.get(target_unit_str.upper(), 2)

        # 1. Fetch Reference Data
        sql = """
            SELECT d.CertifiedValue, d.CertifiedValueUnc, d.ReferenceDate, d.UnitID
            FROM ReferenceControl r
            JOIN ReferenceControlData d ON r.ReferenceID = d.ReferenceID
            WHERE r.SampleID = :sid AND d.MeasurableID = :mid
        """
        params = {"sid": sample_id, "mid": measurable_id}
        try:
            row = conn.execute(text(sql), params).fetchone()
        except Exception:
            # PostgreSQL aborts the whole transaction on any error; roll back so
            # this read-only retry doesn't inherit a poisoned transaction state.
            try:
                conn.rollback()
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
            row = conn.execute(text(sql), params).fetchone()
        
        if not row:
            return 0.0, 0.0, 0, False
            
        cert_val = float(row.CertifiedValue or 0)
        cert_unc = float(row.CertifiedValueUnc or 0)
        ref_date = row.ReferenceDate
        unit_id = row.UnitID

        if not ref_date or not DecayCorrected:
            return cert_val, cert_unc, unit_id, False
         
        # 2. Convert Unit if needed (Pre-decay)
        # Assuming Volume=1 for Standards defined as Specific Activity
        val_conv = cert_val
        unc_conv = cert_unc
        if target_unit != unit_id:
            val_conv, unc_conv = convert_activity_unit(unit_id, target_unit, cert_val, cert_unc, volume=Amount, volume_unc=AmountUnc)
        # 3. Decay Correction
        if DecayCorrected: df, df_unc = calculate_decay_factor(ref_date, count_date)
        
        final_val = val_conv * df
        # Unc: A * B -> sqrt((da/a)^2 + (db/b)^2)
        rel_val = (unc_conv / val_conv)**2 if val_conv else 0
        rel_df = (df_unc / df)**2 if df else 0
        final_unc = abs(final_val) * math.sqrt(rel_val + rel_df)
        
        return final_val, final_unc, target_unit, True
        
    except Exception as e:
        logging.error(f"get_standard_activity error: {e}")
        return 0.0, 0.0, 0, False

# detection_limits.py
# ---------------------------------------------------------------------
# Currie / ISO 11929 style decision threshold (LC) and detection limit (LLD/MDA)
# for LSC count-rate data, plus unit conversions with uncertainty propagation.
# ---------------------------------------------------------------------

from math import sqrt, isfinite
from typing import Optional, Tuple, Dict

import pandas as pd
from sqlalchemy import text

# --- Constants (can be moved to shared config) ---
K_ALPHA = 1.645  # one-sided 95% decision threshold (α)
K_BETA  = 1.645  # one-sided 95% detection probability (β) 

# Lab constants for unit conversion (can be parameterized)
PCI_TO_DPM = 2.22
PCI_TO_BQ  = 0.037
DPM_TO_BQ  = 1.0 / 60.0


# ---------------------------------------------------------------------
# Lower detection limit solver on count rates, then convert to DPM/kg
# ---------------------------------------------------------------------
def lower_detection_limit(
    bkg_cpm: float,
    bkg_time_min: float,
    sample_time_min: float,
    eff_frac: float,
    eff_unc_frac: float = 0.0,
    vol_kg: float = 1.0,
    vol_unc_kg: float = 0.0,
    max_iter: int = 50,
    tol: float = 1e-9
) -> Dict[str, float]:
    """
    Compute decision threshold (LC) and detection limit (LLD/MDA) using the Currie/ISO11929
    approach for count rates, then convert to DPM/kg via efficiency and mass.

    Inputs:
      bkg_cpm        : mean background count rate (CPM)
      bkg_time_min   : total background counting time (minutes)
      sample_time_min: per-sample counting time (minutes)
      eff_frac       : counter efficiency as a fraction (e.g., 0.25 for 25%)
      eff_unc_frac   : uncertainty of efficiency (fraction)
      vol_kg         : analyzed mass (kg)
      vol_unc_kg     : uncertainty of analyzed mass (kg)
      max_iter, tol  : iteration controls for Currie detection limit solver
    Returns:
      dict with keys:
        'LC_cpm'      : decision threshold (count rate)
        'LD_cpm'      : detection limit (count rate)
        'LC_dpm_per_kg': decision threshold (DPM/kg)
        'LD_dpm_per_kg': detection limit (DPM/kg) [MDA]
        'unit'        : 'DPM/kg'
    Method:
      - σ_b (rate) = sqrt(B) / t_b where B = bkg_cpm * t_b and t_b=bkg_time_min.
      - LC_rate = kα * σ_b
      - Solve Ld_rate = kα*σ_b + kβ*sqrt(σ_b^2 + Ld_rate / t_s) iteratively (Currie).
      - Convert rate to activity: DPM = rate / eff; massic activity = DPM / vol_kg.
    """
    if bkg_cpm < 0 or bkg_time_min <= 0 or sample_time_min <= 0:
        raise ValueError("Background rate/time and sample time must be positive.")
    if eff_frac <= 0:
        raise ValueError("Efficiency must be a positive fraction, e.g., 0.25 for 25%.")
    # Decision threshold (counts)
    lc_counts = K_ALPHA * sqrt(bkg_cpm * sample_time_min * (1 + sample_time_min / bkg_time_min))
    lc_cpm = lc_counts / sample_time_min
    # Detection limit (full Currie 1968 closed form, matching VBA)
    ld_counts = lc_counts + (K_BETA**2)/2 * (1 + sqrt(1 + 4*lc_counts/(K_BETA**2) + 4*lc_counts**2/(K_ALPHA**2 * K_BETA**2)))
    ld_cpm = ld_counts / sample_time_min
    # Convert to DPM/kg
    lc_dpm_kg = (lc_cpm / eff_frac) / vol_kg
    ld_dpm_kg = (ld_cpm / eff_frac) / vol_kg
    # print(f"""
    #     bkg_cpm: {bkg_cpm},
    #     sample_time_min: {sample_time_min},
    #     bkg_time_min: {bkg_time_min},
    #     LC_cpm: {lc_cpm},
    #     LD_cpm: {ld_cpm},
    #     LC_dpm_per_kg: {lc_dpm_kg},
    #     LD_dpm_per_kg: {ld_dpm_kg},
    #     unit: DPM/kg""")
    # (Optional) add first-order uncertainty propagation if needed;
    return {
        'LC_cpm': lc_cpm,
        'LD_cpm': ld_cpm,
        'LC_dpm_per_kg': lc_dpm_kg,
        'LD_dpm_per_kg': ld_dpm_kg,
        'unit': 'DPM/kg'
    }

# ---------------------------------------------------------------------
# TRIMS Access-style wrapper: read parameters for RunID and compute
# ---------------------------------------------------------------------

def _compute_detection_limit_from_run_base(
    conn,
    run_id: int,
    mda_unit: int = 2,  # 1=TU, 2=DPM/kg, 3=Bq/kg, 4=pCi/L
    volume_unc_cfg_token: str = "VOLUME_UNCERTAINTY_COCKTAIL"
) -> Dict[str, float]:
    
    """
    Compute LC/MDA for a run using aggregated CPM means and times from vwLSCmeancpm.

    Background formation (standard path):
      - bkg_cpm      := (weighted mean of cpmMeanValue for SampleType=1),
                        weighting priority: 1/unc^2 if cpmMeanValueUnc exists and >0,
                        otherwise time-weighted by TotalCountTime.
      - bkg_time_min := SUM(TotalCountTime) WHERE SampleType=1        <-- (your rule)

    Sample dwell (representative):
      - sample_time_min := median(TotalCountTime) WHERE SampleType != 1

    HIDEX special case (if blanks, SampleType=9, present):
      - bkg_time_min     := 5 * median(TotalCountTime WHERE SampleType=9)
      - sample_time_min  :=     median(TotalCountTime WHERE SampleType=9)

    Efficiency/mass:
      - Efficiency read from LSCRun; accept fraction or % and normalize to fraction
      - Sample mass from AnalysisProcedure.SampleSize (g) → kg
      - Volume uncertainty from GlobalValue[volume_unc_cfg_token] (g) → kg
    """
    # --- Run-level and procedure settings (as before) ---

    run_sql = """
        SELECT r.ProcedureID,
            r.CounterEfficiency           AS eff,
            r.CounterEfficiencyUnc        AS eff_unc,
            r.MeanBackground              AS bkg_cpm_fallback,
            r.MeanBackgroundUnc           AS bkg_unc_fallback,
            lp.CycleLength                AS cycle_len_min,
            lp.NumberOfCycles             AS n_cycles
        FROM TRIMS.LSCRun r
        JOIN LSCProcedure lp ON lp.ProcedureID = r.ProcedureID
        WHERE r.RunID = :rid
    """
    r0 = conn.execute(text(run_sql), {'rid': run_id}).fetchone()
    if not r0:
        raise RuntimeError(f"RunID {run_id} not found in LSCRun/AnalysisProcedure/LSCProcedure.")

    eff_raw      = float(getattr(r0, 'eff', 0.0) or 0.0)
    eff_unc_raw  = float(getattr(r0, 'eff_unc', 0.0) or 0.0)
    bkg_fallback = float(getattr(r0, 'bkg_cpm_fallback', 0.0) or 0.0)

    # Normalize efficiency to fraction
    eff_frac     = eff_raw     if eff_raw     <= 1.0 else eff_raw     / 100.0
    eff_unc_frac = eff_unc_raw if eff_unc_raw <= 1.0 else eff_unc_raw / 100.0
    if eff_frac <= 0:
        raise ValueError("CounterEfficiency is not set or invalid for this run.")

    # --- NEW: derive sample_size_g from LSCLoadList.SampleAmount (grams) ---
    res = conn.execute(
        text("""
            SELECT SampleType, SampleAmount
            FROM TRIMS.LSCLoadList
            WHERE RunID = :rid
        """),
        {'rid': run_id}
    )

    rows = res.fetchall()     # Whatever _CIResult returns, this will always be a list

    # Convert each row to a tuple so pandas sees a clean 2‑column structure
    clean_rows = [tuple(r) for r in rows]

    df_amt = pd.DataFrame(clean_rows, columns=['SampleType', 'SampleAmount'])

    if df_amt.empty:
        df_amt = pd.DataFrame(columns=['SampleType', 'SampleAmount'])


    def _safe_median(series):
        s = pd.to_numeric(series, errors='coerce')
        s = s[(s.notna()) & (s > 0)]
        return float(s.median()) if not s.empty else 0.0

    # Prefer median SampleAmount over non-background rows
    sample_size_g = 0.0
    if not df_amt.empty:
        sample_size_g = _safe_median(df_amt.loc[df_amt['SampleType'] != 1, 'SampleAmount'])
        if sample_size_g <= 0:
            # fallback to max non-background
            s = pd.to_numeric(df_amt.loc[df_amt['SampleType'] != 1, 'SampleAmount'], errors='coerce')
            s = s[(s.notna()) & (s > 0)]
            sample_size_g = float(s.max()) if not s.empty else 0.0
        if sample_size_g <= 0:
            # ultimate fallback: max across the run
            s = pd.to_numeric(df_amt['SampleAmount'], errors='coerce')
            s = s[(s.notna()) & (s > 0)]
            sample_size_g = float(s.max()) if not s.empty else 0.0

    # Convert grams -> kg for detection-limit mass basis
    vol_kg = (sample_size_g / 1000.0) if sample_size_g > 0 else 0.0

    # Volume uncertainty from GlobalValue (g -> kg)
    vol_unc_gv = conn.execute(
        text("SELECT TokenValue FROM GlobalValue WHERE Token=:tok"),
        {'tok': volume_unc_cfg_token}
    ).scalar()
    vol_unc_kg = (float(vol_unc_gv) / 1000.0) if vol_unc_gv else 0.0

    if vol_kg <= 0:
        logging.warning(
            "Sample mass from LSCLoadList.SampleAmount is missing/zero; "
            "using 1.0 kg for detection‑limit mass basis."
        )
        vol_kg = 1.0


    # --- Pull aggregated CPM & time from the view for this run ---
    _cpm_res = conn.execute(
        text("""
            SELECT RunID, PositionInRun, SampleType,
                cpmMeanValue, cpmMeanValueUnc, TotalCountTime
            FROM vwLSCmeancpm
            WHERE RunID = :rid
        """),
        {'rid': run_id}
    )

    # Try to get column names from the result; if not available, use the known order.
    try:
        _cpm_cols = list(_cpm_res.keys())
    except Exception:
        _cpm_cols = ['RunID', 'PositionInRun', 'SampleType',
                    'cpmMeanValue', 'cpmMeanValueUnc', 'TotalCountTime']

    _cpm_rows = _cpm_res.fetchall()

    # Ensure pandas sees a proper 2D structure
    data = [tuple(r) for r in _cpm_rows]

    df = pd.DataFrame(data, columns=_cpm_cols) if data else pd.DataFrame(columns=_cpm_cols)

    # If no rows, fallback route using LSCRun/LSCLoadList
    if df.empty:
        logging.warning("vwLSCmeancpm returned no rows for this run; "
                        "falling back to LSCRun background and LSCLoadList times.")

        bkg_time_min = float(conn.execute(text(
            "SELECT COALESCE(SUM(CountTime),0) FROM TRIMS.LSCLoadList "
            "WHERE RunID=:rid AND SampleType=1"
        ), {'rid': run_id}).scalar() or 0.0)

        sample_time_min = float(conn.execute(text(
            "SELECT COALESCE(AVG(CountTime),0) FROM TRIMS.LSCLoadList "
            "WHERE RunID=:rid AND SampleType<>1"
        ), {'rid': run_id}).scalar() or 0.0)

        if sample_time_min <= 0:
            sample_time_min = float(conn.execute(text(
                "SELECT COALESCE(MAX(CountTime),0) FROM TRIMS.LSCLoadList WHERE RunID=:rid"
            ), {'rid': run_id}).scalar() or 0.0)

        bkg_cpm = bkg_fallback

    else:
        # Partition
        df_bg    = df[df['SampleType'] == 1].copy()
        df_blank = df[df['SampleType'] == 9].copy()  # HIDEX blanks

        # HIDEX path if blanks present in view
        if not df_blank.empty:
            ct_med        = float(df_blank['TotalCountTime'].median() or 0.0)
            bkg_time_min  = 5.0 * ct_med
            sample_time_min = ct_med

            # Background rate from backgrounds, if available; else fallback.
            if not df_bg.empty:
                if (df_bg['cpmMeanValueUnc'].fillna(0).infer_objects(copy=False) > 0).any():
                    w = 1.0 / (df_bg['cpmMeanValueUnc'].replace(0, np.nan) ** 2)
                    bkg_cpm = float(np.nansum(df_bg['cpmMeanValue'] * w) / np.nansum(w))
                else:
                    tw = df_bg['TotalCountTime'].replace(0, np.nan)
                    bkg_cpm = float(np.nansum(df_bg['cpmMeanValue'] * tw) / np.nansum(tw))
            else:
                bkg_cpm = bkg_fallback

        # Standard path
        else:
            # bkg_time_min := SUM(TotalCountTime) WHERE SampleType=1
            bkg_time_min = float(df_bg['TotalCountTime'].sum()) if not df_bg.empty else 0.0

            # Representative sample time = median over non-background rows
            df_samp = df[df['SampleType'] != 1]
            sample_time_min = float(df_samp['TotalCountTime'].median() or 0.0) if not df_samp.empty else 0.0

            if sample_time_min <= 0:
                sample_time_min = float(df['TotalCountTime'].max() or 0.0)

            # Background CPM (rate) from the view
            if not df_bg.empty:
                # prefer 1/unc^2 weighting when uncertainties exist; else time-weighted mean
                if (df_bg['cpmMeanValueUnc'].fillna(0).infer_objects(copy=False) > 0).any():
                    w = 1.0 / (df_bg['cpmMeanValueUnc'].replace(0, np.nan) ** 2)
                    bkg_cpm = float(np.nansum(df_bg['cpmMeanValue'] * w) / np.nansum(w))
                else:
                    tw = df_bg['TotalCountTime'].replace(0, np.nan)
                    bkg_cpm = float(np.nansum(df_bg['cpmMeanValue'] * tw) / np.nansum(tw))
            else:
                # No backgrounds in the view; fall back to LSCRun value
                bkg_cpm = bkg_fallback

    # Guards
    if bkg_time_min <= 0 or sample_time_min <= 0:
        raise ValueError("Derived background/sample times are invalid (<=0). "
                         "Check vwLSCmeancpm contents or SampleType mapping.")

    # --- Core Currie/ISO calculation in DPM/kg ---
    dl = lower_detection_limit(
        bkg_cpm=bkg_cpm,
        bkg_time_min=bkg_time_min,
        sample_time_min=sample_time_min,
        eff_frac=eff_frac,
        eff_unc_frac=eff_unc_frac,
        vol_kg=vol_kg,
        vol_unc_kg=vol_unc_kg
    )

    return {
        'LC_dpm_per_kg': dl['LC_dpm_per_kg'],
        'LD_dpm_per_kg': dl['LD_dpm_per_kg'],
        'LC_in_unit': dl['LC_dpm_per_kg'],
        'MDA_in_unit': dl['LD_dpm_per_kg'],
        'unit': {1: 'TU', 2: 'DPM/kg', 3: 'Bq/kg', 4: 'pCi/L'}.get(mda_unit, 'DPM/kg'),
        'bkg_time_min': bkg_time_min,
        'sample_time_min': sample_time_min
    }

def compute_detection_limit_from_run(
    conn,
    run_id: int,
    run_unit: int = 2,  # e.g. 2 = DPM/kg (default), 1 = TU, 3 = Bq/kg, 4 = pCi/L
    mda_unit: int = 2,  # requested output unit
    volume_unc_cfg_token: str = "VOLUME_UNCERTAINTY_COCKTAIL"
) -> dict:
    """
    Compute detection limits for a run, and convert to requested unit if needed.

    Returns:
        dict with keys:
            'LC'  : Decision threshold (in requested unit)
            'MDA' : Detection limit (in requested unit)
            'unit': Unit label
            ...    (other diagnostic info)
    """
    # --- Compute in base unit (run_unit) ---
    dl = _compute_detection_limit_from_run_base(
        conn, run_id, run_unit, volume_unc_cfg_token
    )
    lc_val = dl['LC_in_unit']
    mda_val = dl['MDA_in_unit']
    lc_unc = dl.get('LC_unc', 0.0)
    mda_unc = dl.get('MDA_unc', 0.0)

    # --- Convert if needed ---
    if run_unit != mda_unit:        
        # Map units: 1=TU, 2=DPM/kg, 3=Bq/kg, 4=pCi/L, 5=CPM
        # For DPM/kg <-> TU, need sample mass (kg)
        sample_mass_kg = dl.get('sample_mass_kg', 1.0)
        sample_mass_unc = dl.get('sample_mass_unc', 0.0)
        # For CPM <-> DPM, need efficiency
        eff = dl.get('efficiency', None)
        eff_unc = dl.get('efficiency_unc', 0.0)

        lc_val, lc_unc = convert_activity_unit(
            unit_from=run_unit, unit_to=mda_unit,
            c_value=lc_val, c_unc=lc_unc,
            volume=sample_mass_kg, volume_unc=sample_mass_unc,
            efficiency=eff, efficiency_unc=eff_unc,
            return_type=1
        )
        mda_val, mda_unc = convert_activity_unit(
            unit_from=run_unit, unit_to=mda_unit,
            c_value=mda_val, c_unc=mda_unc,
            volume=sample_mass_kg, volume_unc=sample_mass_unc,
            efficiency=eff, efficiency_unc=eff_unc,
            return_type=1
        )

    # --- Prepare output ---
    unit_map = {1: 'TU', 2: 'DPM/kg', 3: 'Bq/kg', 4: 'pCi/L', 5: 'CPM'}
    return {
        'LC': lc_val,
        'LC_unc': lc_unc,
        'MDA': mda_val,
        'MDA_unc': mda_unc,
        'unit': unit_map.get(mda_unit, 'DPM/kg'),
        **dl  # include all diagnostic info (background, times, etc.)
    }

# ---------------------------------------------------------------------
# Unit conversion with uncertainty (Python version of VBA ConvertActivityUnit)
# ---------------------------------------------------------------------
def convert_activity_unit(
    unit_from: int, unit_to: int,
    c_value: float, c_unc: float,
    *,
    volume: float = 1000.0,            # Input in grams (standard for your lab)
    volume_unc: float = 0.0,           # Uncertainty in grams
    efficiency: Optional[float] = None,
    efficiency_unc: float = 0.0,
    tu_to_dpm_per_kg: float = 7.10,     # Standard factor
    tu_to_dpm_per_kg_unc: float = 0.0,
    return_type: int = 1
) -> Tuple[float, float]:
    """
    Standardized conversion for massic activity (DPM/kg, Bq/kg, TU).
    Standardizes all inputs to DPM/kg for internal calculation.
    """
    # 1. Standardize Mass (Grams to Kg)
    vol_kg = volume / 1000.0 if volume > 0 else 1.0
    vol_rel_unc = (volume_unc / volume) if volume > 0 else 0.0

    # 2. Convert Input to Base Unit: DPM/kg
    # Units: 1=TU, 2=DPM/kg, 3=Bq/kg, 4=pCi/L, 5=CPM
    if unit_from == 1:  # TU to DPM/kg
        base_dpm_kg = c_value * tu_to_dpm_per_kg
        rel_sq = (c_unc/c_value)**2 + (tu_to_dpm_per_kg_unc/tu_to_dpm_per_kg)**2 if c_value else 0
        base_unc = base_dpm_kg * math.sqrt(rel_sq)

    elif unit_from == 2:  # Already DPM/kg
        base_dpm_kg = c_value
        base_unc = c_unc

    elif unit_from == 3:  # Bq/kg to DPM/kg (1 Bq = 60 DPM)
        base_dpm_kg = c_value * 60.0
        base_unc = c_unc * 60.0

    elif unit_from == 4:  # pCi/kg to DPM/kg (1 pCi = 2.22 DPM)
        base_dpm_kg = c_value * 2.22
        base_unc = c_unc * 2.22

    elif unit_from == 5:  # Raw CPM to DPM/kg
        if not efficiency or efficiency <= 0:
            raise ValueError("Efficiency required for CPM conversion.")
        # net_cpm / efficiency / mass_kg
        base_dpm_kg = (c_value / efficiency) / vol_kg
        rel_sq = (c_unc/c_value)**2 + (efficiency_unc/efficiency)**2 + (vol_rel_unc)**2 if c_value else 0
        base_unc = base_dpm_kg * math.sqrt(rel_sq)
    else:
        raise ValueError("Unsupported source unit.")

    # 3. Convert Base (DPM/kg) to Target Unit
    if unit_to == 1:  # To TU
        activity = base_dpm_kg / tu_to_dpm_per_kg
        rel_sq = (base_unc/base_dpm_kg)**2 + (tu_to_dpm_per_kg_unc/tu_to_dpm_per_kg)**2 if base_dpm_kg else 0
        activity_unc = activity * math.sqrt(rel_sq)

    elif unit_to == 2:  # To DPM/kg
        activity, activity_unc = base_dpm_kg, base_unc

    elif unit_to == 3:  # To Bq/kg
        activity = base_dpm_kg / 60.0
        activity_unc = base_unc / 60.0

    elif unit_to == 4:  # To pCi/L (Approximating kg ~ L for water)
        activity = base_dpm_kg / 2.22
        activity_unc = base_unc / 2.22
    else:
        raise ValueError("Unsupported target unit.")

    return (activity_unc if return_type == 2 else activity, activity_unc)

# --- GUI Utilities ---
def set_status(label: QLabel, text: str, variant: str = "neutral"):
    """Updates a QLabel with text and styling."""
    if not label: return
    label.setText(text)
    style_map = {
        "success": "background-color: #d1fae5; color: #065f46; border: 1px solid #34d399; border-radius: 4px; padding: 4px; font-weight: bold;",
        "error": "background-color: #fee2e2; color: #991b1b; border: 1px solid #f87171; border-radius: 4px; padding: 4px; font-weight: bold;",
        "processing": "background-color: #dbeafe; color: #1e40af; border: 1px solid #60a5fa; border-radius: 4px; padding: 4px; font-weight: bold;",
        "neutral": "background-color: transparent; color: #374151; padding: 4px; font-weight: bold;"
    }
    label.setStyleSheet(style_map.get(variant, style_map["neutral"]))

# --- Patch: detection limits & decay-corrected standard activity ---
import math
from typing import Optional, Tuple

# Safe name formatter
def format_person_name(last_name: Optional[str], first_middle_name: Optional[str]) -> str:
    last = (last_name or '').strip().upper()
    first = (first_middle_name or '').strip()
    if last and first:
        return f"{last}, {first}"
    elif last:
        return last
    elif first:
        return first
    return ''