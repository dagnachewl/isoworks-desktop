"""
ngam_ms_processor.py
====================
Pure processing of Helix SFT parsed data against a load list.
No database or GUI dependencies.

Workflow
--------
1. Match PositionData entries to LoadListRecord entries by position number.
2. Classify each position (blank / standard / air_ref / sample / invalid).
3. Run outlier rejection per position on the 3He cycles.
4. Compute mean & SE for 3He and 4He (after outlier removal).
5. Compute background from blank positions.
6. Compute sensitivity from standard positions.
7. Compute net 3He and activity for sample positions.
"""
from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field
from typing import List, Optional

from ngam_ms_parser import HelixSFTRun, PositionData

log = logging.getLogger(__name__)


# ─────────────────────────────── input dataclasses ────────────────────────────

@dataclass
class LoadListRecord:
    """One row from ngam.ng3hesequenceloadlist joined with procedure info."""
    headerid: int
    positioninrun: int          # 1-based, matches PositionData.position_num
    analysisid: Optional[int]
    sampletype: int             # 0 = sample, non-zero = reference
    bisblank: bool
    bislinreference: bool
    bisreproreference: bool
    knownstdactivity: Optional[float]     # certified 3H activity in TU — NULL for ccSTP-calibrated stds
    knownstdactivityunc: Optional[float]
    isrejected: bool
    ingrowthid: Optional[int] = None     # FK → ngam.ng3heingrowthdata
    sampleamount: Optional[float] = None # ccSTP gas amount dispensed (ref/blank at creation; samples after save)
    knownstdactivityunitid: Optional[int] = None  # unit of knownstdactivity (from referencecontroldata); None = ccSTP


@dataclass
class ProcessingConfig:
    outlier_method: str = "nsigma"    # "none" | "nsigma" | "grubbs"
    nsigma_threshold: float = 2.5
    use_he3_only: bool = True         # if False, use 3He/4He ratio (future)


# ─────────────────────────────── result dataclasses ───────────────────────────

@dataclass
class CycleOutlierFlags:
    position_num: int
    flags: List[bool]   # True = outlier (reject this cycle)
    reason: str         # "nsigma 2.5" etc.


@dataclass
class PositionResult:
    position_num: int
    headerid: Optional[int]
    analysisid: Optional[int]
    description: str
    position_type: str           # "blank" | "standard" | "air_ref" | "sample" | "invalid"

    n_cycles_total: int
    n_cycles_used: int           # after outlier rejection

    mean_he4_v: Optional[float]
    se_he4_v: Optional[float]
    mean_he3_a: Optional[float]
    se_he3_a: Optional[float]

    # Filled after background step:
    bg_mean_a: Optional[float] = None
    bg_se_a: Optional[float] = None
    net_he3_a: Optional[float] = None
    net_he3_unc: Optional[float] = None   # sqrt(se_he3^2 + bg_se^2)

    # Filled after sensitivity step:
    sensitivity: Optional[float] = None
    sensitivity_unc: Optional[float] = None

    # Final result (unit-generic; unit stored separately on RunProcessingResult)
    activity: Optional[float] = None
    activity_unc: Optional[float] = None

    # Ingrowth correction (when ingrowth_days > 0 and water_mass_g > 0)
    ingrowth_days: Optional[float] = None
    water_mass_g: Optional[float] = None
    t_sampling_days: Optional[float] = None   # storage: sampling → extraction (for t_se correction)
    ingrowth_correction_factor: Optional[float] = None
    activity_corrected: Optional[float] = None
    activity_corrected_unc: Optional[float] = None

    isrejected: bool = False
    rejection_reason: str = ""


@dataclass
class RunProcessingResult:
    run_id: Optional[int]
    n_blanks: int
    n_standards: int
    n_samples: int
    bg_mean_a: float          # mean of blank 3He signals
    bg_se_a: float            # SE of blank means
    sensitivity: float        # mean A per unit (TU or ccSTP, depending on standard type)
    sensitivity_unc: float    # SE across standards
    positions: List[PositionResult]
    unitid: Optional[int] = None  # FK → public.measurementunit; None = unresolved (ccSTP run, caller must set)


# ─────────────────────────────── helpers ──────────────────────────────────────

