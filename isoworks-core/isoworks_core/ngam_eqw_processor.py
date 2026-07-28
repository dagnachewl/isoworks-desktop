"""
ngam_eqw_processor.py
=====================
Pure functions for EQW (Equilibrium Water) correction-factor computation
and DB persistence.

Workflow
--------
1. After process_sequence() runs for an NG sequence that contains EQW inlets
   (sampletype = 50), call extract_eqw_cf_data(ir) for each such InletProcessingResult.
2. If Step 11 ran (extraction_info was provided), per-gas ccSTP_per_g and
   c_eq_cm3_per_g are already populated on each IsotopeResult.
   CF = c_eq_cm3_per_g / ccSTP_per_g.
3. Call save_eqw_run() inside the existing DB transaction to persist one row
   per EQW inlet to ngam.ng_eqw_run.

CF aggregation (used by the CF management GUI, not here)
---------------------------------------------------------
For a given (container_type_id, equipmentid):
  - Collect all ng_eqw_run rows where lock_X = 0
  - CF_X = mean(cf_X)
  - err_X = std(cf_X, ddof=1) / mean(cf_X)   (relative 1σ)
"""
from __future__ import annotations

import math
import logging
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

GASES: List[str] = ["He", "Ne", "Ar", "Kr", "Xe"]

# For each element, the isotope key suffixes to search (highest priority first)
# The key format in InletProcessingResult.isotopes is "device:isotope"
_ISO_SUFFIXES: Dict[str, List[str]] = {
    "He": ["4He"],
    "Ne": ["20Ne", "Ne"],
    "Ar": ["40Ar", "Ar"],
    "Kr": ["84Kr", "Kr"],
    "Xe": ["132Xe", "Xe"],
}


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------

def _find_isotope(ir, element: str):
    """Return the first matching IsotopeResult for an element, or None."""
    for suffix in _ISO_SUFFIXES.get(element, [element]):
        for key, iso in ir.isotopes.items():
            label = key.split(":", 1)[-1]
            if label == suffix or label.endswith(suffix):
                return iso
    return None


def extract_eqw_cf_data(ir) -> Optional[Dict]:
    """
    Given an InletProcessingResult for an EQW sample (after Step 11 ran),
    return a dict of per-gas concentrations, equilibrium values and CFs.

    Returns None if no gas has valid Step-11 data (extraction_info absent).

    Dict keys (g = lowercase element name: he / ne / ar / kr / xe):
        {g}_meas  – ccSTP_per_g  (measured dissolved concentration)
        {g}_err   – ccSTP_per_g_unc  (1σ absolute)
        {g}_eq    – c_eq_cm3_per_g  (Weiss equilibrium)
        cf_{g}    – eq / meas  (correction factor; None when meas ≤ 0 or missing)
    """
    result: Dict = {}
    any_valid = False

    for g in GASES:
        g_lo = g.lower()
        iso = _find_isotope(ir, g)

        meas = eq = err = cf = None
        if (iso is not None
                and not math.isnan(iso.ccSTP_per_g)
                and not math.isnan(iso.c_eq_cm3_per_g)
                and iso.ccSTP_per_g > 0):
            meas = iso.ccSTP_per_g
            eq   = iso.c_eq_cm3_per_g
            err  = iso.ccSTP_per_g_unc if not math.isnan(iso.ccSTP_per_g_unc) else None
            cf   = eq / meas
            any_valid = True

        result[f"{g_lo}_meas"] = meas
        result[f"{g_lo}_err"]  = err
        result[f"{g_lo}_eq"]   = eq
        result[f"cf_{g_lo}"]   = cf

    return result if any_valid else None


# ---------------------------------------------------------------------------
# DB persistence
# ---------------------------------------------------------------------------

