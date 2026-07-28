"""
ams_reduction.py — AMS ¹⁴C data reduction engine for IsoWorks.

Pipeline (per wheel):
  1. OXI / OXII normalisation      — wheel-level standard mean
  2. δ¹³C calculation              — from measured ¹³C/¹²C vs VPDB
  3. Fractionation correction       — normalise to δ¹³C = −25 ‰
  4. Uncertainty propagation        — counting stats + normalisation scatter (in quadrature)
  5. Process-blank subtraction      — mass-balance (when samplemass_mg is available)
  6. Conventional age              — Libby factor 8033 y

Entry points
  reduce_wheel(wheel_id, user='') → WheelReductionResult
  reduce_run(run_id,   user='') → list[WheelReductionResult]

After reduce_run / reduce_wheel the results are written to ams.amsresult and the
run status is advanced to 1 (Reduced).

References
  Stuiver & Polach (1977) Radiocarbon 19:355–363
  Donahue et al.  (1990) Radiocarbon 32:135–142
  McNichol et al. (1992) Radiocarbon 34:194–204
"""
from __future__ import annotations

import logging
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from db_core import db_manager
from sqlalchemy import text

log = logging.getLogger(__name__)

# ── Physical constants ─────────────────────────────────────────────────────────

FM_OXI        = 1.0398      # Fraction modern of OXI (SRM 4990B)
FM_OXII       = 1.3407      # Fraction modern of OXII (SRM 4990C)
R13_VPDB      = 0.0112372   # ¹³C/¹²C ratio of VPDB
LIBBY_FACTOR  = 8033.0      # −1 / ln(½) × 5568 y  (conventional age factor)

# Min number of OXI standards required on a wheel for reliable normalisation
MIN_OXI_TARGETS = 2


# ── Data structures ────────────────────────────────────────────────────────────

@dataclass
class _TargetRow:
    """One row fetched from the DB for reduction."""
    amstargetid:     int
    wheelposition:   int
    targetlabel:     str
    targettype:      str
    samplemass_mg:   Optional[float]
    samplevolume_ml: Optional[float]   # GIS gas targets only
    # raw aggregate values (from ams.amsresult, populated at import time)
    r14_12:          Optional[float]
    err14_12:        Optional[float]
    r13_12:          Optional[float]
    err13_12:        Optional[float]


@dataclass
class TargetResult:
    """Reduction output for a single target — maps 1-to-1 onto ams.amsresult."""
    amstargetid:      int
    targettype:       str
    targetlabel:      str

    delta13c_permil:  Optional[float] = None

    fm_raw:           Optional[float] = None   # normalised, pre-δ¹³C correction
    fm_raw_err:       Optional[float] = None

    fm_corrected:     Optional[float] = None   # after fractionation correction
    fm_corrected_err: Optional[float] = None

    pmc:              Optional[float] = None
    congage_bp:       Optional[float] = None
    congage_err:      Optional[float] = None

    nstd_used:        int  = 0
    nblank_used:      int  = 0
    isaccepted:       bool = True
    rejectreason:     Optional[str] = None

    # Informational
    blank_corrected:  bool = False


@dataclass
class WheelReductionResult:
    wheel_id:       int
    wheel_label:    str
    n_oxi:          int
    n_oxii:         int
    n_unknown:      int
    n_blank:        int
    norm_r14:       float           # OXI mean R14/12 used for normalisation
    norm_r14_err:   float
    norm_r13:       float           # OXI mean R13/12
    oxii_fm_check:  Optional[float] # reduced OXII Fm (should ≈ FM_OXII = 1.3407)
    blank_fm:       Optional[float] # mean reduced process-blank Fm
    targets:        list[TargetResult] = field(default_factory=list)
    warnings:       list[str]         = field(default_factory=list)


@dataclass
class RunReductionResult:
    run_id:   int
    wheels:   list[WheelReductionResult] = field(default_factory=list)
    errors:   list[str]                  = field(default_factory=list)


# ── Public API ─────────────────────────────────────────────────────────────────