def _mean(vals: List[float]) -> float:
    finite = [v for v in vals if not math.isnan(v)]
    return sum(finite) / len(finite) if finite else float("nan")


def _std(vals: List[float]) -> float:
    finite = [v for v in vals if not math.isnan(v)]
    n = len(finite)
    if n < 2:
        return float("nan")
    mu = sum(finite) / n
    return math.sqrt(sum((v - mu) ** 2 for v in finite) / (n - 1))


def _se(vals: List[float]) -> float:
    finite = [v for v in vals if not math.isnan(v)]
    n = len(finite)
    if n < 2:
        return float("nan")
    return _std(vals) / math.sqrt(n)


def _weighted_mean_se(vals: List[float]) -> tuple:
    """Return (mean, se) of a list, ignoring NaN."""
    finite = [v for v in vals if not math.isnan(v)]
    if not finite:
        return float("nan"), float("nan")
    mu = sum(finite) / len(finite)
    se = _std(finite) / math.sqrt(len(finite)) if len(finite) > 1 else float("nan")
    return mu, se


# ─────────────────────────────── outlier rejection ────────────────────────────

def _compute_outlier_flags(
    he3_vals: List[float],
    config: ProcessingConfig,
) -> CycleOutlierFlags:
    """Return CycleOutlierFlags for one position's 3He cycle values."""
    n = len(he3_vals)
    flags = [False] * n

    if config.outlier_method == "none" or n < 3:
        return CycleOutlierFlags(
            position_num=0,
            flags=flags,
            reason="none",
        )

    finite = [v for v in he3_vals if not math.isnan(v)]
    if len(finite) < 3:
        return CycleOutlierFlags(position_num=0, flags=flags, reason="insufficient data")

    if config.outlier_method == "nsigma":
        mu = _mean(he3_vals)
        sd = _std(he3_vals)
        if math.isnan(mu) or math.isnan(sd) or sd == 0.0:
            return CycleOutlierFlags(
                position_num=0,
                flags=flags,
                reason=f"nsigma {config.nsigma_threshold}",
            )
        threshold = config.nsigma_threshold * sd
        for i, v in enumerate(he3_vals):
            if math.isnan(v):
                flags[i] = True  # treat NaN as outlier
            elif abs(v - mu) > threshold:
                flags[i] = True
        return CycleOutlierFlags(
            position_num=0,
            flags=flags,
            reason=f"nsigma {config.nsigma_threshold}",
        )

    # Fallback for unknown methods
    return CycleOutlierFlags(position_num=0, flags=flags, reason="none")


def _apply_flags(vals: List[float], flags: List[bool]) -> List[float]:
    """Return only the non-flagged, non-NaN values."""
    return [v for v, f in zip(vals, flags) if not f and not math.isnan(v)]


# ─────────────────────────────── type classification ──────────────────────────

def _classify_position(
    pos: PositionData,
    loadrec: Optional[LoadListRecord],
) -> str:
    """
    Return one of: "blank", "standard", "air_ref", "sample", "invalid".

    Classification uses explicit load list flags — never infers type from the
    presence or value of knownstdactivity, because that field may be NULL for
    ccSTP-calibrated gas-spike standards while still being a valid standard.
    """
    if not pos.is_valid:
        return "invalid"

    if loadrec is not None:
        if loadrec.bisblank:
            return "blank"
        # bisreproreference / bislinreference are the authoritative "standard" flags;
        # the calibration value may be in TU (knownstdactivity) or ccSTP (sampleamount).
        if loadrec.bisreproreference or loadrec.bislinreference:
            return "standard"
        if "air" in pos.description.lower() or loadrec.sampletype != 0:
            return "air_ref"
        return "sample"

    # No load list record: use description heuristics
    desc_lower = pos.description.lower()
    if "blank" in desc_lower:
        return "blank"
    return "sample"


# ─────────────────────────────── main function ────────────────────────────────