def save_eqw_run(
    conn,
    analysisid: int,
    container_type_id: Optional[int],
    equipmentid: Optional[int],
    temperature_c: Optional[float],
    pressure_torr: Optional[float],
    salinity_ppt: float,
    cf_data: Dict,
    created_by: str,
) -> Optional[int]:
    """
    INSERT one row into ngam.ng_eqw_run within an existing connection.
    Uses ON CONFLICT DO NOTHING so re-saving the same analysis is safe.
    Returns the new eqw_run_id or None on failure.
    """
    from sqlalchemy import text

    g_lo = [g.lower() for g in GASES]
    try:
        row = conn.execute(text("""
            INSERT INTO ngam.ng_eqw_run (
                analysisid, container_type_id, equipmentid,
                temperature_c, pressure_torr, salinity_ppt,
                he_meas, ne_meas, ar_meas, kr_meas, xe_meas,
                he_err,  ne_err,  ar_err,  kr_err,  xe_err,
                he_eq,   ne_eq,   ar_eq,   kr_eq,   xe_eq,
                cf_he,   cf_ne,   cf_ar,   cf_kr,   cf_xe,
                created_by
            ) VALUES (
                :aid, :ctype, :equip,
                :temp, :press, :sal,
                :he_m, :ne_m, :ar_m, :kr_m, :xe_m,
                :he_e, :ne_e, :ar_e, :kr_e, :xe_e,
                :he_q, :ne_q, :ar_q, :kr_q, :xe_q,
                :cf_he, :cf_ne, :cf_ar, :cf_kr, :cf_xe,
                :usr
            )
            ON CONFLICT DO NOTHING
            RETURNING eqw_run_id
        """), {
            "aid":   analysisid,
            "ctype": container_type_id,
            "equip": equipmentid,
            "temp":  temperature_c,
            "press": pressure_torr,
            "sal":   salinity_ppt,
            # measured
            "he_m":  cf_data.get("he_meas"),
            "ne_m":  cf_data.get("ne_meas"),
            "ar_m":  cf_data.get("ar_meas"),
            "kr_m":  cf_data.get("kr_meas"),
            "xe_m":  cf_data.get("xe_meas"),
            # errors
            "he_e":  cf_data.get("he_err"),
            "ne_e":  cf_data.get("ne_err"),
            "ar_e":  cf_data.get("ar_err"),
            "kr_e":  cf_data.get("kr_err"),
            "xe_e":  cf_data.get("xe_err"),
            # equilibrium
            "he_q":  cf_data.get("he_eq"),
            "ne_q":  cf_data.get("ne_eq"),
            "ar_q":  cf_data.get("ar_eq"),
            "kr_q":  cf_data.get("kr_eq"),
            "xe_q":  cf_data.get("xe_eq"),
            # CFs
            "cf_he": cf_data.get("cf_he"),
            "cf_ne": cf_data.get("cf_ne"),
            "cf_ar": cf_data.get("cf_ar"),
            "cf_kr": cf_data.get("cf_kr"),
            "cf_xe": cf_data.get("cf_xe"),
            "usr":   created_by,
        })
        inserted = row.fetchone()
        return inserted[0] if inserted else None
    except Exception as exc:
        log.error("save_eqw_run failed for analysisid=%s: %s", analysisid, exc)
        return None


# ---------------------------------------------------------------------------
# CF aggregation (used by the GUI, not the save path)
# ---------------------------------------------------------------------------

def aggregate_cfs(
    eqw_rows: List[Dict],
) -> Dict[str, Optional[Tuple[float, float]]]:
    """
    Given a list of dicts (each row from ng_eqw_run, as returned by fetchall()),
    compute the aggregate CF and relative 1σ error for each gas.

    Expected keys per row: cf_he/cf_ne/cf_ar/cf_kr/cf_xe, lock_he/lock_ne/...

    Returns { 'He': (mean_cf, rel_err), 'Ne': ..., ... }
    Values are None when fewer than 1 unlocked measurement available.
    """
    result: Dict[str, Optional[Tuple[float, float]]] = {}

    for g in GASES:
        g_lo = g.lower()
        vals = [
            r[f"cf_{g_lo}"]
            for r in eqw_rows
            if r.get(f"lock_{g_lo}", 0) != 1
            and r.get(f"cf_{g_lo}") is not None
        ]
        if not vals:
            result[g] = None
            continue
        mu = sum(vals) / len(vals)
        if len(vals) >= 2:
            var = sum((v - mu) ** 2 for v in vals) / (len(vals) - 1)
            rel_err = math.sqrt(var) / mu if mu != 0 else float("nan")
        else:
            rel_err = float("nan")
        result[g] = (mu, rel_err)

    return result


def get_current_applied_cfs(
    conn,
    container_type_id: int,
    equipmentid: int,
) -> Optional[Dict[str, Tuple[float, float]]]:
    """
    Return the current applied CFs for (container_type_id, equipmentid).
    Returns { 'He': (cf, err), 'Ne': ..., ... } or None if no current template.
    """
    from sqlalchemy import text
    try:
        row = conn.execute(text("""
            SELECT applied_cf_he, applied_err_he,
                   applied_cf_ne, applied_err_ne,
                   applied_cf_ar, applied_err_ar,
                   applied_cf_kr, applied_err_kr,
                   applied_cf_xe, applied_err_xe
            FROM   ngam.ng_cf_template
            WHERE  container_type_id = :ctype
              AND  equipmentid       = :equip
              AND  is_current        = TRUE
            LIMIT  1
        """), {"ctype": container_type_id, "equip": equipmentid}).fetchone()
    except Exception as exc:
        log.warning("get_current_applied_cfs failed: %s", exc)
        return None

    if not row:
        return None

    pairs = list(row)
    result = {}
    for i, g in enumerate(GASES):
        cf  = pairs[i * 2]
        err = pairs[i * 2 + 1]
        if cf is not None:
            result[g] = (float(cf), float(err) if err is not None else float("nan"))
    return result if result else None