def reduce_run(run_id: int, user: str = "") -> RunReductionResult:
    """Reduce all wheels of a run.  Writes results to DB; advances runstatus → 1."""
    result = RunReductionResult(run_id=run_id)

    try:
        with db_manager.get_connection() as conn:
            wheel_rows = conn.execute(text("""
                SELECT amswheelid, wheellabel
                FROM ams.amswheel
                WHERE amsrunid = :rid
                ORDER BY wheelnumber
            """), {"rid": run_id}).fetchall()
    except Exception as exc:
        result.errors.append(f"Could not fetch wheels: {exc}")
        return result

    if not wheel_rows:
        result.errors.append("No wheels found for this run.")
        return result

    for wrow in wheel_rows:
        try:
            wr = reduce_wheel(wrow.amswheelid, user=user)
            result.wheels.append(wr)
        except Exception as exc:
            result.errors.append(f"Wheel {wrow.amswheelid} ({wrow.wheellabel}): {exc}")
            log.error("reduce_wheel %d: %s", wrow.amswheelid, exc, exc_info=True)

    # Advance run status to 1 (Reduced) if at least one wheel succeeded
    if result.wheels:
        try:
            with db_manager.get_connection() as conn:
                conn.execute(text("""
                    UPDATE ams.amsrun
                    SET runstatus = 1,
                        modifdatestamp = :now,
                        modifuserstamp = :usr
                    WHERE amsrunid = :rid
                      AND runstatus < 1
                """), {"rid": run_id, "now": datetime.now(), "usr": user or "system"})
                conn.commit()
        except Exception as exc:
            result.errors.append(f"Could not update run status: {exc}")

    return result


