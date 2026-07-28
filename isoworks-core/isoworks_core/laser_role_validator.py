"""
laser_role_validator.py
=======================
Validates sample-role assignments for Laser (LGR / Picarro) post-processing.

Role IDENTIFICATION is handled entirely by database.get_batch_data(), which:
  - reads role_code from LIMS (DRFTC, AMLIN, SCTRL, IHSTD, MEM)
  - applies scientific heuristics to select the best memory pair,
    calibration anchors, drift standard, and validation control
  - writes the results into is_* flag columns of standards_df

This module's sole responsibility is to CHECK the resulting assignments
against the five scientific rules and return structured warnings/errors
that the GUI can present before spawning the processing Worker thread.

Rules validated
---------------
LINEARITY    Only meaningful for LGR.  Assigned sample must have been
             measured at genuinely varying injection amounts
             (water_conc CV >= 10 %).

DRIFT        Assigned standard must appear >= 2 times across the run,
             span >= 70 % of total run duration, with no single gap
             > 50 % of run duration.

MEMORY       Requires >= 2 assigned standards.  They must be consecutive
             in the injection sequence (gap <= 1 slot), appear near the
             start of the run (first 40 %), and span >= MIN_MEMORY_DELTA  permil.

CALIBRATION  Requires >= 2 standards.  Their certified d values must span
             >= MIN_CAL_SPAN  permil.  At least one must appear in the first
             third of injections.

VALIDATION   Assigned control must not share a sample_id with any
             correction role.

Usage
-----
    from laser_role_validator import validate_laser_roles, Severity, format_issues_for_dialog

    roles  = self.get_roles_from_table()   # reads is_* checkboxes from GUI
    issues = validate_laser_roles(
        roles              = roles,
        injection_df       = self.data,
        standards_df       = self.standards_data,
        correction_methods = {
            "linearity": self._get_combo_text("linearity_combo", "None"),
            "drift":     self._get_combo_text("drift_combo",     "None"),
            "memory":    self._get_combo_text("memory_combo",    "None"),
        },
        instrument = instrument_label,
        isotope    = isotopes_to_process0,
    )
    errors   = [i for i in issues if i.severity == Severity.ERROR]
    warnings = [i for i in issues if i.severity == Severity.WARNING]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

import numpy as np
import pandas as pd


# ------------------------------------------------------------------------------
# PUBLIC TYPES
# ------------------------------------------------------------------------------

class Severity(Enum):
    INFO    = "INFO"
    WARNING = "WARNING"
    ERROR   = "ERROR"


@dataclass
class ValidationIssue:
    severity: Severity
    role:     str
    message:  str
    detail:   str = ""

    def __str__(self):
        icon = {"INFO": "i", "WARNING": "[!]", "ERROR": "X"}[self.severity.value]
        body = f"{icon} [{self.role.upper()}]  {self.message}"
        if self.detail:
            body += f"\n    -> {self.detail}"
        return body


# ------------------------------------------------------------------------------
# TUNABLE THRESHOLDS  (mirror the values used in database.get_batch_data)
# ------------------------------------------------------------------------------

AMOUNT_CV_MIN          = 0.05   # linearity: min CV of water_conc across injections
MIN_DRIFT_SPAN_FRAC    = 0.70   # drift must span >= 70 % of run
MAX_DRIFT_GAP_FRAC     = 0.50   # largest single gap <= 50 % of run
MIN_DRIFT_COUNT        = 2      # drift standard must appear >= 2 x
MIN_MEMORY_DELTA       = 5.0    #  permil  minimum |Deltad| between assigned memory standards
MAX_MEMORY_START_FRAC  = 0.40   # memory pair must start in first 40 % of injections
MAX_CONSECUTIVE_GAP    = 1      # injection slots allowed between memory pair members
MIN_CAL_SPAN           = 5.0    #  permil  min span between calibration anchors
MIN_CAL_COUNT          = 2
MAX_CAL_FIRST_FRAC     = 0.33   # first cal standard must be in first 33 % of injections


# ------------------------------------------------------------------------------
# MAIN ENTRY POINT
# ------------------------------------------------------------------------------

def validate_laser_roles(
    roles:              dict,
    injection_df:       "pd.DataFrame | None",
    standards_df:       "pd.DataFrame | None",
    correction_methods: dict,
    instrument:         str = "Picarro",
    isotope:            str = "d18O",
) -> list[ValidationIssue]:
    """
    Validate the role assignments already present in `roles` (from
    get_roles_from_table() or _infer_roles_from_standards()) and return
    a sorted list of ValidationIssues.

    Parameters
    ----------
    roles              : dict with keys "linearity" (str|None), "drift" (str|None),
                         "memory" (list), "calibration" (list), "validation" (list)
    injection_df       : self.data  -- injection-level DataFrame
                         expected columns: sample_id, timestamp, injection_no,
                                           water_conc, <isotope>
    standards_df       : self.standards_data  -- columns: sample_id, <isotope>_true
    correction_methods : dict with "linearity", "drift", "memory" keys ->
                         combo-box strings (e.g. "Linear", "None")
    instrument         : "LGR" or "Picarro"
    isotope            : primary isotope base name (e.g. "d18O")
    """
    r = {
        "linearity":   roles.get("linearity"),
        "drift":       roles.get("drift"),
        "memory":      list(roles.get("memory",      [])),
        "calibration": list(roles.get("calibration", [])),
        "validation":  list(roles.get("validation",  [])),
    }
    active = {k: _is_active(correction_methods.get(k, "None"))
              for k in ("linearity", "drift", "memory")}

    inj  = injection_df.copy()  if isinstance(injection_df,  pd.DataFrame) else pd.DataFrame()
    stds = standards_df.copy()  if isinstance(standards_df,  pd.DataFrame) else pd.DataFrame()

    issues: list[ValidationIssue] = []
    issues += _check_linearity  (r, inj,  active, instrument)
    issues += _check_drift      (r, inj,  active)
    issues += _check_memory     (r, inj,  stds, active, isotope)
    issues += _check_calibration(r, inj,  stds, f"{isotope}_true")
    issues += _check_validation (r)
    issues += _check_overlap    (r)

    _order = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}
    issues.sort(key=lambda i: _order[i.severity])
    for iss in issues:
        (logging.warning if iss.severity != Severity.INFO else logging.info)(
            f"RoleValidation {iss}")
    return issues


# ------------------------------------------------------------------------------
# RULE IMPLEMENTATIONS
# ------------------------------------------------------------------------------

def _check_linearity(r, inj, active, instrument) -> list[ValidationIssue]:
    issues = []
    if not active["linearity"]:
        return issues
    if instrument.upper() != "LGR":
        issues.append(ValidationIssue(
            Severity.WARNING, "linearity",
            "Linearity correction is currently only validated for LGR instruments.",
            f"Selected instrument: {instrument}.",
        ))
    sid = r["linearity"]
    if not sid:
        issues.append(ValidationIssue(
            Severity.ERROR, "linearity",
            "Linearity correction is enabled but no linearity sample (AMLIN) was found "
            "in the LIMS loadlist or standards file.",
        ))
        return issues
    
    # Check Amount column (injection amount variation) - preferred for LGR
    if inj.empty:
        return issues
    
    amount_col = None
    if "Amount" in inj.columns:
        amount_col = "Amount"
    # elif "water_conc" in inj.columns:
    #     amount_col = "water_conc"
    else:
        return issues
    
    rows = inj[inj["sample_id"] == sid]
    if rows.empty:
        issues.append(ValidationIssue(Severity.ERROR, "linearity",
            f"Linearity sample '{sid}' not found in injection data."))
        return issues
    
    amounts = pd.to_numeric(rows[amount_col], errors="coerce").dropna()
    if len(amounts) < 3:
        issues.append(ValidationIssue(Severity.WARNING, "linearity",
            f"Linearity sample '{sid}' has only {len(amounts)} {amount_col} readings (min 3 needed)."))
        return issues
    
    cv = amounts.std(ddof=1) / amounts.mean() if amounts.mean() != 0 else 0.0
    if cv < AMOUNT_CV_MIN:
        issues.append(ValidationIssue(
            Severity.ERROR, "linearity",
            f"Linearity sample '{sid}' shows insufficient {amount_col} variation "
            f"(CV = {cv:.1%}, required >= {AMOUNT_CV_MIN:.0%}).  "
            "The sample must have been measured at genuinely varying injection amounts.",
            f"{amount_col} range: {amounts.min():.0f}-{amounts.max():.0f}",
        ))
    return issues


def _check_drift(r, inj, active) -> list[ValidationIssue]:
    issues = []
    if not active["drift"]:
        return issues
    sid = r["drift"]
    if not sid:
        issues.append(ValidationIssue(
            Severity.ERROR, "drift",
            "Drift correction is enabled but no drift standard (DRFTC / IHSTD) was "
            "identified.  A standard spanning the full run duration is required.",
        ))
        return issues
    if inj.empty or "timestamp" not in inj.columns:
        return issues
    all_ts  = pd.to_datetime(inj["timestamp"], errors="coerce").dropna()
    run_dur = (all_ts.max() - all_ts.min()).total_seconds()
    if run_dur <= 0:
        return issues
    drift_ts = (
        pd.to_datetime(inj.loc[inj["sample_id"] == sid, "timestamp"], errors="coerce")
        .dropna().sort_values()
    )
    if drift_ts.empty:
        issues.append(ValidationIssue(Severity.ERROR, "drift",
            f"Drift standard '{sid}' has no timestamp data in the injection file."))
        return issues
    if len(drift_ts) < MIN_DRIFT_COUNT:
        issues.append(ValidationIssue(Severity.WARNING, "drift",
            f"Drift standard '{sid}' appears only {len(drift_ts)}x in the run "
            f"(minimum {MIN_DRIFT_COUNT} required)."))
        return issues
    span_frac = (drift_ts.max() - drift_ts.min()).total_seconds() / run_dur
    if span_frac < MIN_DRIFT_SPAN_FRAC:
        issues.append(ValidationIssue(
            Severity.WARNING, "drift",
            f"Drift standard '{sid}' spans only {span_frac:.0%} of the run "
            f"(recommended >= {MIN_DRIFT_SPAN_FRAC:.0%}).  "
            "Correction beyond the last measurement will be extrapolated.",
            _ts_range(drift_ts, all_ts),
        ))
    if len(drift_ts) >= 2:
        gaps = np.diff(drift_ts.values).astype("timedelta64[s]").astype(float)
        mgf  = gaps.max() / run_dur
        if mgf > MAX_DRIFT_GAP_FRAC:
            issues.append(ValidationIssue(
                Severity.WARNING, "drift",
                f"Largest gap between drift measurements is {mgf:.0%} of the run "
                f"(recommended < {MAX_DRIFT_GAP_FRAC:.0%}).  "
                "Consider adding intermediate drift standards for better distribution.",
            ))
    return issues


def _check_memory(r, inj, stds, active, isotope) -> list[ValidationIssue]:
    issues = []
    if not active["memory"]:
        return issues
    mem_ids  = r["memory"]
    true_col = f"{isotope}_true"
    if not mem_ids:
        issues.append(ValidationIssue(Severity.ERROR, "memory",
            "Memory correction is enabled but no memory standards were identified.  "
            "A consecutive pair of standards with large |Deltad| is required."))
        return issues
    if len(mem_ids) < 2:
        issues.append(ValidationIssue(Severity.ERROR, "memory",
            f"Only 1 memory standard found ({mem_ids[0]}).  "
            "At least 2 consecutive standards are needed for a memory fit."))
        return issues

    if not inj.empty and "injection_no" in inj.columns:
        total_inj = pd.to_numeric(inj["injection_no"], errors="coerce").max()
        first_pos = (
            inj[inj["sample_id"].isin(mem_ids)]
            .groupby("sample_id")["injection_no"].min()
            .pipe(lambda s: pd.to_numeric(s, errors="coerce")).dropna()
        )
        sorted_ids = first_pos.sort_values().index.tolist()
        sorted_pos = first_pos.loc[sorted_ids].values

        # Consecutiveness check
        if len(sorted_pos) >= 2:
            bad = [
                (sorted_ids[i], sorted_ids[i+1], int(np.diff(sorted_pos)[i]))
                for i in range(len(sorted_pos) - 1)
                if np.diff(sorted_pos)[i] > MAX_CONSECUTIVE_GAP + 1
            ]
            if bad:
                issues.append(ValidationIssue(
                    Severity.ERROR, "memory",
                    "Memory standards are not measured consecutively (must be back-to-back).",
                    ";  ".join(f"'{a}'->'{b}' gap={g}" for a, b, g in bad),
                ))

        # Start-of-run check
        if total_inj and total_inj > 0 and len(sorted_pos) > 0:
            ef = float(sorted_pos.min()) / float(total_inj)
            if ef > MAX_MEMORY_START_FRAC:
                issues.append(ValidationIssue(
                    Severity.WARNING, "memory",
                    f"Memory pair first appears at injection {int(sorted_pos.min())}/{int(total_inj)} "
                    f"({ef:.0%} into the run, recommended within first {MAX_MEMORY_START_FRAC:.0%}).",
                ))

    # Delta check
    if not stds.empty and true_col in stds.columns:
        mem_true = (
            stds[stds["sample_id"].isin(mem_ids)]
            .drop_duplicates("sample_id").set_index("sample_id")[true_col]
            .pipe(lambda s: pd.to_numeric(s, errors="coerce")).dropna()
        )
        if len(mem_true) >= 2:
            delta = mem_true.max() - mem_true.min()
            if delta < MIN_MEMORY_DELTA:
                issues.append(ValidationIssue(
                    Severity.ERROR, "memory",
                    f"Memory pair spans only {delta:.2f}  permil in {true_col} "
                    f"(minimum {MIN_MEMORY_DELTA}  permil).  "
                    "Choose standards with the largest isotopic contrast in the batch.",
                    ", ".join(f"{s}={v:.2f} permil" for s, v in mem_true.items()),
                ))
            # Cross-check: is a better pair available?
            all_true = (
                stds.drop_duplicates("sample_id").set_index("sample_id")[true_col]
                .pipe(lambda s: pd.to_numeric(s, errors="coerce")).dropna()
            )
            if len(all_true) >= 2 and (all_true.max() - all_true.min()) > delta + 2.0:
                issues.append(ValidationIssue(
                    Severity.WARNING, "memory",
                    f"A wider-contrast pair exists "
                    f"('{all_true.idxmin()}'/ '{all_true.idxmax()}', "
                    f"Delta = {all_true.max()-all_true.min():.2f}  permil vs {delta:.2f}  permil).  "
                    "Consider reassigning for a more reliable memory fit.",
                ))
        else:
            issues.append(ValidationIssue(Severity.INFO, "memory",
                f"{true_col} missing for some memory standards; |Deltad| cannot be verified."))
    return issues


def _check_calibration(r, inj, stds, true_col) -> list[ValidationIssue]:
    issues = []
    cal_ids = r["calibration"]
    if not cal_ids:
        issues.append(ValidationIssue(Severity.ERROR, "calibration",
            "No calibration standards were identified."))
        return issues
    if len(cal_ids) < MIN_CAL_COUNT:
        issues.append(ValidationIssue(Severity.ERROR, "calibration",
            f"Only 1 calibration standard found ({cal_ids[0]}).  "
            "Two-point calibration requires a HIGH and a LOW anchor."))
        return issues
    if not stds.empty and true_col in stds.columns:
        tv = (
            stds[stds["sample_id"].isin(cal_ids)]
            .drop_duplicates("sample_id").set_index("sample_id")[true_col]
            .pipe(lambda s: pd.to_numeric(s, errors="coerce")).dropna()
        )
        if len(tv) >= 2:
            span = tv.max() - tv.min()
            if span < MIN_CAL_SPAN:
                issues.append(ValidationIssue(
                    Severity.WARNING, "calibration",
                    f"Calibration standards span only {span:.2f}  permil in {true_col} "
                    f"(recommended >= {MIN_CAL_SPAN}  permil).  "
                    "A wider high–low bracket reduces calibration uncertainty.",
                    ", ".join(f"{s}={v:.2f} permil" for s, v in tv.items()),
                ))
    if not inj.empty and "injection_no" in inj.columns:
        total_inj = pd.to_numeric(inj["injection_no"], errors="coerce").max()
        cal_pos = (
            inj[inj["sample_id"].isin(cal_ids)]
            .groupby("sample_id")["injection_no"].min()
            .pipe(lambda s: pd.to_numeric(s, errors="coerce")).dropna()
        )
        if total_inj and total_inj > 0 and not cal_pos.empty:
            ef = cal_pos.min() / total_inj
            if ef > MAX_CAL_FIRST_FRAC:
                issues.append(ValidationIssue(
                    Severity.WARNING, "calibration",
                    f"First calibration standard appears at injection "
                    f"{int(cal_pos.min())}/{int(total_inj)} ({ef:.0%}).  "
                    f"At least one should be in the first {MAX_CAL_FIRST_FRAC:.0%} of injections.",
                ))
            if len(cal_ids) == MIN_CAL_COUNT and cal_pos.max() / total_inj < 0.66:
                issues.append(ValidationIssue(Severity.INFO, "calibration",
                    "Only the minimum number of calibration standards is assigned, "
                    "with none in the last third of the run.  "
                    "Additional bracketing standards are recommended."))
    return issues


def _check_validation(r) -> list[ValidationIssue]:
    if not r["validation"]:
        return [ValidationIssue(Severity.INFO, "validation",
            "No validation (control) samples assigned.  "
            "QC statistics will not be produced.")]
    return []


def _check_overlap(r) -> list[ValidationIssue]:
    issues = []
    correction_ids: set = set()
    for role in ("calibration", "memory", "linearity", "drift"):
        v = r[role]
        correction_ids.update(v if isinstance(v, list) else (v if v else []))
    for vid in r["validation"]:
        if vid in correction_ids:
            issues.append(ValidationIssue(
                Severity.WARNING, "validation",
                f"Sample '{vid}' is a validation (control) sample but also participates "
                "in a correction role.  Independent controls must not derive any correction.",
            ))
    for role in ("drift", "linearity"):
        sid = r[role]
        if not sid:
            continue
        overlaps = [k for k in ("calibration", "memory") if sid in r[k]]
        if overlaps:
            issues.append(ValidationIssue(Severity.INFO, role,
                f"Sample '{sid}' ({role}) also used for "
                + " and ".join(overlaps) + ".  Acceptable but verify it is intentional."))
    return issues


# ------------------------------------------------------------------------------
# HELPERS
# ------------------------------------------------------------------------------

def _is_active(method_text: str) -> bool:
    return (method_text or "None").strip().lower() not in ("none", "off", "")


def _ts_range(drift_ts, all_ts) -> str:
    fmt = "%H:%M"
    return (
        f"Drift: {drift_ts.min().strftime(fmt)}–{drift_ts.max().strftime(fmt)}  |  "
        f"Run: {all_ts.min().strftime(fmt)}–{all_ts.max().strftime(fmt)}"
    )


def format_issues_for_dialog(issues: list[ValidationIssue]) -> str:
    """Format issues into a human-readable string for QMessageBox."""
    if not issues:
        return "✔  All role assignment checks passed."
    lines = []
    for sev in (Severity.ERROR, Severity.WARNING, Severity.INFO):
        subset = [i for i in issues if i.severity == sev]
        if subset:
            lines += [f"{'-'*44}", f"  {sev.value}S", f"{'-'*44}"]
            for iss in subset:
                lines += [str(iss), ""]
    return "\n".join(lines).strip()