def process_run(
    helix_run: HelixSFTRun,
    load_list: List[LoadListRecord],
    config: ProcessingConfig = None,
    run_id: Optional[int] = None,
    ingrowth_data: Optional[dict] = None,
) -> RunProcessingResult:
    """
    Process a parsed Helix SFT run against a load list.

    Parameters
    ----------
    helix_run : HelixSFTRun
        Output of parse_helix_sft().
    load_list : list of LoadListRecord
        Load list rows from the database.
    config : ProcessingConfig, optional
        Outlier and processing settings (defaults to ProcessingConfig()).
    run_id : int, optional
        Database run ID for tagging the result.
    ingrowth_data : dict, optional
        ``{position_num: IngrowthInfo}`` for ingrowth decay correction.

    Returns
    -------
    RunProcessingResult
    """
    if config is None:
        config = ProcessingConfig()

    # Index load list by positioninrun for O(1) lookup
    ll_by_pos = {rec.positioninrun: rec for rec in load_list}

    position_results: List[PositionResult] = []

    # ── Step 1 & 2: match, classify, outlier-reject, compute stats ────────────
    for pos in helix_run.positions:
        loadrec = ll_by_pos.get(pos.position_num)
        ptype = _classify_position(pos, loadrec)

        if ptype == "invalid":
            position_results.append(
                PositionResult(
                    position_num=pos.position_num,
                    headerid=loadrec.headerid if loadrec else None,
                    analysisid=loadrec.analysisid if loadrec else None,
                    description=pos.description,
                    position_type="invalid",
                    n_cycles_total=helix_run.n_cycles,
                    n_cycles_used=0,
                    mean_he4_v=None,
                    se_he4_v=None,
                    mean_he3_a=None,
                    se_he3_a=None,
                    isrejected=True,
                    rejection_reason="invalid position (empty description)",

                )
            )
            continue

        # Outlier flags on 3He
        flags_obj = _compute_outlier_flags(pos.he3_y, config)
        flags_obj.position_num = pos.position_num
        flags = flags_obj.flags

        good_he3 = _apply_flags(pos.he3_y, flags)
        good_he4 = _apply_flags(pos.he4_y, flags)

        n_used = len(good_he3)

        m_he3 = _mean(good_he3) if good_he3 else None
        s_he3 = _se(good_he3) if len(good_he3) > 1 else None
        m_he4 = _mean(good_he4) if good_he4 else None
        s_he4 = _se(good_he4) if len(good_he4) > 1 else None

        is_rejected = loadrec.isrejected if loadrec else False
        rej_reason = "marked rejected in load list" if is_rejected else ""

        # Attach ingrowth data if available
        ig_days = None
        ig_water = None
        ig_tse = None
        if ingrowth_data:
            info = ingrowth_data.get(pos.position_num)
            if info is not None:
                ig_days  = info.t_ingrowth_seconds / 86400.0
                ig_water = info.water_mass_after_g
                ig_tse   = info.t_sampling_seconds / 86400.0 if info.t_sampling_seconds > 0 else None

        position_results.append(
            PositionResult(
                position_num=pos.position_num,
                headerid=loadrec.headerid if loadrec else None,
                analysisid=loadrec.analysisid if loadrec else None,
                description=pos.description,
                position_type=ptype,
                n_cycles_total=helix_run.n_cycles,
                n_cycles_used=n_used,
                mean_he4_v=m_he4,
                se_he4_v=s_he4,
                mean_he3_a=m_he3,
                se_he3_a=s_he3,
                isrejected=is_rejected,
                rejection_reason=rej_reason,
                ingrowth_days=ig_days,
                water_mass_g=ig_water,
                t_sampling_days=ig_tse,
            )
        )

    # ── Step 3: Background ────────────────────────────────────────────────────
    blank_results = [
        r for r in position_results
        if r.position_type == "blank"
        and not r.isrejected
        and r.mean_he3_a is not None
    ]
    n_blanks = len(blank_results)

    if n_blanks == 0:
        bg_mean = float("nan")
        bg_se = float("nan")
    elif n_blanks == 1:
        bg_mean = blank_results[0].mean_he3_a
        # Use the SE of that single blank's cycles
        bg_se = blank_results[0].se_he3_a if blank_results[0].se_he3_a is not None else float("nan")
    else:
        blank_means = [r.mean_he3_a for r in blank_results]
        bg_mean = _mean(blank_means)
        bg_se = _std(blank_means) / math.sqrt(n_blanks)

    # Attach BG info to each result
    for r in position_results:
        r.bg_mean_a = bg_mean if not math.isnan(bg_mean) else None
        r.bg_se_a = bg_se if not math.isnan(bg_se) else None

    # ── Step 4: Sensitivity ───────────────────────────────────────────────────
    # S_i = net_signal_i / cal_amount_i
    # cal_amount is knownstdactivity (TU, from referencecontroldata) when
    # available; falls back to sampleamount (ccSTP gas amount dispensed) for
    # gas-spike standards where knownstdactivity is NULL.
    # Both cases produce a valid sensitivity; the unit differs (A/TU vs A/ccSTP)
    # and is tracked via cal_unitid so the final activity carries the right unit.
    std_results = [
        r for r in position_results
        if r.position_type == "standard"
        and not r.isrejected
        and r.mean_he3_a is not None
    ]
    n_standards = len(std_results)

    sensitivities: List[float] = []
    sensitivity_uncs: List[float] = []
    cal_unitids: List[Optional[int]] = []    # parallel list — unit per standard

    for r in std_results:
        loadrec = ll_by_pos.get(r.position_num)
        if loadrec is None:
            continue

        # Resolve calibration amount and its unit from the load list record.
        # Priority: knownstdactivity (TU, certified by referencecontroldata) >
        #           sampleamount   (ccSTP, gas amount dispensed from template).
        if loadrec.knownstdactivity is not None and loadrec.knownstdactivity > 0:
            cal_amount = loadrec.knownstdactivity
            cal_unitid = loadrec.knownstdactivityunitid   # unit from referencecontroldata
        elif loadrec.sampleamount is not None and loadrec.sampleamount > 0:
            cal_amount = loadrec.sampleamount
            cal_unitid = loadrec.knownstdactivityunitid   # None → caller resolves ccSTP unit
        else:
            log.warning(
                "Standard at pos %d has neither knownstdactivity nor sampleamount — skipped",
                r.position_num,
            )
            continue

        if math.isnan(bg_mean):
            net_std = r.mean_he3_a
        else:
            net_std = r.mean_he3_a - bg_mean
        S_i = net_std / cal_amount
        sensitivities.append(S_i)
        cal_unitids.append(cal_unitid)

        # Uncertainty on S_i (relative error propagation)
        se3 = r.se_he3_a if r.se_he3_a is not None else 0.0
        if not math.isnan(bg_mean) and bg_mean != 0 and not math.isnan(bg_se):
            rel_unc = math.sqrt(
                (se3 / r.mean_he3_a) ** 2 + (bg_se / bg_mean) ** 2
            ) if r.mean_he3_a and r.mean_he3_a != 0 else float("nan")
        else:
            rel_unc = abs(se3 / r.mean_he3_a) if r.mean_he3_a and r.mean_he3_a != 0 else float("nan")
        S_unc = abs(S_i) * rel_unc if not math.isnan(rel_unc) else float("nan")
        sensitivity_uncs.append(S_unc)

    if len(sensitivities) == 0:
        sensitivity = float("nan")
        sensitivity_unc = float("nan")
        run_unitid: Optional[int] = None
    elif len(sensitivities) == 1:
        sensitivity = sensitivities[0]
        sensitivity_unc = sensitivity_uncs[0] if sensitivity_uncs else float("nan")
        run_unitid = cal_unitids[0]
    else:
        sensitivity = _mean(sensitivities)
        sensitivity_unc = _std(sensitivities) / math.sqrt(len(sensitivities))
        unique_units = set(cal_unitids)
        if len(unique_units) > 1:
            log.warning("Mixed calibration units across standards: %s — unit tracking unreliable", unique_units)
        run_unitid = cal_unitids[0]

    # Attach sensitivity to all results
    for r in position_results:
        r.sensitivity = sensitivity if not math.isnan(sensitivity) else None
        r.sensitivity_unc = sensitivity_unc if not math.isnan(sensitivity_unc) else None

    # ── Step 5: Net 3He and Activity ─────────────────────────────────────────
    n_samples = 0
    for r in position_results:
        if r.mean_he3_a is None:
            continue

        # Net 3He
        bg = r.bg_mean_a if r.bg_mean_a is not None else 0.0
        bg_s = r.bg_se_a if r.bg_se_a is not None else 0.0
        se3 = r.se_he3_a if r.se_he3_a is not None else 0.0

        net = r.mean_he3_a - bg
        net_unc = math.sqrt(se3 ** 2 + bg_s ** 2)
        r.net_he3_a = net
        r.net_he3_unc = net_unc

        # Activity only for samples
        if r.position_type == "sample" and not r.isrejected:
            n_samples += 1
            if r.sensitivity is None or r.sensitivity == 0 or math.isnan(r.sensitivity):
                r.activity = None
                r.activity_unc = None
                r.rejection_reason = (r.rejection_reason + "; no sensitivity").lstrip("; ")
            elif net <= 0:
                r.activity = 0.0
                r.activity_unc = 0.0
                if not r.isrejected:
                    r.rejection_reason = "signal below background"
            else:
                activity = net / r.sensitivity
                sens_unc = r.sensitivity_unc if r.sensitivity_unc else 0.0
                rel_net = net_unc / net if net != 0 else float("nan")
                rel_sens = sens_unc / r.sensitivity if r.sensitivity != 0 else float("nan")
                act_unc = activity * math.sqrt(
                    rel_net ** 2 + rel_sens ** 2
                ) if not (math.isnan(rel_net) or math.isnan(rel_sens)) else float("nan")
                r.activity = activity
                r.activity_unc = act_unc if not math.isnan(act_unc) else None

                # ── Ingrowth correction / TU conversion ──────────────────
                if r.ingrowth_days is not None and r.ingrowth_days > 0:
                    unc_safe = act_unc if (act_unc and not math.isnan(act_unc)) else 0.0
                    if run_unitid is None:
                        # ccSTP-calibrated run: convert ccSTP → TU using water mass.
                        # ccstp_to_tritium_activity already includes the ingrowth
                        # correction factor 1/(1-exp(-λt)) internally.
                        if r.water_mass_g and r.water_mass_g > 0:
                            from ngam_ingrowth_processor import (
                                ccstp_to_tritium_activity,
                                compute_ingrowth_correction,
                            )
                            try:
                                corr_act, corr_unc = ccstp_to_tritium_activity(
                                    activity, unc_safe,
                                    r.ingrowth_days, r.water_mass_g,
                                    t_sampling_days=r.t_sampling_days or 0.0,
                                )
                                r.activity_corrected = corr_act
                                r.activity_corrected_unc = corr_unc if corr_unc else None
                                r.ingrowth_correction_factor = compute_ingrowth_correction(
                                    r.ingrowth_days
                                )
                            except ValueError:
                                pass
                        else:
                            log.warning(
                                "pos %d: ccSTP run but water_mass_g missing — "
                                "cannot convert to TU", r.position_num
                            )
                    else:
                        # TU-calibrated run: activity is already in TU; apply
                        # ingrowth decay correction only.
                        from ngam_ingrowth_processor import decay_correct_activity
                        try:
                            corr_act, corr_unc = decay_correct_activity(
                                activity, unc_safe, r.ingrowth_days,
                            )
                            r.activity_corrected = corr_act
                            r.activity_corrected_unc = corr_unc if corr_unc else None
                            r.ingrowth_correction_factor = corr_act / activity if activity else 1.0
                        except ValueError:
                            pass
        elif r.position_type in ("blank", "standard", "air_ref"):
            # Count blanks and standards but don't compute activity
            pass

    # Recount n_samples from what we actually computed
    n_samples = sum(
        1 for r in position_results if r.position_type == "sample" and not r.isrejected
    )

    return RunProcessingResult(
        run_id=run_id,
        n_blanks=n_blanks,
        n_standards=n_standards,
        n_samples=n_samples,
        bg_mean_a=bg_mean if not math.isnan(bg_mean) else 0.0,
        bg_se_a=bg_se if not math.isnan(bg_se) else 0.0,
        sensitivity=sensitivity if not math.isnan(sensitivity) else 0.0,
        sensitivity_unc=sensitivity_unc if not math.isnan(sensitivity_unc) else 0.0,
        positions=position_results,
        unitid=run_unitid,   # None when only ccSTP standards used; import GUI resolves
    )