def reduce_wheel(wheel_id: int, user: str = "") -> WheelReductionResult:
    """Reduce all targets on one wheel.  Writes results to ams.amsresult."""
    targets = _fetch_targets(wheel_id)
    if not targets:
        raise ValueError(f"No targets found for wheel {wheel_id}.")

    wheel_label = _fetch_wheel_label(wheel_id)

    # ── Split by type ──────────────────────────────────────────────────────────
    oxi_rows        = [t for t in targets if t.targettype == "OXI"]
    oxii_rows       = [t for t in targets if t.targettype == "OXII"]
    blank_rows      = [t for t in targets if "blank" in t.targettype]
    unk_rows        = [t for t in targets if t.targettype == "unknown"]
    sec_rows        = [t for t in targets if t.targettype == "secondary_std"]
    gas_unk_rows    = [t for t in targets if t.targettype == "gas_unknown"]
    gas_blank_rows  = [t for t in targets if t.targettype == "gas_blank"]
    solid_blank_rows = [t for t in blank_rows
                        if t.targettype in ("process_blank", "graphite_blank")]

    warnings: list[str] = []

    # ── Step 1: OXI normalisation ──────────────────────────────────────────────
    norm_rows = oxi_rows if oxi_rows else oxii_rows
    norm_fm   = FM_OXI   if oxi_rows else FM_OXII
    norm_label = "OXI"   if oxi_rows else "OXII"

    if len(norm_rows) < MIN_OXI_TARGETS:
        warnings.append(
            f"Only {len(norm_rows)} {norm_label} target(s) found "
            f"(minimum recommended: {MIN_OXI_TARGETS}). Normalisation may be unreliable."
        )
    if not norm_rows:
        raise ValueError("No OXI or OXII standard targets on this wheel; cannot normalise.")

    norm_r14, norm_r14_err, norm_r13, norm_r13_err = _standard_mean(norm_rows)

    # ── Step 2 & 3: Reduce every target ───────────────────────────────────────
    results: list[TargetResult] = []
    n_std = len(norm_rows)

    for t in targets:
        res = _reduce_one(t, norm_r14, norm_r14_err, norm_r13, norm_fm, n_std)
        results.append(res)

    # ── Step 4: OXII check (if OXI was the primary standard) ──────────────────
    oxii_fm_check: Optional[float] = None
    if oxi_rows and oxii_rows:
        oxii_res = [r for r in results if r.targettype == "OXII"]
        if oxii_res:
            fms = [r.fm_corrected for r in oxii_res if r.fm_corrected is not None]
            if fms:
                oxii_fm_check = sum(fms) / len(fms)
                deviation = abs(oxii_fm_check - FM_OXII) / FM_OXII
                if deviation > 0.01:  # > 1 % deviation
                    warnings.append(
                        f"OXII check standard mean Fm = {oxii_fm_check:.4f} "
                        f"(expected {FM_OXII:.4f}, deviation {deviation*100:.2f} %)."
                    )

    # ── Step 5a: Solid blank characterisation ─────────────────────────────────
    blank_fm:       Optional[float] = None
    blank_fm_err:   Optional[float] = None
    blank_mass_mg:  Optional[float] = None

    solid_blank_results = [r for r in results
                           if r.targettype in ("process_blank", "graphite_blank")
                           and r.fm_corrected is not None]
    if solid_blank_results:
        fms  = [r.fm_corrected     for r in solid_blank_results]
        errs = [r.fm_corrected_err for r in solid_blank_results
                if r.fm_corrected_err is not None]
        blank_fm      = sum(fms) / len(fms)
        blank_fm_err  = (statistics.stdev(fms) if len(fms) > 1
                         else (errs[0] if errs else None))
        blank_masses = [_mass(t) for t in solid_blank_rows if _mass(t) is not None]
        if blank_masses:
            blank_mass_mg = sum(blank_masses) / len(blank_masses)

    # ── Step 5b: Gas blank characterisation ───────────────────────────────────
    gas_blank_fm:      Optional[float] = None
    gas_blank_fm_err:  Optional[float] = None
    gas_blank_vol_ml:  Optional[float] = None

    gas_blank_results = [r for r in results
                         if r.targettype == "gas_blank"
                         and r.fm_corrected is not None]
    if gas_blank_results:
        gfms  = [r.fm_corrected     for r in gas_blank_results]
        gerrs = [r.fm_corrected_err for r in gas_blank_results
                 if r.fm_corrected_err is not None]
        gas_blank_fm     = sum(gfms) / len(gfms)
        gas_blank_fm_err = (statistics.stdev(gfms) if len(gfms) > 1
                            else (gerrs[0] if gerrs else None))
        blank_vols = [t.samplevolume_ml for t in gas_blank_rows
                      if t.samplevolume_ml is not None]
        if blank_vols:
            gas_blank_vol_ml = sum(blank_vols) / len(blank_vols)

    # ── Step 6a: Blank subtraction for solid unknowns ─────────────────────────
    if blank_fm is not None and blank_mass_mg is not None:
        for r in results:
            if r.targettype != "unknown":
                continue
            t = next(x for x in targets if x.amstargetid == r.amstargetid)
            m = _mass(t)
            if m is None or m <= blank_mass_mg:
                continue
            r, did_bc = _blank_subtract(r, blank_fm, blank_fm_err, blank_mass_mg, m)
            if did_bc:
                r.nblank_used = len(solid_blank_results)
                r.blank_corrected = True
                if r.fm_corrected is not None and r.fm_corrected > 0:
                    r.pmc       = r.fm_corrected * 100.0
                    r.congage_bp, r.congage_err = _conventional_age(
                        r.fm_corrected, r.fm_corrected_err
                    )

    # ── Step 6b: Volume-based blank subtraction for gas unknowns ──────────────
    if gas_blank_fm is not None and gas_blank_vol_ml is not None:
        for r in results:
            if r.targettype != "gas_unknown":
                continue
            t = next(x for x in targets if x.amstargetid == r.amstargetid)
            v = t.samplevolume_ml
            if v is None or v <= gas_blank_vol_ml:
                continue
            r, did_bc = _blank_subtract(
                r, gas_blank_fm, gas_blank_fm_err, gas_blank_vol_ml, v
            )
            if did_bc:
                r.nblank_used = len(gas_blank_results)
                r.blank_corrected = True
                if r.fm_corrected is not None and r.fm_corrected > 0:
                    r.pmc       = r.fm_corrected * 100.0
                    r.congage_bp, r.congage_err = _conventional_age(
                        r.fm_corrected, r.fm_corrected_err
                    )

    # ── Persist ────────────────────────────────────────────────────────────────
    _save_results(results, user=user)

    return WheelReductionResult(
        wheel_id      = wheel_id,
        wheel_label   = wheel_label,
        n_oxi         = len(oxi_rows),
        n_oxii        = len(oxii_rows),
        n_unknown     = len(unk_rows),
        n_blank       = len(blank_rows),
        norm_r14      = norm_r14,
        norm_r14_err  = norm_r14_err,
        norm_r13      = norm_r13,
        oxii_fm_check = oxii_fm_check,
        blank_fm      = blank_fm,
        targets       = results,
        warnings      = warnings,
    )


# ── Internal helpers ───────────────────────────────────────────────────────────

def _fetch_wheel_label(wheel_id: int) -> str:
    with db_manager.get_connection() as conn:
        row = conn.execute(text(
            "SELECT wheellabel FROM ams.amswheel WHERE amswheelid = :wid"
        ), {"wid": wheel_id}).fetchone()
    return row.wheellabel if row else str(wheel_id)


def _fetch_targets(wheel_id: int) -> list[_TargetRow]:
    with db_manager.get_connection() as conn:
        rows = conn.execute(text("""
            SELECT t.amstargetid, t.wheelposition, t.targetlabel, t.targettype,
                   t.samplemass_mg, t.samplevolume_ml,
                   res.rawratio14_12      AS r14_12,
                   res.rawratio14_12_err  AS err14_12,
                   res.ratio13_12         AS r13_12,
                   res.ratio13_12_err     AS err13_12
            FROM ams.amstarget  t
            JOIN ams.amsresult  res ON res.amstargetid = t.amstargetid
            WHERE t.amswheelid = :wid
            ORDER BY t.wheelposition
        """), {"wid": wheel_id}).fetchall()

    result = []
    for r in rows:
        if r.r14_12 is None or r.r13_12 is None:
            log.warning("Target %d (%s) missing raw ratios — skipped.",
                        r.amstargetid, r.targetlabel)
            continue
        result.append(_TargetRow(
            amstargetid     = r.amstargetid,
            wheelposition   = r.wheelposition,
            targetlabel     = r.targetlabel,
            targettype      = r.targettype,
            samplemass_mg   = r.samplemass_mg,
            samplevolume_ml = r.samplevolume_ml,
            r14_12          = float(r.r14_12),
            err14_12        = float(r.err14_12) if r.err14_12 is not None else None,
            r13_12          = float(r.r13_12),
            err13_12        = float(r.err13_12) if r.err13_12 is not None else None,
        ))
    return result


def _standard_mean(rows: list[_TargetRow]) -> tuple[float, float, float, float]:
    """Return (mean_R14, err_R14, mean_R13, err_R13) for a list of standard targets.
    The reported error is the larger of the internal (counting-stats) mean and
    the external (inter-standard scatter) standard error."""
    r14s = [t.r14_12 for t in rows]
    r13s = [t.r13_12 for t in rows]
    n    = len(rows)

    mean_r14 = sum(r14s) / n
    mean_r13 = sum(r13s) / n

    # External uncertainty (inter-standard scatter)
    ext_r14 = (statistics.stdev(r14s) / math.sqrt(n)) if n > 1 else 0.0
    ext_r13 = (statistics.stdev(r13s) / math.sqrt(n)) if n > 1 else 0.0

    # Internal uncertainty (mean of individual counting errors)
    int_errs_14 = [t.err14_12 for t in rows if t.err14_12 is not None]
    int_errs_13 = [t.err13_12 for t in rows if t.err13_12 is not None]
    int_r14 = (sum(int_errs_14) / n / math.sqrt(n)) if int_errs_14 else 0.0
    int_r13 = (sum(int_errs_13) / n / math.sqrt(n)) if int_errs_13 else 0.0

    err_r14 = max(ext_r14, int_r14)
    err_r13 = max(ext_r13, int_r13)

    return mean_r14, err_r14, mean_r13, err_r13


def _delta13c(r13_12: float) -> float:
    """δ¹³C in ‰ vs VPDB from measured ¹³C/¹²C."""
    return (r13_12 / R13_VPDB - 1.0) * 1000.0


def _reduce_one(t: _TargetRow,
                norm_r14: float, norm_r14_err: float,
                norm_r13: float, norm_fm: float,
                n_std: int) -> TargetResult:
    """Apply OXI normalisation + fractionation correction to one target."""
    res = TargetResult(
        amstargetid = t.amstargetid,
        targettype  = t.targettype,
        targetlabel = t.targetlabel,
        nstd_used   = n_std,
    )

    # δ¹³C from AMS beam measurement (not corrected to −25 ‰)
    res.delta13c_permil = _delta13c(t.r13_12)

    # ── OXI normalisation ──────────────────────────────────────────────────────
    # Fm_raw = (R14_sample / R14_OXI_mean) × Fm_OXI_consensus
    fm_raw = (t.r14_12 / norm_r14) * norm_fm

    # Relative σ: quadrature of sample counting + OXI normalisation scatter
    rel_r14_sample = (t.err14_12 / t.r14_12) if t.err14_12 else 0.0
    rel_r14_norm   = norm_r14_err / norm_r14
    fm_raw_err     = fm_raw * math.sqrt(rel_r14_sample ** 2 + rel_r14_norm ** 2)

    res.fm_raw     = fm_raw
    res.fm_raw_err = fm_raw_err

    # ── Fractionation correction ───────────────────────────────────────────────
    # Fm_corrected = Fm_raw × (R13_OXI_mean / R13_sample)²
    #
    # This normalises both sample and standard to the same reference δ¹³C so
    # that systematic ion-source fractionation cancels exactly.
    frac = (norm_r13 / t.r13_12) ** 2
    fm_corr = fm_raw * frac

    # Additional σ from ¹³C measurement (factor of 2 on each relative error)
    rel_r13_sample = (t.err13_12 / t.r13_12 * 2.0) if t.err13_12 else 0.0
    # norm_r13 uncertainty (from standard mean) — already well-determined
    rel_r13_norm   = 0.0   # dominated by counting stats absorbed in norm_r14_err

    fm_corr_err = fm_corr * math.sqrt(
        (fm_raw_err / fm_raw) ** 2 +
        rel_r13_sample ** 2 +
        rel_r13_norm ** 2
    )

    res.fm_corrected     = fm_corr
    res.fm_corrected_err = fm_corr_err

    # ── Derived quantities ─────────────────────────────────────────────────────
    res.pmc = fm_corr * 100.0
    res.congage_bp, res.congage_err = _conventional_age(fm_corr, fm_corr_err)

    return res


def _conventional_age(fm: float,
                       fm_err: Optional[float]) -> tuple[Optional[float], Optional[float]]:
    """Return (age_BP, age_err) using Libby factor 8033 y.  Returns (None, None) for Fm ≤ 0."""
    if fm is None or fm <= 0:
        return None, None
    age = -LIBBY_FACTOR * math.log(fm)
    err = (LIBBY_FACTOR * fm_err / fm) if fm_err else None
    return age, err


def _mass(t: _TargetRow) -> Optional[float]:
    return t.samplemass_mg

def _volume(t: _TargetRow) -> Optional[float]:
    return t.samplevolume_ml


def _blank_subtract(
    res: TargetResult,
    blank_fm: float,
    blank_fm_err: Optional[float],
    blank_mass_mg: float,
    sample_mass_mg: float,
) -> tuple[TargetResult, bool]:
    """Apply mass-balance blank subtraction to an unknown result (in-place).
    Returns (result, did_apply).  Skips if Fm < blank Fm (already cleaner than blank)."""
    if res.fm_corrected is None:
        return res, False
    if res.fm_corrected <= blank_fm:
        return res, False   # sample is already at or below blank level

    m_s = sample_mass_mg
    m_b = blank_mass_mg
    denom = m_s - m_b

    fm_bc = (m_s * res.fm_corrected - m_b * blank_fm) / denom

    # Uncertainty propagation
    if res.fm_corrected_err is not None and blank_fm_err is not None:
        err_bc = math.sqrt(
            (m_s / denom * res.fm_corrected_err) ** 2 +
            (m_b / denom * blank_fm_err) ** 2
        )
    else:
        err_bc = res.fm_corrected_err   # unchanged if errors unavailable

    res.fm_corrected     = fm_bc
    res.fm_corrected_err = err_bc
    res.pmc              = fm_bc * 100.0
    return res, True


def _save_results(results: list[TargetResult], user: str = ""):
    """Upsert all TargetResult rows into ams.amsresult."""
    now = datetime.now()
    usr = user or "system"

    with db_manager.get_connection() as conn:
        for r in results:
            conn.execute(text("""
                UPDATE ams.amsresult SET
                    delta13c_permil      = :d13c,
                    fm_raw               = :fmr,
                    fm_raw_err           = :fmre,
                    fm_corrected         = :fmc,
                    fm_corrected_err     = :fmce,
                    pmc                  = :pmc,
                    congage_bp           = :age,
                    congage_err          = :age_err,
                    nstd_used            = :nstd,
                    nblank_used          = :nblank,
                    isaccepted           = :ok,
                    rejectreason         = :reason,
                    reducedat            = :now,
                    reducedby            = :usr
                WHERE amstargetid = :tid
            """), {
                "d13c":   r.delta13c_permil,
                "fmr":    r.fm_raw,
                "fmre":   r.fm_raw_err,
                "fmc":    r.fm_corrected,
                "fmce":   r.fm_corrected_err,
                "pmc":    r.pmc,
                "age":    r.congage_bp,
                "age_err":r.congage_err,
                "nstd":   r.nstd_used,
                "nblank": r.nblank_used,
                "ok":     r.isaccepted,
                "reason": r.rejectreason,
                "now":    now,
                "usr":    usr,
                "tid":    r.amstargetid,
            })
        conn.commit()

    log.info("Saved %d reduction results (user=%s)", len(results), usr)
