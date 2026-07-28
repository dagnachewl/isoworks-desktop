"""
ngam_protocol_processor.py
==========================
Data-reduction pipeline for Noble Gas MS sequences parsed from NobleControl
.protocol files.

Converts raw signal (A) → background-corrected → blank-subtracted →
sensitivity-calibrated concentration (ccSTP) for every isotope in every
inlet of a sequence.

Pipeline (per isotope, per device, per inlet)
---------------------------------------------
1.  Compute reference time t_ref = mean of all real measurement times for
    that device/inlet (minimises linear-extrapolation error).
2.  Fit y = a + b*(t − t_ref) to every action block individually (linear
    regression).  a = signal at t_ref; b = drift rate (A/s).
3.  net_signal = meas_fit.a − bg_fit.a   (both evaluated at t_ref).
4.  Blank correction: subtract mean net signal of blank inlets.
5.  Sensitivity:  S = blank_corrected_standard / reference_amount  [A/ccSTP]
6.  ccSTP = blank_corrected_sample / S

No database or GUI dependencies.  Works directly on a ProtocolSequence
returned by ngam_protocol_parser.parse_protocol().
"""
from __future__ import annotations

import math
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

# ---------------------------------------------------------------------------
# Processing configuration
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Physical constants
# ---------------------------------------------------------------------------

_MOLAR_VOLUME_STP = 22413.969      # cm^3 / mol  (ideal gas at STP: 0 °C, 1 atm)
_AVOGADRO = 6.02214076e23           # atoms / mol
_SECONDS_PER_YEAR = 365.25 * 24.0 * 3600.0
_WATER_MOLAR_MASS = 18.015          # g / mol  (H₂O)
_H_ATOMS_PER_MOL_WATER = 2.0       # hydrogen atoms per water molecule
_TU_SCALE = 1e18                    # 1 TU = 1 tritium per 10^18 hydrogen atoms

# ---------------------------------------------------------------------------
# Isotope master lookup — maps bare isotope name to element, atmospheric
# abundance, and main-isotope flag.  Derived from noble-gas reference data
# (tblElementIsotopeRatios).  Used to auto-derive ratio definitions and for
# total-element reconstruction.
# ---------------------------------------------------------------------------

ISOTOPE_MASTER: Dict[str, Dict[str, object]] = {
    "3He":  {"element": "He", "abundance": 1.384e-06, "is_main": False},
    "4He":  {"element": "He", "abundance": 0.999998616, "is_main": True},
    "20Ne": {"element": "Ne", "abundance": 0.9048, "is_main": True},
    "21Ne": {"element": "Ne", "abundance": 0.00268, "is_main": False},
    "22Ne": {"element": "Ne", "abundance": 0.0925, "is_main": False},
    "36Ar": {"element": "Ar", "abundance": 0.003364, "is_main": False},
    "38Ar": {"element": "Ar", "abundance": 0.000632, "is_main": False},
    "40Ar": {"element": "Ar", "abundance": 0.996004, "is_main": True},
    "78Kr": {"element": "Kr", "abundance": 0.003469, "is_main": False},
    "80Kr": {"element": "Kr", "abundance": 0.0225, "is_main": False},
    "82Kr": {"element": "Kr", "abundance": 0.1152, "is_main": False},
    "83Kr": {"element": "Kr", "abundance": 0.1147, "is_main": False},
    "84Kr": {"element": "Kr", "abundance": 0.5699, "is_main": True},
    "86Kr": {"element": "Kr", "abundance": 0.1742, "is_main": False},
    "124Xe": {"element": "Xe", "abundance": 0.000951, "is_main": False},
    "126Xe": {"element": "Xe", "abundance": 0.000887, "is_main": False},
    "128Xe": {"element": "Xe", "abundance": 0.019, "is_main": False},
    "129Xe": {"element": "Xe", "abundance": 0.264, "is_main": False},
    "130Xe": {"element": "Xe", "abundance": 0.041, "is_main": False},
    "131Xe": {"element": "Xe", "abundance": 0.212, "is_main": False},
    "132Xe": {"element": "Xe", "abundance": 0.269, "is_main": True},
    "134Xe": {"element": "Xe", "abundance": 0.104, "is_main": False},
    "136Xe": {"element": "Xe", "abundance": 0.089, "is_main": False},
}

# Auto-derived ratio definitions per device.
# Each entry: (ratio_name, numerator_bare_isotope, denominator_bare_isotope).
# All isotopes of the same element measured on a device form a ratio
# with the main isotope as denominator.
NG_RATIOS: Dict[str, List[Tuple[str, str, str]]] = {
    "SMS":    [("3He/4He",    "3He",  "4He")],
    "QMSNe":  [("20Ne/22Ne",  "20Ne", "22Ne"),
               ("21Ne/22Ne",  "21Ne", "22Ne")],
    "QMSAr":  [("38Ar/36Ar",  "38Ar", "36Ar"),
               ("40Ar/36Ar",  "40Ar", "36Ar")],
    "QMSKrXe": [("82Kr/84Kr",   "82Kr",  "84Kr"),
                ("83Kr/84Kr",   "83Kr",  "84Kr"),
                ("86Kr/84Kr",   "86Kr",  "84Kr"),
                ("129Xe/132Xe", "129Xe", "132Xe"),
                ("131Xe/132Xe", "131Xe", "132Xe"),
                ("134Xe/132Xe", "134Xe", "132Xe"),
                ("136Xe/132Xe", "136Xe", "132Xe")],
}

# Reverse map: "3He4He" (no-slash DB name) → "3He/4He" (NG_RATIOS key).
# Lets certified_values keyed by the old no-slash analyte name feed into
# reference_amounts under the slash key that ratio processing expects.
_RATIO_NOSLASH_TO_KEY: Dict[str, str] = {
    rn.replace("/", ""): rn
    for ratios in NG_RATIOS.values()
    for rn, _, _ in ratios
}


@dataclass
class ProcessingConfig:
    """Controls outlier rejection, blank interpolation, and drift correction."""
    outlier_method: str = "mad"        # "none" | "nsigma" | "mad" | "huber"
    nsigma_threshold: float = 2.5
    # Blank interpolation fit type.
    # "mean"        — constant mean of all blanks (no time dependence)
    # "linear"      — degree-1 polynomial vs time
    # "quadratic"   — degree-2 polynomial vs time
    # "cubic"       — degree-3 polynomial vs time
    blank_interpolation: str = "mean"
    # Sensitivity / drift correction fit type.
    # "none"        — use mean sensitivity (no drift correction)
    # "linear"      — degree-1 polynomial vs time
    # "quadratic"   — degree-2 polynomial vs time
    # "cubic"       — degree-3 polynomial vs time
    # "exponential" — y = a · exp(b·t); falls back to linear if values ≤ 0
    drift_correction: str = "none"
    min_std_for_drift: int = 2        # minimum standards needed for a drift fit
    # Detector linearity correction: fit sensitivity vs signal level through standards.
    # "none"      — no correction
    # "linear"    — degree-1 polynomial fit S vs bc-signal
    # "quadratic" — degree-2 polynomial fit
    linearity_correction: str = "none"
    # Linearity mode: "single" (current sequence only) or "multi" (cross-run).
    linearity_mode: str = "single"
    # Activity/concentration computation.
    compute_activity: bool = False    # enable step-10 conversion
    tritium_half_life_years: float = 12.32  # ³H half-life
    # Signal fitting model for SMS/QMS net-signal extraction.
    # "Auto"        — AICc-based automatic model selection (recommended)
    # "Linear"      — always use linear extrapolation to t=0
    # "Average"/"Poly2"/"Poly3"/"Exponential" — fixed model
    # "block"       — legacy: per-block linear fit at t_ref (original behaviour)
    signal_fit_model: str = "Auto"
    # SEM dead-time correction for the 3He multiplier channel.
    # Formula: n_true = n_meas / (1 − n_meas · dead_time_tau)
    # Units: same as the raw signal values in the SMS Multiplier column.
    # If signals are in CPS, dead_time_tau is in seconds (e.g. 20e-9 for 20 ns).
    # If the instrument outputs a scaled unit (e.g. ~0.1–0.2 for a large standard),
    # determine τ empirically from a linearity curve.  Default 0 = disabled.
    dead_time_tau: float = 0.0
    # Gauge (SRG / pressure sensor) summary from the .InletState file.
    # gauge_sigma        : σ-clipping threshold for outlier removal within each inlet window
    # gauge_qc_flags     : when True, flag inlets whose SRG mean > 3× run median in QC tab
    # gauge_plateau_max_*: cap plateau for each element to last N readings; None = all stable
    gauge_sigma: float = 3.0
    gauge_qc_flags: bool = True
    gauge_plateau_max_he: Optional[int] = None
    gauge_plateau_max_ne: Optional[int] = None
    gauge_plateau_max_ar: Optional[int] = None
    gauge_baseline_max_he: Optional[int] = None
    gauge_baseline_max_ne: Optional[int] = None
    gauge_baseline_max_ar: Optional[int] = None
    # Step 9b — Physical dilution correction.
    # When enabled, ccSTP / drift_ccSTP / linearity_ccSTP (and uncertainties)
    # are multiplied by each inlet's dilution_factor.  Default 1.0 = no change.
    dilution_enabled: bool = False
    # Per-inlet dilution overrides: {seq_num: factor}.  Falls back to
    # InletPrep.dilution_factor (parsed from .protocol), then to 1.0.
    dilution_factors: Dict[int, float] = field(default_factory=dict)
    # Linearity total-load x-axis: when non-empty, the x-axis of the S(I) fit
    # is the sum of blank-corrected signals for the listed isotope keys rather
    # than the per-isotope signal.  Multi-run historical data is skipped in this
    # mode (archived x-values are per-isotope and are not comparable).
    linearity_total_load_keys: List[str] = field(default_factory=list)
    # Gauge linearity: fit S vs TotalP (Baratron) through multiple standard sizes.
    # Values: "none" | "auto" | "linear" | "quadratic" | "cubic"
    gauge_linearity_correction: str = "none"
    he_dilution_factor: float = 2.4
    ne_dilution_factor: float = 2.4
    db_dilution_factors: Dict[str, float] = field(default_factory=dict)

    def get_dilution_factor_for_gas(self, gas: str) -> float:
        """Resolve dilution factor for gas ("He" or "Ne") using DB lookups first, then config/defaults."""
        if gas == "He":
            return self.db_dilution_factors.get("He", self.he_dilution_factor)
        elif gas == "Ne":
            return self.db_dilution_factors.get("Ne", self.ne_dilution_factor)
        return 1.0
    # Step 9c — Isotope Dilution Mass Spectrometry (IDMS).
    # Computes B_sample from the measured ratio Rm = (A/B) of the
    # blank-corrected signals and the certified spike characteristics.
    # idms_config maps target_isotope (bare name, e.g. "20Ne") → {
    #     "spike_isotope": str,          # e.g. "22Ne"
    #     "sample_ratio": float,         # atmospheric (A/B)_sample e.g. 0.102
    #     "spike_per_inlet": Dict[int, str],  # seq_num → spike_labid
    # }
    idms_enabled: bool = False
    idms_config: Dict[str, Dict] = field(default_factory=dict)
    # Maps spike_labid → {"A_spike": float, "A_spike_unc": float, "R_spike": float, "R_spike_unc": float}
    # populated dynamically from public.referencecontroldata or provided in config
    spike_certified: Dict[str, Dict[str, float]] = field(default_factory=dict)
    # Step 9d — isotope ratio computation (within-inlet, ccSTP-based).
    # When True, all possible ratios for the detected devices are auto-computed.
    compute_ratios: bool = True
    # Background proxy mapping and scaling factors
    bg_proxy_map: Dict[str, str] = field(default_factory=dict)
    bg_proxy_factors: Dict[str, float] = field(default_factory=dict)
    bg_proxy_mode: str = "auto"

from ngam_protocol_parser import ProtocolSequence, InletPrep, MSMeasurementData

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Action classification helpers
# ---------------------------------------------------------------------------

def _is_background(action: str) -> bool:
    return "background" in action.lower()


def _is_inlet_scan(action: str) -> bool:
    """Pre-measurement inlet scans (Inlet* prefix) — not used in calibration."""
    return action.lower().startswith("inlet")


def _is_measurement(action: str) -> bool:
    return not _is_background(action) and not _is_inlet_scan(action)


# ---------------------------------------------------------------------------
# Statistics helpers
# ---------------------------------------------------------------------------

def _finite(vals: List[float]) -> List[float]:
    return [v for v in vals if not math.isnan(v) and not math.isinf(v)]


def _mean(vals: List[float]) -> float:
    f = _finite(vals)
    return sum(f) / len(f) if f else float("nan")


def _se(vals: List[float]) -> float:
    f = _finite(vals)
    n = len(f)
    if n < 2:
        return float("nan")
    mu = sum(f) / n
    var = sum((v - mu) ** 2 for v in f) / (n - 1)
    return math.sqrt(var / n)


def _linearity_x(
    ir: Any,
    key: str,
    total_load_keys: List[str],
    prefer_interpolated: bool = False,
) -> float:
    """x-axis value for the linearity S(I) fit for one inlet.

    When *total_load_keys* is non-empty, return the sum of blank-corrected
    signals for those keys (total source-load proxy).  Otherwise return the
    per-isotope blank-corrected signal for *key*.
    """
    if total_load_keys:
        total = 0.0
        any_valid = False
        for k in total_load_keys:
            iso = ir.isotopes.get(k)
            if iso is None:
                continue
            v = (iso.interpolated_blank_corrected
                 if prefer_interpolated and not math.isnan(iso.interpolated_blank_corrected)
                 else iso.blank_corrected)
            if not math.isnan(v):
                total += v
                any_valid = True
        return total if any_valid else float("nan")
    iso = ir.isotopes.get(key)
    if iso is None:
        return float("nan")
    if prefer_interpolated and not math.isnan(iso.interpolated_blank_corrected):
        return iso.interpolated_blank_corrected
    return iso.blank_corrected


def _linear_fit(
    times: List[float],
    signals: List[float],
    t_ref: float,
) -> Tuple[float, float, float, float, float]:
    """
    Fit y = a + b*(t − t_ref) by ordinary least squares.

    Returns
    -------
    (a, se_a, b, se_b, r2)
        a     : signal at t_ref
        se_a  : standard error of a
        b     : slope  (A/s)
        se_b  : standard error of b
        r2    : coefficient of determination (NaN if < 3 points)
    """
    pairs = [
        (t - t_ref, s)
        for t, s in zip(times, signals)
        if not math.isnan(t) and not math.isnan(s)
        and not math.isinf(t) and not math.isinf(s)
    ]
    n = len(pairs)

    if n == 0:
        nan = float("nan")
        return nan, nan, nan, nan, nan

    xs = [p[0] for p in pairs]
    ys = [p[1] for p in pairs]

    if n == 1:
        return ys[0], float("nan"), 0.0, float("nan"), float("nan")

    xbar = sum(xs) / n
    ybar = sum(ys) / n

    sxx = sum((x - xbar) ** 2 for x in xs)
    sxy = sum((x - xbar) * (y - ybar) for x, y in zip(xs, ys))
    syy = sum((y - ybar) ** 2 for y in ys)

    if sxx == 0:
        # All timestamps identical — return mean ± SE_mean
        sd = math.sqrt(sum((y - ybar) ** 2 for y in ys) / (n - 1)) if n > 1 else 0.0
        se_a = sd / math.sqrt(n) if n > 0 else float("nan")
        return ybar, se_a, 0.0, float("nan"), float("nan")

    b = sxy / sxx
    a = ybar - b * xbar            # intercept: signal at x=0, i.e. at t_ref

    residuals = [y - (a + b * x) for x, y in zip(xs, ys)]
    ss_res = sum(r ** 2 for r in residuals)

    if n > 2:
        s2 = ss_res / (n - 2)
        se_a = math.sqrt(s2 * (1.0 / n + xbar ** 2 / sxx))
        se_b = math.sqrt(s2 / sxx)
        r2 = 1.0 - ss_res / syy if syy > 0 else float("nan")
    else:
        se_a = float("nan")
        se_b = float("nan")
        r2 = float("nan")

    return a, se_a, b, se_b, r2


def _get_gas_type(isotope_key: str) -> str:
    """Return gas type: Helium, Neon, Argon, or Other."""
    iso = isotope_key.split(":", 1)[1] if ":" in isotope_key else isotope_key
    iso_clean = iso.strip().lower()
    if "he" in iso_clean:
        return "Helium"
    elif "ne" in iso_clean:
        return "Neon"
    elif "ar" in iso_clean:
        return "Argon"
    return "Other"


def _compute_idms(
    A_spike: float,
    A_spike_unc: float,
    R_spike: float,
    R_spike_unc: float,
    R_m: float,
    R_m_unc: float,
    R_sample: float,
) -> tuple:
    """Compute isotope-dilution corrected sample amount and uncertainty.

    IDMS equation::

        B_sample = A_spike * (R_spike - R_m) / (R_spike * (R_m - R_sample))

    where A_spike is the certified spike-isotope amount, R_spike is the
    certified spike ratio A/B, R_m is the measured ratio A/B in the
    sample-spike mixture, and R_sample is the natural (atmospheric)
    ratio A/B of the unspiked sample.

    Returns (B_sample, B_sample_unc) or (None, float("nan")) on invalid inputs.
    """
    denominator = R_spike * (R_m - R_sample)
    if denominator == 0 or R_spike <= 0:
        return None, float("nan")
    if A_spike <= 0:
        return None, float("nan")

    numerator = R_spike - R_m
    B = A_spike * numerator / denominator

    if B <= 0:
        return None, float("nan")

    # First-order error propagation:
    # ∂B/∂A        = (R_spike - R_m) / (R_spike * (R_m - R_sample))
    # ∂B/∂R_m      = -A_spike * (R_spike - R_sample)
    #                 / (R_spike * (R_m - R_sample)^2)
    # ∂B/∂R_spike  =  A_spike * R_m / (R_spike^2 * (R_m - R_sample))
    # ∂B/∂R_sample =  A_spike * (R_spike - R_m)
    #                 / (R_spike * (R_m - R_sample)^2)  — treated as exact
    dBA = numerator / denominator
    dB_Rm = -A_spike * (R_spike - R_sample) / (R_spike * (R_m - R_sample) ** 2)
    dB_Rs = A_spike * R_m / (R_spike ** 2 * (R_m - R_sample))

    var = (
        dBA ** 2 * A_spike_unc ** 2
        + dB_Rm ** 2 * R_m_unc ** 2
        + dB_Rs ** 2 * R_spike_unc ** 2
    )
    B_unc = math.sqrt(var) if var > 0 else 0.0
    return B, B_unc


def _pick_best_ccstp(iso) -> Tuple[float, float]:
    """Return (ccSTP, uncertainty) picking linearity > drift > basic."""
    if not math.isnan(iso.linearity_ccSTP):
        return iso.linearity_ccSTP, iso.linearity_ccSTP_unc
    if not math.isnan(iso.drift_ccSTP):
        return iso.drift_ccSTP, iso.drift_ccSTP_unc
    return iso.ccSTP, iso.ccSTP_unc


def _compute_ccstp_ratio(
    c_num: float, u_num: float,
    c_den: float, u_den: float,
) -> Tuple[float, float]:
    """Compute ratio = num/den with quadrature relative-uncertainty propagation."""
    if math.isnan(c_num) or math.isnan(c_den) or c_den == 0:
        return float("nan"), float("nan")
    ratio = c_num / c_den
    rel_num = u_num / c_num if c_num != 0 and not math.isnan(u_num) else 0.0
    rel_den = u_den / c_den if not math.isnan(u_den) else 0.0
    unc = ratio * math.sqrt(rel_num ** 2 + rel_den ** 2)
    return ratio, unc


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class BlockFitResult:
    """Linear regression result for one action block within a device file."""
    action: str
    n_points: int
    n_outliers: int                                   # points rejected before final fit
    outlier_flags: List[bool] = field(default_factory=list)  # True = outlier, len == n_points
    t_ref: float = float("nan")   # LabVIEW timestamp used as reference
    value_at_ref: float = float("nan")  # a  — signal (A) at t_ref
    value_unc: float = 0.0        # se_a
    slope: float = float("nan")   # b  — A/s
    slope_unc: float = float("nan")  # se_b
    r_squared: float = float("nan")
    is_background: bool = False


@dataclass
class IsotopeResult:
    """
    Full reduction chain for one isotope within one inlet and one device.
    Fields are populated progressively through the pipeline.
    """
    isotope: str
    device: str

    # Step 2 — fits
    meas_fit: Optional[BlockFitResult] = None
    bg_fit: Optional[BlockFitResult] = None

    # Step 3 — background subtraction
    net_signal: float = float("nan")
    net_unc: float = float("nan")
    signal_fit_model: str = "block"  # model used for net_signal; "block" = legacy linear
    bg_used: str = "original"

    # Step 4 — blank correction
    blank_net: float = float("nan")
    blank_unc: float = float("nan")
    blank_corrected: float = float("nan")
    blank_corrected_unc: float = float("nan")

    # Step 5/6 — mean sensitivity & ccSTP
    sensitivity: float = float("nan")
    sensitivity_unc: float = float("nan")
    ccSTP: float = float("nan")
    ccSTP_unc: float = float("nan")

    # Individual sensitivity for standards (set during step 5)
    inlet_sensitivity: float = float("nan")
    inlet_sensitivity_unc: float = float("nan")

    # Step 7 — interpolated blank correction
    interpolated_blank: float = float("nan")
    interpolated_blank_corrected: float = float("nan")
    interpolated_blank_corrected_unc: float = float("nan")

    # Step 8 — drift-corrected ccSTP
    drift_sensitivity: float = float("nan")
    drift_ccSTP: float = float("nan")
    drift_ccSTP_unc: float = float("nan")

    # Step 9 — linearity-corrected ccSTP
    linearity_sensitivity: float = float("nan")
    linearity_ccSTP: float = float("nan")
    linearity_ccSTP_unc: float = float("nan")

    # Step 10 — activity / concentration
    activity: float = float("nan")
    activity_unc: float = float("nan")
    concentration: float = float("nan")
    concentration_unc: float = float("nan")

    # Step 11 — extraction correction (water samples only)
    # ccSTP_true = ccSTP_final / extraction_efficiency
    extraction_efficiency: float = float("nan")   # η applied (nan = not applicable)
    ccSTP_true: float = float("nan")              # efficiency-corrected total gas
    ccSTP_true_unc: float = float("nan")
    ccSTP_per_g: float = float("nan")             # ccSTP_true / water_mass_g
    ccSTP_per_g_unc: float = float("nan")
    c_eq_cm3_per_g: float = float("nan")          # equilibrium solubility at T_sample

    # Step 9d — within-inlet isotope ratios that include this isotope.
    # Key: ratio_name (e.g. "3He/4He"), value: (ratio, uncertainty).
    ratios: Dict[str, Tuple[float, float]] = field(default_factory=dict)


@dataclass
class RatioResult:
    """Per-inlet result for one isotope ratio, processed in ratio signal space."""
    ratio_name: str      # e.g. "3He/4He"
    num_key: str         # e.g. "SMS:3He"
    den_key: str         # e.g. "SMS:4He"

    # Step 3b — raw ratio of net signals
    raw_ratio: float = float("nan")
    raw_ratio_unc: float = float("nan")

    # Step 7b — blank correction via mixing equation
    # (4He)_meas = (4He)_sample + (4He)_blank  →  a = (4He)_blank / (4He)_meas
    # R_bc = (R_meas − a·R_blank) / (1 − a)
    blank_ratio: float = float("nan")         # interpolated blank ratio at inlet time
    blank_fraction: float = float("nan")      # a = I_den_blank_interp / I_den_meas
    blank_corrected: float = float("nan")
    blank_corrected_unc: float = float("nan")

    # Step 8b — sensitivity and drift correction
    inlet_sensitivity: float = float("nan")      # R_bc / R_certified (standard inlets)
    inlet_sensitivity_unc: float = float("nan")
    drift_sensitivity: float = float("nan")      # interpolated from drift fit
    drift_corrected: float = float("nan")        # R_bc / drift_sensitivity
    drift_corrected_unc: float = float("nan")

    # Step 9b — linearity correction (vs denominator signal level)
    linearity_sensitivity: float = float("nan")
    linearity_corrected: float = float("nan")
    linearity_corrected_unc: float = float("nan")


@dataclass
class InletProcessingResult:
    """All isotope results for one inlet (one InletPrep)."""
    seq_num: int
    inlet_string: str
    inlet_type: str           # "blank" | "standard" | "sample"
    is_repro_ref: bool = False  # R inlet — bisreproreference; used for drift/sensitivity fit
    is_lin_ref: bool = False    # L inlet — bislinreference; used for linearity fit
    reference_amount: float = 0.0   # ccSTP (from InletPrep / ngpreparations)
    dilution_factor: float = 1.0
    partition_steps: Dict[str, int] = field(default_factory=lambda: {"Helium": 0, "Neon": 0, "Argon": 0})

    # Per-isotope reference amounts for repro references.
    # Key: "device:isotope", value: certified amount in ccSTP.
    reference_amounts: Dict[str, float] = field(default_factory=dict)
    # Per-isotope certified uncertainties.
    reference_uncs: Dict[str, float] = field(default_factory=dict)

    # Key format: "{device}:{isotope}"
    isotopes: Dict[str, IsotopeResult] = field(default_factory=dict)

    # All block fits per device (including background), for chart overlays.
    # block_fits[device][action] = BlockFitResult
    block_fits: Dict[str, Dict[str, "BlockFitResult"]] = field(default_factory=dict)

    # Step 9d — within-inlet ratios for this inlet (legacy, populated from ratio_results).
    # Key: ratio_name (e.g. "3He/4He"), value: (ratio, uncertainty).
    ratios: Dict[str, Tuple[float, float]] = field(default_factory=dict)

    # Ratio pipeline results (Steps 3b/7b/8b/9b).
    # Key: ratio_name (e.g. "3He/4He")
    ratio_results: Dict[str, "RatioResult"] = field(default_factory=dict)

    # Gauge-based concentrations (He, Ne, Ar) from SRG pressure.
    # Populated after compute_gauge_summary; empty when no gauge data.
    gauge_conc: Dict[str, float] = field(default_factory=dict)
    gauge_conc_unc: Dict[str, float] = field(default_factory=dict)
    # Per-gram gauge concentrations; populated only when water_mass_g is known.
    gauge_conc_per_g: Dict[str, float] = field(default_factory=dict)
    gauge_conc_per_g_unc: Dict[str, float] = field(default_factory=dict)

    def iso_key(self, device: str, isotope: str) -> str:
        return f"{device}:{isotope}"

    def get_isotope(self, device: str, isotope: str) -> Optional[IsotopeResult]:
        return self.isotopes.get(self.iso_key(device, isotope))


@dataclass
class DriftFit:
    """Fit of per-standard sensitivity vs time for drift correction."""
    isotope_key: str
    fit_type: str                   # "mean"|"linear"|"quadratic"|"cubic"|"exponential"|"akima"
    degree: int                     # polynomial degree (0 for mean/exponential/akima)
    coeffs: List[float]             # polynomial: high→low; exponential: [a,b]; akima: []
    r_squared: float
    std_times: List[float]          # lv_time_start for each standard (all, including outliers)
    std_sensitivities: List[float]  # individual S per standard (all)
    std_sensitivity_uncs: List[float]  # 1-σ uncertainty of each standard's sensitivity
    std_fit_residuals: List[float]   # |S_i - fit(t_i)| — deviation from drift model (for error bars)
    std_seq_nums: List[int]         # seq_num of each standard (all)
    cov: Optional[List[List[float]]] = None
    outlier_mask: List[bool] = field(default_factory=list)       # True = auto-rejected from fit
    user_excluded_mask: List[bool] = field(default_factory=list) # True = manually excluded by user


@dataclass
class BlankFit:
    """Fit of blank net signal vs time for blank interpolation."""
    isotope_key: str
    fit_type: str                   # "mean"|"linear"|"quadratic"|"cubic"|"akima"
    degree: int                     # 0 for mean/akima
    coeffs: List[float]             # polynomial coeffs; [] for akima
    r_squared: float
    blank_times: List[float]        # all blank times (including outliers)
    blank_signals: List[float]      # all blank net signals (including outliers)
    blank_signal_uncs: List[float]  # 1-σ uncertainty of each blank's net signal
    blank_fit_residuals: List[float] # |B_i - fit(t_i)| — deviation from blank model (for error bars)
    blank_seq_nums: List[int]
    cov: Optional[List[List[float]]] = None
    outlier_mask: List[bool] = field(default_factory=list)       # True = auto-rejected from fit
    user_excluded_mask: List[bool] = field(default_factory=list) # True = manually excluded by user


@dataclass
class LinearityFit:
    """
    Fit of sensitivity vs blank-corrected signal level for linearity assessment.

    S(bc) = f(bc) where bc is the blank-corrected signal (A).
    A flat S(bc) means perfectly linear response.  A sloping fit indicates
    signal-level-dependent sensitivity (detector non-linearity).
    """
    isotope_key: str
    fit_type: str               # "none" | "linear" | "quadratic"
    degree: int
    coeffs: List[float]         # polynomial coeffs high→low
    r_squared: float
    signal_levels: List[float]  # blank-corrected signal of each standard (A), all
    sensitivities: List[float]  # individual sensitivity of each standard, all
    sensitivity_uncs: List[float]  # 1-σ uncertainty of each standard's sensitivity
    sensitivity_fit_residuals: List[float] # |S_i - fit(X_i)| — deviation from linearity model (for error bars)
    std_seq_nums: List[int]
    cov: Optional[List[List[float]]] = None
    outlier_mask: List[bool] = field(default_factory=list)       # True = auto-rejected from fit
    user_excluded_mask: List[bool] = field(default_factory=list) # True = manually excluded by user


@dataclass
class CrossInletRatio:
    """One cross-inlet ratio result — num/den may come from different inlets."""
    ratio_name: str              # e.g. "3He/4He"
    ratio_value: float
    ratio_unc: float
    num_seq_num: int             # inlet providing numerator ccSTP
    den_seq_num: int             # inlet providing denominator ccSTP
    method: str = "ccSTP"        # "ccSTP_best" | "ccSTP_per_g"


@dataclass
class SequenceProcessingResult:
    """Complete reduction result for one ProtocolSequence."""
    sequence_id: Optional[int]
    n_blanks: int
    n_standards: int
    n_samples: int

    # Mean blank per "device:isotope"
    blank_means: Dict[str, float] = field(default_factory=dict)
    blank_uncs: Dict[str, float] = field(default_factory=dict)

    # Mean sensitivity per "device:isotope"  (A / ccSTP)
    sensitivities: Dict[str, float] = field(default_factory=dict)
    sensitivity_uncs: Dict[str, float] = field(default_factory=dict)

    # Calibration fits (populated in steps 7–9)
    drift_fits: Dict[str, DriftFit] = field(default_factory=dict)
    blank_fits: Dict[str, BlankFit] = field(default_factory=dict)
    linearity_fits: Dict[str, LinearityFit] = field(default_factory=dict)

    inlets: List[InletProcessingResult] = field(default_factory=list)

    # Step 11b — cross-inlet ratios (samples only, after extraction correction).
    # Key: ratio_name, value: list of CrossInletRatio entries.
    cross_ratios: Dict[str, List[CrossInletRatio]] = field(default_factory=dict)

    # Gauge summary from .InletState (None when InletState absent or disabled)
    gauge_summary: Optional["GaugeSequenceSummary"] = None  # type: ignore[name-defined]

    # Auto-computed 4He/3He background proxy scaling factor from blank inlet data
    auto_bg_proxy_alpha: Optional[float] = None
    auto_bg_proxy_alpha_n: int = 0
    auto_bg_proxy_alpha_std: Optional[float] = None

    # Human-readable warnings about CV date fallbacks (populated by the API /
    # calling layer after enrich_sequence_with_reference_amounts).
    calibration_warnings: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# External calibration data (passed in from GUI / DB layer)
# ---------------------------------------------------------------------------

@dataclass
class ReproReferenceInfo:
    """Certified-value metadata for a reproducibility-reference inlet.

    A reproducibility reference is a real sample whose concentration is
    certified in public.referencecontroldata.  The certification is
    per-measurable (isotope), so this struct carries per-isotope-key
    (e.g. ``"SMS:3He"``) amounts rather than a single inlet-level scalar.
    """
    seq_num: int
    ourlabid: str                    # e.g. "PRFX-1234"
    # isotope_key → (certified_value, certified_unc)
    certified_values: Dict[str, Tuple[float, float]] = field(default_factory=dict)


@dataclass
class MultiRunLinearityData:
    """Pre-computed linearity data gathered from historical runs.

    The caller queries ``ngam.nglinearitysnapshots`` for runs that share the
    same linearity reference (bislinreference=True, same ourlabid) and builds
    one instance per isotope key.  The processor merges this data with the
    current run's standards before fitting.
    """
    isotope_key: str
    signal_levels: List[float] = field(default_factory=list)  # bc signals (A)
    sensitivities: List[float] = field(default_factory=list)   # S (A/ccSTP)
    run_ids: List[int] = field(default_factory=list)


@dataclass
class ExtractionInfo:
    """Per-sample metadata for water-extraction noble gas samples.

    Populated by the caller from ngam.ngextractiondata.  All environmental
    fields are optional — None means "use default / no correction".

    Corrections applied in process_sequence() Step 11:
      1. Extraction efficiency:  ccSTP_true = ccSTP_measured / η
         η priority: element_efficiency[element] > extraction_efficiency > 1.0
      2. Concentration normalisation:  ccSTP_per_g = ccSTP_true / water_mass_g
      3. Equilibrium reference:  C_eq(T, S, P) from ngam_solubility — stored
         alongside the result for QC display (not subtracted from the data).
    """
    seq_num: int
    water_mass_g: float              # net water mass extracted from (g)
    temperature_c: Optional[float] = None    # field sampling temperature (°C)
    salinity_ppt: float = 0.0               # salinity g/kg; 0 = freshwater
    altitude_m: Optional[float] = None      # site altitude (m); None = sea level
    extraction_efficiency: Optional[float] = None  # scalar fallback: 0 < η ≤ 1.0
    # Per-element η from ngextractionlineefficiency — keyed by element symbol.
    # Takes priority over extraction_efficiency when present.
    element_efficiency: Dict[str, float] = field(default_factory=dict)
    element_efficiency_unc: Dict[str, float] = field(default_factory=dict)


@dataclass
class IngrowthInfo:
    """Per-sample ingrowth metadata needed for ³He → ³H (TU) conversion.

    Populated by the caller from NGAM.NG3HeIngrowthData via ingrowthid:
      t_ingrowth_seconds  = (DTimeEnd  − DTimeStart).total_seconds()
      water_mass_before_g = FWeightWaterBulbBefore − FWeightWaterBulbEmpty
      water_mass_after_g  = FWeightWaterBulbAfter  − FWeightWaterBulbEmpty

    The post-degassing water mass (water_mass_after_g) is used for TU
    normalisation because it represents the actual sample that produced ³He.
    """
    seq_num: int
    t_ingrowth_seconds: float    # ingrowth duration in seconds (DTimeEnd − DTimeStart)
    water_mass_before_g: float   # water before degassing (g)
    water_mass_after_g: float    # water after degassing — used for TU (g)
    t_sampling_seconds: float = 0.0  # storage duration (DTimeStart − collectiondate)


# ---------------------------------------------------------------------------
# Outlier detection
# ---------------------------------------------------------------------------

def _compute_outlier_flags(
    times: List[float],
    values: List[float],
    t_ref: float,
    config: ProcessingConfig,
) -> List[bool]:
    """
    Outlier detection for block signals based on residuals from a linear fit.
    Supports: "none", "nsigma" (or "sd"), "mad", "huber"
    """
    n = len(times)
    flags = [False] * n
    method = (config.outlier_method or "none").lower()
    if method == "none" or n < 3:
        return flags

    # Pass 1: standard OLS fit
    a, _, b, _, _ = _linear_fit(times, values, t_ref)
    if math.isnan(a) or math.isnan(b):
        return flags

    residuals = [v - (a + b * (t - t_ref)) for t, v in zip(times, values)]
    finite_res = [r for r in residuals if not math.isnan(r) and not math.isinf(r)]
    if len(finite_res) < 3:
        return flags

    threshold = config.nsigma_threshold

    if method in ("nsigma", "sd"):
        mu_r = sum(finite_res) / len(finite_res)
        var_r = sum((r - mu_r) ** 2 for r in finite_res) / (len(finite_res) - 1)
        sd_r = math.sqrt(var_r) if var_r > 0 else 0.0
        if sd_r == 0:
            return flags
        lim = threshold * sd_r
        for i, v in enumerate(values):
            if math.isnan(v) or math.isinf(v):
                flags[i] = True
            elif abs(residuals[i]) > lim:
                flags[i] = True

    elif method == "mad":
        import numpy as np
        res_arr = np.array(finite_res)
        med_r = np.median(res_arr)
        mad_r = np.median(np.abs(res_arr - med_r))
        if mad_r == 0:
            # Fall back to standard deviation
            mad_r = np.std(res_arr, ddof=1)
        if mad_r == 0:
            return flags
        
        for i, v in enumerate(values):
            if math.isnan(v) or math.isinf(v):
                flags[i] = True
            else:
                z = 0.6745 * abs(residuals[i] - med_r) / mad_r
                if z > threshold:
                    flags[i] = True

    elif method == "huber":
        import numpy as np
        xs = np.array([t - t_ref for t in times])
        ys = np.array(values)
        
        mask = np.isfinite(xs) & np.isfinite(ys)
        if np.sum(mask) < 3:
            return flags
        
        w = np.ones(n)
        a_fit, b_fit = a, b
        k = threshold  # Huber constant
        
        for _ in range(20):
            W = np.diag(w * mask)
            X = np.column_stack((np.ones(n), xs))
            try:
                XT_W_X = np.dot(X.T, np.dot(W, X))
                XT_W_y = np.dot(X.T, np.dot(W, ys))
                c = np.linalg.solve(XT_W_X, XT_W_y)
                a_new, b_new = c[0], c[1]
            except np.linalg.LinAlgError:
                break
                
            res = ys - (a_new + b_new * xs)
            scale = np.median(np.abs(res[mask])) / 0.6745
            if scale < 1e-12:
                scale = 1e-12
                
            u = res / scale
            abs_u = np.abs(u)
            with np.errstate(divide="ignore", invalid="ignore"):
                w_new = np.where(abs_u <= k, 1.0, k / np.where(abs_u == 0, 1.0, abs_u))
            
            if np.abs(a_new - a_fit) < 1e-6 * scale and np.abs(b_new - b_fit) < 1e-6 * scale:
                a_fit, b_fit = a_new, b_new
                break
            a_fit, b_fit = a_new, b_new
            w = w_new
            
        final_res = ys - (a_fit + b_fit * xs)
        final_scale = np.median(np.abs(final_res[mask])) / 0.6745
        if final_scale < 1e-12:
            final_scale = 1e-12
        u_final = np.abs(final_res / final_scale)
        
        for i, v in enumerate(values):
            if not mask[i]:
                flags[i] = True
            elif u_final[i] > k:
                flags[i] = True

    return flags


# ---------------------------------------------------------------------------
# Reference-time computation
# ---------------------------------------------------------------------------

def _compute_t_ref(ms: MSMeasurementData) -> float:
    """
    Reference time = mean timestamp of all real measurement signals.

    Using the mean of the measurement times as t_ref ensures the linear
    regression intercept (= signal at t_ref) has the minimum possible
    standard error — no extrapolation is required.
    """
    meas_times = [
        r.lv_time for r in ms.signals
        if _is_measurement(r.action)
        and not math.isnan(r.lv_time)
    ]
    if not meas_times:
        meas_times = [r.lv_time for r in ms.signals if not math.isnan(r.lv_time)]
    return _mean(meas_times)


# ---------------------------------------------------------------------------
# Block fitting
# ---------------------------------------------------------------------------

def fit_block(
    action: str,
    signals,                          # iterable of MSSignalRow
    t_ref: float,
    config: Optional[ProcessingConfig] = None,
    override_flags: Optional[List[bool]] = None,
) -> BlockFitResult:
    """
    Fit a linear trend to all rows of one ActionToDo block.

    When config.outlier_method == "nsigma", performs a two-pass fit:
    first pass on all points to get residuals, then rejects points whose
    residual exceeds nsigma * std(residuals) and refits on survivors.
    """
    if config is None:
        config = ProcessingConfig()

    rows = list(signals)
    times = [r.lv_time for r in rows]
    values = [r.signal for r in rows]
    n = len(rows)

    # User-supplied flags take precedence over auto-detection.
    if override_flags is not None:
        flags = list(override_flags)
        # Pad / trim to match n
        while len(flags) < n:
            flags.append(False)
        flags = flags[:n]
    else:
        flags = _compute_outlier_flags(times, values, t_ref, config)

    clean_t = [t for t, f in zip(times, flags) if not f]
    clean_v = [v for v, f in zip(values, flags) if not f]

    a, se_a, b, se_b, r2 = _linear_fit(clean_t, clean_v, t_ref)

    return BlockFitResult(
        action=action,
        n_points=n,
        n_outliers=sum(flags),
        outlier_flags=flags,
        t_ref=t_ref,
        value_at_ref=a,
        value_unc=se_a if not math.isnan(se_a) else 0.0,
        slope=b,
        slope_unc=se_b,
        r_squared=r2,
        is_background=_is_background(action),
    )


# ---------------------------------------------------------------------------
# Per-device processing
# ---------------------------------------------------------------------------

def process_ms_device(
    ms: MSMeasurementData,
    config: Optional[ProcessingConfig] = None,
    flag_overrides: Optional[Dict[str, List[bool]]] = None,
) -> Tuple[Dict[str, IsotopeResult], Dict[str, BlockFitResult]]:
    """
    Fit all blocks for one device file and return background-corrected
    net signals keyed by isotope name, plus the raw fit dict for all blocks.

    Parameters
    ----------
    ms : MSMeasurementData
        Parsed data for one device (SMS / QMSNe / QMSAr / QMSKrXe).
    config : ProcessingConfig, optional
        Outlier-rejection settings.

    Returns
    -------
    (isotope_results, all_fits)
        isotope_results : dict  action → IsotopeResult  (net_signal computed)
        all_fits        : dict  action → BlockFitResult  (every block incl. bg)
    """
    if config is None:
        config = ProcessingConfig()

    if not ms.signals:
        return {}, {}

    t_ref = _compute_t_ref(ms)

    # --- collect unique actions -----------------------------------------
    all_actions = list(dict.fromkeys(r.action for r in ms.signals))  # preserves order
    bg_actions  = [a for a in all_actions if _is_background(a)]
    meas_actions = [a for a in all_actions if _is_measurement(a)]

    # --- fit every block -----------------------------------------------
    fits: Dict[str, BlockFitResult] = {}
    for action in all_actions:
        rows = ms.signals_for_action(action)
        if rows:
            action_flags = flag_overrides.get(action) if flag_overrides else None
            fits[action] = fit_block(action, rows, t_ref, config,
                                     override_flags=action_flags)

    # --- resolve background for this device ----------------------------
    # Normally exactly one background action per device.
    # If multiple, merge by averaging their fit values at t_ref.
    if len(bg_actions) == 0:
        bg_fit: Optional[BlockFitResult] = None
    elif len(bg_actions) == 1:
        bg_fit = fits.get(bg_actions[0])
    else:
        bg_vals = _finite([fits[a].value_at_ref for a in bg_actions if a in fits])
        bg_uncs = _finite([fits[a].value_unc    for a in bg_actions if a in fits])
        n_outliers_bg = sum(fits[a].n_outliers for a in bg_actions if a in fits)
        if bg_vals:
            bg_fit = BlockFitResult(
                action="+".join(sorted(bg_actions)),
                n_points=sum(fits[a].n_points for a in bg_actions if a in fits),
                n_outliers=n_outliers_bg,
                t_ref=t_ref,
                value_at_ref=_mean(bg_vals),
                value_unc=(
                    math.sqrt(sum(u ** 2 for u in bg_uncs)) / len(bg_uncs)
                    if bg_uncs else float("nan")
                ),
                is_background=True,
            )
        else:
            bg_fit = None

    # --- build IsotopeResult for each measurement action ---------------
    results: Dict[str, IsotopeResult] = {}
    for action in meas_actions:
        meas_fit = fits.get(action)
        iso = IsotopeResult(
            isotope=action,
            device=ms.device,
            meas_fit=meas_fit,
            bg_fit=bg_fit,
        )
        _apply_background_subtraction(iso)
        results[action] = iso

    return results, fits


def _apply_background_subtraction(iso: IsotopeResult) -> None:
    """Fill iso.net_signal / net_unc in-place."""
    mf = iso.meas_fit
    if mf is None or math.isnan(mf.value_at_ref):
        return

    meas_v = mf.value_at_ref
    meas_u = mf.value_unc   # already 0.0 if NaN was returned

    bf = iso.bg_fit
    if bf is None or math.isnan(bf.value_at_ref):
        iso.net_signal = meas_v
        iso.net_unc = meas_u
    else:
        bg_v = bf.value_at_ref
        bg_u = bf.value_unc
        iso.net_signal = meas_v - bg_v
        iso.net_unc = math.sqrt(meas_u ** 2 + bg_u ** 2)


# ---------------------------------------------------------------------------
# Polynomial helpers (used by drift / blank-interpolation steps)
# ---------------------------------------------------------------------------

def _akima_build(xs: List[float], ys: List[float]):
    """
    Build an Akima1DInterpolator from (xs, ys) sorted by x.

    Returns (interp_fn, r_squared) where interp_fn(t) clamps t to
    [xs[0], xs[-1]] before evaluating — no extrapolation outside the
    bracketed range.  Falls back to linear polyfit when fewer than 3
    unique x-values are present or scipy is unavailable.
    """
    import numpy as np
    pairs = sorted(zip(xs, ys), key=lambda p: p[0])
    xp = np.array([p[0] for p in pairs])
    yp = np.array([p[1] for p in pairs])

    if len(xp) < 3:
        coeffs, cov, r2 = _polyfit(xs, ys, degree=min(1, len(xs) - 1))
        return (lambda t: _polyval(coeffs, t)), r2

    try:
        from scipy.interpolate import Akima1DInterpolator
        ak = Akima1DInterpolator(xp, yp)
        t_lo, t_hi = float(xp[0]), float(xp[-1])

        def _eval(t: float, _ak=ak, _lo=t_lo, _hi=t_hi) -> float:
            return float(_ak(max(_lo, min(_hi, t))))

        yhat = np.array([_eval(x) for x in xs])
        ss_res = float(np.sum((yp - yhat) ** 2))
        mean_y = float(np.mean(yp))
        ss_tot = float(np.sum((yp - mean_y) ** 2))
        r2 = (1.0 - ss_res / ss_tot) if ss_tot > 1e-30 else float("nan")
        return _eval, r2

    except ImportError:
        log.warning("scipy not available — Akima falls back to linear polyfit")
        coeffs, cov, r2 = _polyfit(xs, ys, degree=1)
        return (lambda t: _polyval(coeffs, t)), r2


def _polyfit(
    xs: List[float],
    ys: List[float],
    degree: int,
) -> Tuple[List[float], Optional[List[List[float]]], float]:
    """
    Fit a polynomial of *degree* through (xs, ys) using numpy.
    Returns (coeffs, cov, r_squared) where coeffs is highest-degree first
    (numpy polyfit convention) and cov is the covariance matrix of coefficients.
    """
    try:
        import numpy as np
    except ImportError:
        mean_y = sum(ys) / len(ys) if ys else 0.0
        return [mean_y], None, float("nan")

    n = len(xs)
    if n < 2:
        mean_y = float(np.mean(ys)) if n else 0.0
        return [mean_y], None, float("nan")

    actual_deg = min(degree, n - 1)
    # np.RankWarning moved to np.exceptions.RankWarning in NumPy 2.x;
    # resolve once so the simplefilter call doesn't raise AttributeError.
    _RankWarning = getattr(np, "RankWarning", None) or getattr(
        getattr(np, "exceptions", None), "RankWarning", Warning
    )
    import warnings as _warnings
    try:
        with _warnings.catch_warnings():
            _warnings.simplefilter("ignore", _RankWarning)
            if n > actual_deg + 1:
                try:
                    coeffs, cov = np.polyfit(xs, ys, actual_deg, cov=True)
                    cov_list = cov.tolist() if cov is not None else None
                except (np.linalg.LinAlgError, ValueError):
                    # cov computation fails when x-values are identical or
                    # nearly collinear — fit without covariance
                    coeffs = np.polyfit(xs, ys, actual_deg)
                    cov_list = None
            else:
                coeffs = np.polyfit(xs, ys, actual_deg)
                cov_list = None

        coeffs = coeffs.tolist()
        ys_fit = np.polyval(coeffs, xs)
        ss_res = float(np.sum((np.array(ys) - ys_fit) ** 2))
        mean_y = float(np.mean(ys))
        ss_tot = float(np.sum((np.array(ys) - mean_y) ** 2))
        r2 = (1.0 - ss_res / ss_tot) if ss_tot > 1e-30 else float("nan")
        return coeffs, cov_list, r2
    except Exception as e:
        log.debug(f"polyfit degree={actual_deg} n={n} failed: {e}")
        mean_y = float(np.mean(ys))
        return [mean_y], None, float("nan")


def _reject_calibration_outliers(
    xs: List[float],
    ys: List[float],
    config: "ProcessingConfig",
) -> Tuple[List[float], List[float], List[bool]]:
    """
    Sigma-clip outliers from a blank or drift calibration series.

    Always uses median + MAD regardless of the configured outlier_method.
    MAD is inherently robust: the median and MAD are not inflated by the
    very outlier being detected, unlike mean/std or a linear reference fit.

    NOT appropriate for linearity data (where high-signal standards
    legitimately have different sensitivities — use no outlier removal there).

    Returns (clean_xs, clean_ys, outlier_mask).  Always keeps ≥ 2 points.
    Requires n ≥ 3 to attempt rejection.
    """
    n = len(xs)
    outlier_mask = [False] * n
    method = (config.outlier_method or "none").lower()
    if method == "none" or n < 3:
        return list(xs), list(ys), outlier_mask

    threshold = config.nsigma_threshold
    try:
        import numpy as np
        ys_arr = np.array(ys, dtype=float)

        med = float(np.median(ys_arr))
        mad = float(np.median(np.abs(ys_arr - med)))
        if mad < 1e-30:
            # Fall back to std when all values are identical or MAD is zero
            mad = float(np.std(ys_arr, ddof=1)) / 1.4826 if n > 1 else 1e-30
        scale = mad / 0.6745          # normalise MAD → σ-equivalent
        if scale < 1e-30:
            return list(xs), list(ys), outlier_mask

        for i, y in enumerate(ys):
            if abs(y - med) > threshold * scale:
                outlier_mask[i] = True

    except ImportError:
        return list(xs), list(ys), outlier_mask

    # Never reject all or all-but-one points
    n_clean = sum(1 for f in outlier_mask if not f)
    if n_clean < 2:
        return list(xs), list(ys), [False] * n

    clean_xs = [x for x, f in zip(xs, outlier_mask) if not f]
    clean_ys = [y for y, f in zip(ys, outlier_mask) if not f]
    return clean_xs, clean_ys, outlier_mask


def _aicc_select_poly(
    xs: List[float],
    ys: List[float],
    candidates: List[Tuple[str, int]],
) -> Tuple[str, List[float], Optional[List[List[float]]], float]:
    """
    Pick the best polynomial model from *candidates* using AICc.

    candidates : [(name, degree), …] e.g.
        [("mean",0), ("linear",1), ("quadratic",2), ("cubic",3)]

    Returns (chosen_name, coeffs, cov, r_squared).
    Falls back to the first feasible candidate when no model has enough
    points to compute a valid AICc (requires n ≥ k + 2).
    """
    try:
        import numpy as np
    except ImportError:
        name, degree = candidates[0]
        coeffs, cov, r2 = _polyfit(xs, ys, degree=degree)
        return name, coeffs, cov, r2

    n = len(xs)
    best_name: str = ""
    best_coeffs: List[float] = []
    best_cov: Optional[List[List[float]]] = None
    best_r2: float = float("nan")
    best_aicc: float = float("inf")

    for name, degree in candidates:
        actual_deg = min(degree, max(0, n - 1))
        k = actual_deg + 1          # number of free parameters
        if n < k + 2:               # AICc denominator n-k-1 must be > 0
            continue
        coeffs, cov, r2 = _polyfit(xs, ys, degree=actual_deg)
        if not coeffs:
            continue
        if len(coeffs) != actual_deg + 1:
            # _polyfit returned degenerate fallback (mean) for a higher-degree request
            continue
        ys_fit = np.polyval(coeffs, xs)
        rss = float(np.sum((np.array(ys) - ys_fit) ** 2))
        if rss <= 0.0:
            aicc = -1e30            # perfect fit — accept unconditionally
        else:
            aicc = (
                n * math.log(rss / n)
                + 2.0 * k
                + 2.0 * k * (k + 1) / (n - k - 1)
            )
        if aicc < best_aicc:
            best_aicc, best_name = aicc, name
            best_coeffs, best_cov, best_r2 = coeffs, cov, r2

    if not best_name:
        # Too few points for AICc — fall back to lowest feasible degree
        for name, degree in candidates:
            actual_deg = min(degree, max(0, n - 1))
            if n >= actual_deg + 1:
                coeffs, cov, r2 = _polyfit(xs, ys, degree=actual_deg)
                if coeffs and len(coeffs) == actual_deg + 1:
                    return name, coeffs, cov, r2
        # Ultimate fallback: forced degree-0 mean (all higher fits failed)
        mean_y = sum(ys) / len(ys) if ys else 0.0
        se_y = _se(ys) if len(ys) > 1 else 0.0
        return "none", [mean_y], [[se_y ** 2]], float("nan")

    return best_name, best_coeffs, best_cov, best_r2


def _polyval(coeffs: List[float], x: float) -> float:
    """Evaluate polynomial at x using Horner's method (coeffs: high→low)."""
    result = 0.0
    for c in coeffs:
        result = result * x + c
    return result


def _polyval_with_unc(
    coeffs: List[float],
    cov: Optional[List[List[float]]],
    x: float,
) -> Tuple[float, float]:
    """Evaluate polynomial and its prediction uncertainty (standard error) at x."""
    val = _polyval(coeffs, x)
    if not cov:
        return val, float("nan")
    try:
        import numpy as np
        d = len(coeffs) - 1
        v = np.array([x**(d - i) for i in range(d + 1)])
        cov_arr = np.array(cov)
        var = float(np.dot(v, np.dot(cov_arr, v)))
        unc = math.sqrt(var) if var >= 0 else 0.0
        return val, unc
    except Exception:
        return val, float("nan")


def _expfit(
    xs: List[float],
    ys: List[float],
) -> Tuple[List[float], Optional[List[List[float]]], float]:
    """
    Fit y = a · exp(b · x) via log-transform OLS.
    Returns ([a, b], cov_log, r_squared) where cov_log is the covariance of the
    log-transformed linear fit. Falls back to linear polynomial when any
    y ≤ 0 (log transform is undefined); returns ([], None, nan) on total failure.
    """
    if not xs or not ys:
        return [], None, float("nan")
    if any(y <= 0 for y in ys):
        log.debug("_expfit: non-positive y values — falling back to linear")
        coeffs, cov, r2 = _polyfit(xs, ys, degree=1)
        return coeffs, cov, r2
    try:
        import numpy as np
        log_ys = [math.log(y) for y in ys]
        # fit log(y) = log(a) + b·x.  In polyfit, the coefficients returned are [b, log_a]
        coeffs_log, cov_log, r2 = _polyfit(xs, log_ys, degree=1)
        b = coeffs_log[0]
        a = math.exp(coeffs_log[1])
        return [a, b], cov_log, r2
    except Exception as e:
        logging.warning(f"Exception caught in _expfit: {e}")
        return [], None, float("nan")


def _expval(coeffs: List[float], x: float) -> float:
    """Evaluate y = a · exp(b · x).  coeffs = [a, b]."""
    if len(coeffs) < 2:
        return float("nan")
    return coeffs[0] * math.exp(coeffs[1] * x)


def _expval_with_unc(
    coeffs: List[float],
    cov_log: Optional[List[List[float]]],
    x: float,
) -> Tuple[float, float]:
    """Evaluate y = a · exp(b · x) and its prediction uncertainty using the delta method."""
    if len(coeffs) < 2:
        return float("nan"), float("nan")
    a, b = coeffs[0], coeffs[1]
    y = a * math.exp(b * x)
    if not cov_log:
        return y, float("nan")
    try:
        import numpy as np
        # coeffs_log is [b, log_a]
        # so prediction log(y) = b * x + log_a
        v = np.array([x, 1.0])
        cov_arr = np.array(cov_log)
        var_log = float(np.dot(v, np.dot(cov_arr, v)))
        # delta method: var(y) = (dy/dlogy)^2 * var(logy) = y^2 * var_log
        var = (y ** 2) * var_log
        unc = math.sqrt(var) if var >= 0 else 0.0
        return y, unc
    except Exception:
        return y, float("nan")


def _akima_unc_estimate(xs: List[float], uncs: List[float], x: float) -> float:
    """Estimate uncertainty at x for Akima fit by interpolating input uncertainties."""
    pairs = [(xv, uv) for xv, uv in zip(xs, uncs) if not math.isnan(xv) and not math.isnan(uv)]
    if not pairs:
        return float("nan")
    if len(pairs) == 1:
        return pairs[0][1]
    pairs = sorted(pairs, key=lambda p: p[0])
    xp = [p[0] for p in pairs]
    up = [p[1] for p in pairs]
    try:
        import numpy as np
        return float(np.interp(x, xp, up))
    except Exception:
        return sum(up) / len(up)



# ---------------------------------------------------------------------------
# Inlet classification
# ---------------------------------------------------------------------------

def _auto_bg_proxy_alpha(
    seq: "ProtocolSequence",
) -> Tuple[Optional[float], int, Optional[float]]:
    """
    Estimate the 4He/3He background scaling factor α from blank-inlet SMS data.

    For each blank inlet the function reads the pre-measurement background
    blocks on both the 3He (SEM) and 4He (Faraday) channels and computes the
    ratio bg_4He / bg_3He.  The median over all valid blank inlets is returned
    as the empirical α so the proxy correction is grounded in measured data
    rather than a fixed default.

    Returns
    -------
    (alpha, n, std)
        alpha : median ratio across valid blanks; None when no blanks qualify.
        n     : number of blank inlets that contributed.
        std   : sample std-dev of per-inlet ratios; None when n < 2.
    """
    try:
        from ngam_sms_parser import parse_sms
        from ngam_signal_fitter import is_bg_unreliable
        import numpy as _np
    except ImportError:
        return None, 0, None

    ratios: List[float] = []
    for prep in seq.inlets:
        if not (prep.is_blank or "blank" in prep.inlet_string.lower()):
            continue
        for ms in prep.ms_data:
            if ms.device != "SMS":
                continue
            if not ms.signals:
                continue
            try:
                sms_data = (
                    parse_sms(ms.resolved_path)
                    if ms.resolved_path and os.path.isfile(ms.resolved_path)
                    else _sms_inlet_from_signals(ms)
                )
            except Exception:
                continue

            bg4 = sms_data.he4_bg_sig
            bg3 = sms_data.he3_bg_sig
            if not bg4 or not bg3:
                continue
            if is_bg_unreliable(bg4, sms_data.he4_bg_t, "4He"):
                continue
            if is_bg_unreliable(bg3, sms_data.he3_bg_t, "3He"):
                continue

            mean4 = float(_np.mean(bg4))
            mean3 = float(_np.mean(bg3))
            if abs(mean3) < 1e-20 or mean4 <= 0:
                continue

            ratios.append(mean4 / mean3)

    if not ratios:
        return None, 0, None

    arr = _np.array(ratios)
    std = float(_np.std(arr, ddof=1)) if len(arr) > 1 else None
    return float(_np.median(arr)), len(ratios), std


def _classify_inlet(
    prep: InletPrep,
    repro_ref_seqs: Optional[set] = None,
) -> str:
    if prep.is_blank or "blank" in prep.inlet_string.lower():
        return "blank"
    if prep.is_reference and prep.reference_amount > 0:
        return "standard"
    if repro_ref_seqs and prep.seq_num in repro_ref_seqs:
        return "standard"
    return "sample"


# ---------------------------------------------------------------------------
# Signal-fitter integration helper
# ---------------------------------------------------------------------------

def _sms_inlet_from_signals(ms: "MSMeasurementData") -> "SMSInletData":
    """Build an SMSInletData from pre-parsed signals (e.g. Helix SFT bridge data).

    Used when ms.resolved_path is a sentinel string rather than a real file, so
    parse_sms() cannot be called but ms.signals already holds the cycle data.
    """
    from ngam_sms_parser import SMSInletData
    he3 = [(r.lv_time, r.signal) for r in ms.signals if r.action == "3He"]
    he4 = [(r.lv_time, r.signal) for r in ms.signals if r.action == "4He"]
    bg3 = [(r.lv_time, r.signal) for r in ms.signals if r.action == "3HeBackGround"]
    bg4 = [(r.lv_time, r.signal) for r in ms.signals if r.action == "4HeBackGround"]
    t0 = he4[0][0] if he4 else (he3[0][0] if he3 else 0.0)
    return SMSInletData(
        filepath=ms.original_path or "",
        inlet_num=ms.inlet_num,
        description=ms.original_path or "",
        t0_lv=t0,
        he3_t=[t - t0 for t, _ in he3],
        he3_sig=[s for _, s in he3],
        he4_t=[t - t0 for t, _ in he4],
        he4_sig=[s for _, s in he4],
        he3_bg_t=[t - t0 for t, _ in bg3],
        he3_bg_sig=[s for _, s in bg3],
        he4_bg_t=[t - t0 for t, _ in bg4],
        he4_bg_sig=[s for _, s in bg4],
    )


def _apply_signal_fitter(
    ms: "MSMeasurementData",
    ir: "InletProcessingResult",
    config: "ProcessingConfig",
    device_isotopes: dict,
    seq_num: int,
    fit_model_overrides: Optional[Dict[int, Dict[str, str]]] = None,
) -> None:
    """
    Replace block-level net_signal / net_unc in *ir* with values from the
    signal fitter (AICc-optimal extrapolation to t = 0).

    Called once per device per inlet, only when config.signal_fit_model != "block".
    Silently skips on import errors or parse failures so the block-level
    fallback remains in ir.isotopes unchanged.
    """
    try:
        fit_model = config.signal_fit_model
        dead_time_tau = config.dead_time_tau
        bg_proxy_mode = getattr(config, "bg_proxy_mode", "always")
        
        if not hasattr(ms, "bg_proxy_choices"):
            ms.bg_proxy_choices = {}

        if ms.device == "SMS":
            from ngam_sms_parser import parse_sms
            from ngam_signal_fitter import fit_inlet, select_auto_model as _auto
            if os.path.isfile(ms.resolved_path):
                sms_data = parse_sms(ms.resolved_path)
            else:
                sms_data = _sms_inlet_from_signals(ms)
            for isotope in ("3He", "4He"):
                key = ir.iso_key("SMS", isotope)
                if isotope not in device_isotopes:
                    continue
                # Resolve override model if present
                model = fit_model
                if fit_model_overrides and seq_num in fit_model_overrides:
                    if key in fit_model_overrides[seq_num]:
                        model = fit_model_overrides[seq_num][key]
                sfr = fit_inlet(sms_data, isotope, selected_model=model,
                                dead_time_tau=dead_time_tau,
                                bg_proxy_map=config.bg_proxy_map,
                                bg_proxy_factors=config.bg_proxy_factors,
                                bg_proxy_mode=bg_proxy_mode)
                chosen = _auto(sfr) if model == "Auto" else model
                fr = sfr.fits.get(chosen)
                if fr and fr.success and not math.isnan(fr.value_at_t0):
                    iso = ir.isotopes[key]
                    iso.net_signal = fr.value_at_t0
                    iso.net_unc    = fr.uncertainty if not math.isnan(fr.uncertainty) else 0.0
                    iso.signal_fit_model = chosen
                    iso.bg_used = getattr(sfr, 'bg_used', 'original')
                    ms.bg_proxy_choices[isotope] = sfr.bg_used
                    log.debug(
                        "Inlet %d SMS %s: block net=%.4e → fitter(%s) net=%.4e (bg=%s)",
                        seq_num, isotope,
                        fr.value_at_t0, chosen, fr.value_at_t0, sfr.bg_used
                    )
        else:
            from ngam_qms_parser import parse_qms
            from ngam_signal_fitter import fit_qms_isotope, select_auto_model as _auto
            qms_data = parse_qms(ms.resolved_path)
            for isotope in list(qms_data.signals.keys()):
                key = ir.iso_key(ms.device, isotope)
                if isotope not in device_isotopes:
                    continue
                # Resolve override model if present
                model = fit_model
                if fit_model_overrides and seq_num in fit_model_overrides:
                    if key in fit_model_overrides[seq_num]:
                        model = fit_model_overrides[seq_num][key]
                sfr = fit_qms_isotope(qms_data, isotope, selected_model=model,
                                      bg_proxy_map=config.bg_proxy_map,
                                      bg_proxy_factors=config.bg_proxy_factors,
                                      bg_proxy_mode=bg_proxy_mode)
                chosen = _auto(sfr) if model == "Auto" else model
                fr = sfr.fits.get(chosen)
                if fr and fr.success and not math.isnan(fr.value_at_t0):
                    iso = ir.isotopes[key]
                    iso.net_signal = fr.value_at_t0
                    iso.net_unc    = fr.uncertainty if not math.isnan(fr.uncertainty) else 0.0
                    iso.signal_fit_model = chosen
                    iso.bg_used = getattr(sfr, 'bg_used', 'original')
                    ms.bg_proxy_choices[isotope] = sfr.bg_used
    except Exception:
        log.exception(
            "Signal fitter failed for inlet %d device %s — using block-level values",
            seq_num, ms.device,
        )


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def _std_ok(seq_num: int, iso_key: str,
            excluded: Optional[Dict[int, Optional[Set[str]]]]) -> bool:
    """Return True if this standard inlet is NOT excluded for iso_key.

    excluded[seq_num] = None  → exclude from every isotope
    excluded[seq_num] = set() → exclude only for listed isotope keys
    """
    if not excluded or seq_num not in excluded:
        return True
    entry = excluded[seq_num]
    return entry is not None and iso_key not in entry


def _is_force_included(seq_num: int, iso_key: str,
                       force: Optional[Dict[int, Optional[Set[str]]]]) -> bool:
    """Return True if this inlet is force-included for iso_key (overrides auto-outlier)."""
    if not force or seq_num not in force:
        return False
    entry = force[seq_num]
    return entry is None or iso_key in entry


def _compute_gauge_concentrations(
    inlet_results: List["InletProcessingResult"],
    gauge_summary: Optional["GaugeSequenceSummary"],
    seq: Optional["ProtocolSequence"] = None,
    excluded_standards: Optional[Dict[int, Optional[Set[str]]]] = None,
    result: Optional["SequenceProcessingResult"] = None,
    config: Optional["ProcessingConfig"] = None,
    inlet_times: Optional[Dict[int, float]] = None,
    blank_fit_overrides: Optional[Dict[str, str]] = None,
    drift_fit_overrides: Optional[Dict[str, str]] = None,
) -> None:
    """
    Compute He, Ne, Ar concentrations from SRG net signals and store in
    ir.gauge_conc / ir.gauge_conc_unc for every inlet in *inlet_results*.

    When *result* and *config* are provided, polynomial blank and drift fits
    are computed and stored as result.blank_fits["He_gauge"] etc., enabling
    the same interactive blank/drift override controls as regular isotopes.
    Fit model selection respects config.blank_fit_overrides / drift_fit_overrides
    keyed by "He_gauge", "Ne_gauge", "Ar_gauge".

    Signal sources (priority order):
      1. InletState plateau net from gauge phase computation.
      2. Protocol log step pressure (srg_he_step etc.).
      3. Full-window channel mean fallback.
    """
    inlets_by_seq: Dict[int, Any] = {}
    if seq is not None:
        inlets_by_seq = {prep.seq_num: prep for prep in seq.inlets}
    gauge_by_seq: Dict[int, Any] = {ig.seq_num: ig for ig in gauge_summary.inlets} if gauge_summary else {}

    ELEMENT_DEFS = [
        ("He", lambda p: p.srg_he_step, "SRGHeNe"),
        ("Ne", lambda p: p.srg_ne_net,  "SRGHeNe"),
        ("Ar", lambda p: p.srg_ar_net,  "SRGAr"),
    ]

    def _get_signal(ir, getter_fn, channel, element):
        ig = gauge_by_seq.get(ir.seq_num)
        if ig:
            ph = ig.gauge_phases.get(element)
            if ph is not None and not math.isnan(ph.net):
                return ph.net
        prep = inlets_by_seq.get(ir.seq_num)
        if prep is not None:
            val = getter_fn(prep)
            if not math.isnan(val):
                return val
        if ig and channel in ig.channels:
            m = ig.channels[channel].mean
            if not math.isnan(m):
                return m
        return float("nan")

    def _time_of(ir: Any) -> float:
        if inlet_times is not None:
            return inlet_times.get(ir.seq_num, float("nan"))
        prep = inlets_by_seq.get(ir.seq_num)
        if prep is not None:
            return float(getattr(prep, 'lv_time_start', float("nan")))
        return float("nan")

    _bdeg_map = {"linear": 1, "quadratic": 2, "cubic": 3}
    _ddeg_map = {"mean": 0, "linear": 1, "quadratic": 2, "cubic": 3}
    _poly_modes = [("mean", 0), ("linear", 1), ("quadratic", 2), ("cubic", 3)]

    for element, getter_fn, channel in ELEMENT_DEFS:
        gauge_key = f"{element}_gauge"
        signals: Dict[int, float] = {
            ir.seq_num: _get_signal(ir, getter_fn, channel, element)
            for ir in inlet_results
        }

        # ── Blank fit ────────────────────────────────────────────────────────
        blank_irs = [ir for ir in inlet_results
                     if ir.inlet_type == "blank"
                     and not math.isnan(signals.get(ir.seq_num, float("nan")))]
        btimes_g = [_time_of(ir) for ir in blank_irs]
        bsigs_g  = [signals[ir.seq_num] for ir in blank_irs]
        bseqs_g  = [ir.seq_num for ir in blank_irs]
        blank_mean_fb = sum(bsigs_g) / len(bsigs_g) if bsigs_g else 0.0

        eval_blank_g: Any = None
        if btimes_g and config is not None and result is not None \
                and not all(math.isnan(t) for t in btimes_g):
            bmode_g = ((blank_fit_overrides or {}).get(gauge_key)
                       or config.blank_interpolation or "auto")
            b_deg_g = _bdeg_map.get(bmode_g, 0)
            _bc_t, _bc_s, _b_omask_g = _reject_calibration_outliers(btimes_g, bsigs_g, config)
            if bmode_g == "auto":
                _, coeffs_bg, cov_bg, _ = _aicc_select_poly(_bc_t, _bc_s, _poly_modes)
            elif bmode_g not in ("mean", "akima") and len(_bc_t) >= max(2, b_deg_g + 1):
                coeffs_bg, cov_bg, _ = _polyfit(_bc_t, _bc_s, degree=b_deg_g)
            else:
                mean_bg = sum(_bc_s) / len(_bc_s) if _bc_s else float("nan")
                se_bg = _se(_bc_s) if len(_bc_s) > 1 else 0.0
                coeffs_bg = [mean_bg]
                cov_bg = [[se_bg ** 2]] if not math.isnan(se_bg) else [[0.0]]
            eval_blank_g = lambda t, _c=coeffs_bg, _cv=cov_bg: _polyval_with_unc(_c, _cv, t)
            _b_resids_g: List[float] = []
            for _t, _s in zip(btimes_g, bsigs_g):
                if math.isnan(_t) or math.isnan(_s):
                    _b_resids_g.append(0.0)
                else:
                    _fv = eval_blank_g(_t)
                    _b_resids_g.append(abs(_s - (_fv[0] if isinstance(_fv, tuple) else _fv)))
            result.blank_fits[gauge_key] = BlankFit(
                isotope_key=gauge_key,
                fit_type=bmode_g,
                degree=b_deg_g,
                coeffs=list(coeffs_bg),
                r_squared=float("nan"),
                blank_times=btimes_g,
                blank_signals=bsigs_g,
                blank_signal_uncs=[0.0] * len(bsigs_g),
                blank_fit_residuals=_b_resids_g,
                blank_seq_nums=bseqs_g,
                cov=cov_bg,
                outlier_mask=_b_omask_g,
                user_excluded_mask=[False] * len(btimes_g),
            )

        def _blank_at(t: float, _eb=eval_blank_g, _fb=blank_mean_fb) -> float:
            if _eb is not None and not math.isnan(t):
                res = _eb(t)
                v = res[0] if isinstance(res, tuple) else float(res)
                return v if not math.isnan(v) else _fb
            return _fb

        # ── Sensitivity / drift fit ──────────────────────────────────────────
        stimes_g: List[float] = []
        ssens_g: List[float] = []
        sseqs_g: List[int] = []
        for ir in inlet_results:
            if ir.inlet_type != "standard":
                continue
            if not _std_ok(ir.seq_num, element, excluded_standards):
                continue
            sig = signals.get(ir.seq_num, float("nan"))
            if math.isnan(sig):
                continue
            cert = ir.reference_amounts.get(element)
            if not cert or cert <= 0:
                continue
            t_ir = _time_of(ir)
            p_bc = sig - _blank_at(t_ir)
            if p_bc <= 0:
                continue
            stimes_g.append(t_ir)
            ssens_g.append(p_bc / cert)
            sseqs_g.append(ir.seq_num)

        if not ssens_g:
            continue

        n_s = len(ssens_g)
        sens_mean_fb = sum(ssens_g) / n_s
        sens_std_fb = math.sqrt(sum((s - sens_mean_fb) ** 2 for s in ssens_g) / n_s) if n_s > 1 else 0.0

        eval_drift_g: Any = None
        if stimes_g and config is not None and result is not None \
                and not all(math.isnan(t) for t in stimes_g):
            dmode_g = ((drift_fit_overrides or {}).get(gauge_key)
                       or config.drift_correction or "auto")
            d_deg_g = _ddeg_map.get(dmode_g, 0)
            _sc_t, _sc_s, _d_omask_g = _reject_calibration_outliers(stimes_g, ssens_g, config)
            if dmode_g == "auto":
                _, coeffs_dg, cov_dg, _ = _aicc_select_poly(_sc_t, _sc_s, _poly_modes)
            elif dmode_g != "mean" and len(_sc_t) >= max(2, d_deg_g + 1):
                coeffs_dg, cov_dg, _ = _polyfit(_sc_t, _sc_s, degree=d_deg_g)
            else:
                mean_dg = sum(_sc_s) / len(_sc_s) if _sc_s else float("nan")
                se_dg = _se(_sc_s) if len(_sc_s) > 1 else 0.0
                coeffs_dg = [mean_dg]
                cov_dg = [[se_dg ** 2]] if not math.isnan(se_dg) else [[0.0]]
            eval_drift_g = lambda t, _c=coeffs_dg, _cv=cov_dg: _polyval_with_unc(_c, _cv, t)
            _d_resids_g: List[float] = []
            for _t, _s in zip(stimes_g, ssens_g):
                if math.isnan(_t) or math.isnan(_s):
                    _d_resids_g.append(0.0)
                else:
                    _fv = eval_drift_g(_t)
                    _d_resids_g.append(abs(_s - (_fv[0] if isinstance(_fv, tuple) else _fv)))
            result.drift_fits[gauge_key] = DriftFit(
                isotope_key=gauge_key,
                fit_type=dmode_g,
                degree=d_deg_g,
                coeffs=list(coeffs_dg),
                r_squared=float("nan"),
                std_times=stimes_g,
                std_sensitivities=ssens_g,
                std_sensitivity_uncs=[0.0] * len(ssens_g),
                std_fit_residuals=_d_resids_g,
                std_seq_nums=sseqs_g,
                cov=cov_dg,
                outlier_mask=_d_omask_g,
                user_excluded_mask=[False] * len(stimes_g),
            )

        # ── Concentrations ───────────────────────────────────────────────────
        for ir in inlet_results:
            sig = signals.get(ir.seq_num, float("nan"))
            if math.isnan(sig):
                continue
            t_ir = _time_of(ir)
            p_bc = sig - _blank_at(t_ir)
            if p_bc <= 0:
                ir.gauge_conc[element] = 0.0
                ir.gauge_conc_unc[element] = 0.0
                continue
            if eval_drift_g is not None and not math.isnan(t_ir):
                res_d = eval_drift_g(t_ir)
                S = res_d[0] if isinstance(res_d, tuple) else float(res_d)
                S_unc = res_d[1] if isinstance(res_d, tuple) else 0.0
                if math.isnan(S) or S <= 0:
                    S, S_unc = sens_mean_fb, sens_std_fb
            else:
                S, S_unc = sens_mean_fb, sens_std_fb
            if math.isnan(S) or S <= 0:
                continue
            c = p_bc / S
            rel_s = S_unc / S if S > 0 and not math.isnan(S_unc) else 0.0
            ir.gauge_conc[element] = c
            ir.gauge_conc_unc[element] = c * math.sqrt(0.02 ** 2 + rel_s ** 2)

        # ── Gauge linearity: S vs TotalP (Baratron) fit ──────────────────────
        _glmode = (config.gauge_linearity_correction
                   if config is not None else "none") or "none"
        if _glmode != "none" and result is not None:
            glin_x: List[float] = []
            glin_s: List[float] = []
            glin_seqs: List[int] = []
            for ir in inlet_results:
                if ir.inlet_type != "standard":
                    continue
                if not _std_ok(ir.seq_num, element, excluded_standards):
                    continue
                sig = signals.get(ir.seq_num, float("nan"))
                if math.isnan(sig):
                    continue
                cert = ir.reference_amounts.get(element)
                if not cert or cert <= 0:
                    continue
                ig = gauge_by_seq.get(ir.seq_num)
                if ig is None:
                    continue
                ch_bar = ig.channels.get("BaratronInlet")
                if ch_bar is None or math.isnan(ch_bar.mean) or ch_bar.mean <= 0:
                    continue
                t_ir = _time_of(ir)
                p_bc = sig - _blank_at(t_ir)
                if p_bc <= 0:
                    continue
                glin_x.append(ch_bar.mean)
                glin_s.append(p_bc / cert)
                glin_seqs.append(ir.seq_num)

            _apply_glin = False
            eval_glin_fn: Any = None
            gl_coeffs: List[float] = []
            gl_cov: List[List[float]] = [[]]
            gl_r2 = float("nan")
            gl_fit_type = "none"
            gl_degree = 0

            if len(glin_x) >= 2:
                _gl_deg_map = {"linear": 1, "quadratic": 2, "cubic": 3}
                gl_degree = _gl_deg_map.get(_glmode, 1)
                if _glmode == "auto":
                    if len(glin_x) >= 3:
                        _gl_chosen, gl_coeffs, gl_cov, gl_r2 = _aicc_select_poly(
                            glin_x, glin_s,
                            [("linear", 1), ("quadratic", 2), ("cubic", 3)])
                    else:
                        _gl_chosen = "linear"
                        gl_coeffs, gl_cov, gl_r2 = _polyfit(glin_x, glin_s, degree=1)
                    gl_degree = _gl_deg_map.get(_gl_chosen, 1)
                    gl_fit_type = f"auto→{_gl_chosen}"
                    _apply_glin = (_gl_chosen != "none")
                elif len(glin_x) >= max(2, gl_degree + 1):
                    gl_coeffs, gl_cov, gl_r2 = _polyfit(glin_x, glin_s, degree=gl_degree)
                    gl_fit_type = _glmode
                    _apply_glin = True
                else:
                    _gm = sum(glin_s) / len(glin_s)
                    _gse = _se(glin_s) if len(glin_s) > 1 else 0.0
                    gl_coeffs = [_gm]; gl_cov = [[_gse ** 2]]
                    gl_fit_type = "none"; gl_degree = 0

                eval_glin_fn = (
                    lambda x, _c=gl_coeffs, _cv=gl_cov: _polyval_with_unc(_c, _cv, x)
                )
                _gl_resids = []
                for _gx, _gs in zip(glin_x, glin_s):
                    _gfv = eval_glin_fn(_gx)
                    _gfs = _gfv[0] if isinstance(_gfv, tuple) else _gfv
                    _gl_resids.append(abs(_gs - _gfs) if not math.isnan(_gfs) else 0.0)

                result.linearity_fits[gauge_key] = LinearityFit(
                    isotope_key=gauge_key,
                    fit_type=gl_fit_type,
                    degree=gl_degree,
                    coeffs=gl_coeffs,
                    r_squared=gl_r2,
                    signal_levels=glin_x,       # TotalP (mbar) per standard
                    sensitivities=glin_s,
                    sensitivity_uncs=[0.0] * len(glin_s),
                    sensitivity_fit_residuals=_gl_resids,
                    std_seq_nums=glin_seqs,
                    cov=gl_cov,
                    user_excluded_mask=[False] * len(glin_seqs),
                )

            if _apply_glin and eval_glin_fn is not None:
                for ir in inlet_results:
                    sig = signals.get(ir.seq_num, float("nan"))
                    if math.isnan(sig):
                        continue
                    ig = gauge_by_seq.get(ir.seq_num)
                    if ig is None:
                        continue
                    ch_bar = ig.channels.get("BaratronInlet")
                    if ch_bar is None or math.isnan(ch_bar.mean) or ch_bar.mean <= 0:
                        continue
                    t_ir = _time_of(ir)
                    p_bc = sig - _blank_at(t_ir)
                    if p_bc <= 0:
                        ir.gauge_conc[element] = 0.0
                        ir.gauge_conc_unc[element] = 0.0
                        continue
                    _gres = eval_glin_fn(ch_bar.mean)
                    S_gl = _gres[0] if isinstance(_gres, tuple) else float(_gres)
                    S_gl_unc = _gres[1] if isinstance(_gres, tuple) else float("nan")
                    if math.isnan(S_gl) or S_gl <= 0:
                        continue
                    c = p_bc / S_gl
                    rel_s = (S_gl_unc / S_gl
                             if not math.isnan(S_gl_unc) and S_gl > 0 else 0.0)
                    ir.gauge_conc[element] = c
                    ir.gauge_conc_unc[element] = c * math.sqrt(0.02 ** 2 + rel_s ** 2)


def process_sequence(
    seq: ProtocolSequence,
    sequence_id: Optional[int] = None,
    config: Optional[ProcessingConfig] = None,
    flag_overrides: Optional[Dict[int, Dict[str, Dict[str, List[bool]]]]] = None,
    fit_model_overrides: Optional[Dict[int, Dict[str, str]]] = None,
    repro_references: Optional[List[ReproReferenceInfo]] = None,
    multi_run_linearity: Optional[List[MultiRunLinearityData]] = None,
    aliquot_volumes: Optional[Dict[int, float]] = None,
    ingrowth_data: Optional[Dict[int, "IngrowthInfo"]] = None,
    extraction_info: Optional[Dict[int, "ExtractionInfo"]] = None,
    excluded_standards: Optional[Dict[int, Optional[Set[str]]]] = None,
    excluded_blanks: Optional[Dict[int, Optional[Set[str]]]] = None,
    force_included_standards: Optional[Dict[int, Optional[Set[str]]]] = None,
    force_included_blanks: Optional[Dict[int, Optional[Set[str]]]] = None,
    blank_fit_overrides: Optional[Dict[str, str]] = None,
    drift_fit_overrides: Optional[Dict[str, str]] = None,
    linearity_fit_overrides: Optional[Dict[str, str]] = None,
    inlet_role_overrides: Optional[Dict[int, str]] = None,
) -> SequenceProcessingResult:
    """
    Run the full data-reduction pipeline on a ProtocolSequence.

    Parameters
    ----------
    seq : ProtocolSequence
        Output of ngam_protocol_parser.parse_protocol().
    sequence_id : int, optional
        Database run ID stored in the result for reference.
    flag_overrides : dict, optional
        ``{inlet_seq_num: {device: {action: [bool, ...]}}}`` — user-checked
        outlier overrides from the interactive results widget.
    repro_references : list of ReproReferenceInfo, optional
        Certified-value metadata for inlets tagged bisreproreference=True.
    multi_run_linearity : list of MultiRunLinearityData, optional
        Historical linearity data gathered from past runs.
    aliquot_volumes : dict, optional
        ``{seq_num: volume_ml}`` for noble-gas concentration conversion (ccSTP/mL).
        Used when ingrowth_data is not provided.
    ingrowth_data : dict, optional
        ``{seq_num: IngrowthInfo}`` for Helix SFT ³He → ³H (TU) conversion.
        When provided, step 10 applies the full ingrowth-corrected TU formula
        instead of the generic concentration formula.  Noble-gas (.protocol)
        runs should leave this None.
    extraction_info : dict, optional
        ``{seq_num: ExtractionInfo}`` for water-extraction noble gas samples.
        When provided, step 11 applies extraction efficiency correction and
        computes ccSTP/g alongside the equilibrium solubility reference value.

    Returns
    -------
    SequenceProcessingResult
    """
    if config is None:
        config = ProcessingConfig()

    result = SequenceProcessingResult(
        sequence_id=sequence_id,
        n_blanks=0, n_standards=0, n_samples=0,
    )

    # Build lookup: seq_num → lv_time_start (needed for interpolation steps)
    inlet_times: Dict[int, float] = {
        p.seq_num: p.lv_time_start for p in seq.inlets
    }

    # Build repro-reference lookup: seq_num → ReproReferenceInfo
    repro_ref_by_seq: Dict[int, ReproReferenceInfo] = {}
    if repro_references:
        for rri in repro_references:
            repro_ref_by_seq[rri.seq_num] = rri
    repro_ref_seqs = set(repro_ref_by_seq.keys()) if repro_references else None

    # ── Auto-compute 4He/3He BG proxy α from blank inlet data ────────────────
    # Runs whenever 4He→3He proxy is configured and mode is not 'off'.
    # The computed α replaces whatever was in config.bg_proxy_factors["4He"]
    # so the correction is data-driven for this specific run.
    if (getattr(config, "bg_proxy_mode", "off") != "off"
            and "4He" in getattr(config, "bg_proxy_map", {})):
        alpha, n_alpha, std_alpha = _auto_bg_proxy_alpha(seq)
        if alpha is not None:
            config.bg_proxy_factors = dict(config.bg_proxy_factors or {})
            config.bg_proxy_factors["4He"] = alpha
            result.auto_bg_proxy_alpha     = alpha
            result.auto_bg_proxy_alpha_n   = n_alpha
            result.auto_bg_proxy_alpha_std = std_alpha
            log.info(
                "Auto BG proxy α = %.4g (n=%d, std=%.4g)",
                alpha, n_alpha, std_alpha or 0.0,
            )

    # ── Steps 1–3: fit + background subtract per inlet/device ────────────────
    inlet_results: List[InletProcessingResult] = []

    for prep in seq.inlets:
        itype = _classify_inlet(prep, repro_ref_seqs)
        is_repro = (prep.seq_num in repro_ref_by_seq)
        is_lin   = (itype == "standard") and not is_repro

        if inlet_role_overrides and prep.seq_num in inlet_role_overrides:
            raw = inlet_role_overrides[prep.seq_num]
            if raw == "repro_ref":
                itype    = "standard"
                is_repro = True
                is_lin   = False
            elif raw == "lin_ref":
                itype    = "standard"
                is_repro = False
                is_lin   = True
            elif raw == "repro_lin_ref":
                # Inlet designated as BOTH drift reference (R) and linearity reference (L)
                itype    = "standard"
                is_repro = True
                is_lin   = True
            else:
                itype    = raw  # "blank" | "standard" | "sample"
                is_repro = False
                is_lin   = (itype == "standard")

        # Resolve reference_amount(s): protocol-declared vs repro-ref certified
        ref_amt = prep.reference_amount or 0.0
        ref_amounts: Dict[str, float] = {}
        ref_uncs: Dict[str, float] = {}

        # Per-isotope amounts: repro_ref (highest priority) → InletPrep.per_isotope_amounts
        if itype == "standard" and prep.seq_num in repro_ref_by_seq:
            rri = repro_ref_by_seq[prep.seq_num]
            for iso_key, (cert_val, cert_unc) in rri.certified_values.items():
                # certifiedvalue is a concentration (cm³/cm³); multiply by the
                # reference gas volume (ccSTP) from the .protocol file to get
                # the absolute isotope amount: S = net / (Ref[ccSTP] × cert_conc)
                ref_amounts[iso_key] = ref_amt * cert_val if ref_amt > 0 else cert_val
                unc = cert_unc if cert_unc is not None and not math.isnan(cert_unc) and cert_unc > 0 else 0.0
                ref_uncs[iso_key] = ref_amt * unc if ref_amt > 0 else unc
                # DB analyte names omit the slash (e.g. "3He4He"); ratio processing
                # looks up the slash key ("3He/4He").  Store both so either works.
                slash_key = _RATIO_NOSLASH_TO_KEY.get(iso_key)
                if slash_key and slash_key not in ref_amounts:
                    ref_amounts[slash_key] = ref_amounts[iso_key]
                    ref_uncs[slash_key] = ref_uncs[iso_key]
            # Fall back to first available certified value if no per-isotope match
            if not ref_amounts and ref_amt <= 0:
                for cv in rri.certified_values.values():
                    ref_amt = cv[0]
                    break

        # Merge per-isotope amounts set directly on InletPrep (e.g. by the Helix SFT
        # bridge for 3He-only calibration, or by enrich_sequence_with_reference_amounts
        # which keys ratios as "3He4He" from nvcspecies).
        # Does not overwrite repro_ref values.
        for iso_key, amt in prep.per_isotope_amounts.items():
            if iso_key not in ref_amounts:
                ref_amounts[iso_key] = amt
            # DB stores ratio species without slash ("3He4He"); ratio processing
            # looks up the slash key ("3He/4He").  Store both so either works.
            slash_key = _RATIO_NOSLASH_TO_KEY.get(iso_key)
            if slash_key and slash_key not in ref_amounts:
                ref_amounts[slash_key] = amt
                if iso_key in ref_uncs:
                    ref_uncs[slash_key] = ref_uncs[iso_key]

        ir = InletProcessingResult(
            seq_num=prep.seq_num,
            inlet_string=prep.inlet_string,
            inlet_type=itype,
            is_repro_ref=is_repro,
            is_lin_ref=is_lin,
            reference_amount=ref_amt,
            reference_amounts=ref_amounts,
            reference_uncs=ref_uncs,
            dilution_factor=getattr(prep, 'dilution_factor', 1.0),
            partition_steps=getattr(prep, 'partition_steps', {"Helium": 0, "Neon": 0, "Argon": 0}),
        )

        for ms in prep.ms_data:
            if ms.resolved_path is None or not ms.signals:
                log.debug(
                    "Inlet %d device %s: no signals — skipped",
                    prep.seq_num, ms.device,
                )
                continue
            dev_flag_ov = None
            if flag_overrides:
                dev_flag_ov = flag_overrides.get(prep.seq_num, {}).get(ms.device)
            device_isotopes, device_fits = process_ms_device(ms, config, dev_flag_ov)
            for isotope, iso in device_isotopes.items():
                ir.isotopes[ir.iso_key(ms.device, isotope)] = iso
            ir.block_fits[ms.device] = device_fits

            # Signal fitter override: replace block-level net_signal with
            # AICc-optimal t=0 extrapolation when signal_fit_model != "block"
            if config.signal_fit_model != "block" and ms.resolved_path:
                _apply_signal_fitter(
                    ms, ir, config,
                    device_isotopes, prep.seq_num,
                    fit_model_overrides=fit_model_overrides,
                )

        inlet_results.append(ir)

        if itype == "blank":
            result.n_blanks += 1
        elif itype == "standard":
            result.n_standards += 1
        else:
            result.n_samples += 1

    result.inlets = inlet_results

    # ── Step 3b: compute raw ratio signals (R = I_num / I_den) per inlet ─────
    for ir in inlet_results:
        devices = {k.split(":", 1)[0] for k in ir.isotopes}
        for device in devices:
            for ratio_name, num_bare, den_bare in NG_RATIOS.get(device, []):
                num_key = f"{device}:{num_bare}"
                den_key = f"{device}:{den_bare}"
                num_iso = ir.isotopes.get(num_key)
                den_iso = ir.isotopes.get(den_key)
                if num_iso is None or den_iso is None:
                    continue
                I_num = num_iso.net_signal
                I_den = den_iso.net_signal
                rr = RatioResult(ratio_name=ratio_name, num_key=num_key, den_key=den_key)
                if not math.isnan(I_num) and not math.isnan(I_den) and I_den != 0:
                    R = I_num / I_den
                    rel_num = num_iso.net_unc / I_num if I_num != 0 and not math.isnan(num_iso.net_unc) else 0.0
                    rel_den = den_iso.net_unc / I_den if I_den != 0 and not math.isnan(den_iso.net_unc) else 0.0
                    rr.raw_ratio = R
                    rr.raw_ratio_unc = R * math.sqrt(rel_num ** 2 + rel_den ** 2)
                ir.ratio_results[ratio_name] = rr

    all_ratio_names: set = {rn for ir in inlet_results for rn in ir.ratio_results}

    # ── Step 4a: compute mean blank net signal per device:isotope key ────────
    all_keys = {k for ir in inlet_results for k in ir.isotopes}
    blank_inlets = [ir for ir in inlet_results if ir.inlet_type == "blank"]

    for key in all_keys:
        blank_nets = [
            ir.isotopes[key].net_signal
            for ir in blank_inlets
            if key in ir.isotopes
            and not math.isnan(ir.isotopes[key].net_signal)
        ]
        blank_net_uncs = [
            ir.isotopes[key].net_unc
            for ir in blank_inlets
            if key in ir.isotopes
            and not math.isnan(ir.isotopes[key].net_unc)
        ]
        if not blank_nets:
            continue

        n_b = len(blank_nets)
        mean_b = sum(blank_nets) / n_b
        if n_b > 1:
            # SE of the blank means
            se_b = (
                math.sqrt(sum((v - mean_b) ** 2 for v in blank_nets) / (n_b - 1))
                / math.sqrt(n_b)
            )
        else:
            se_b = blank_net_uncs[0] if blank_net_uncs else 0.0

        result.blank_means[key] = mean_b
        result.blank_uncs[key] = se_b

    # ── Step 4b: apply blank correction to all inlets ────────────────────────
    for ir in inlet_results:
        for key, iso in ir.isotopes.items():
            gas_type = _get_gas_type(key)
            if gas_type in ("Helium", "Neon"):
                n_steps = ir.partition_steps.get(gas_type, 0)
                matching_blanks = [
                    b for b in blank_inlets
                    if b.partition_steps.get(gas_type, 0) == n_steps
                    and key in b.isotopes
                    and not math.isnan(b.isotopes[key].net_signal)
                ]
                if matching_blanks:
                    blank_nets = [b.isotopes[key].net_signal for b in matching_blanks]
                    blank_net_uncs = [
                        b.isotopes[key].net_unc
                        for b in matching_blanks
                        if not math.isnan(b.isotopes[key].net_unc)
                    ]
                    n_b = len(blank_nets)
                    blank_v = sum(blank_nets) / n_b
                    if n_b > 1:
                        blank_u = (
                            math.sqrt(sum((v - blank_v) ** 2 for v in blank_nets) / (n_b - 1))
                            / math.sqrt(n_b)
                        )
                    else:
                        blank_u = blank_net_uncs[0] if blank_net_uncs else 0.0
                else:
                    blank_v = result.blank_means.get(key, float("nan"))
                    blank_u = result.blank_uncs.get(key, 0.0)
            else:
                blank_v = result.blank_means.get(key, float("nan"))
                blank_u = result.blank_uncs.get(key, 0.0)

            iso.blank_net = blank_v
            iso.blank_unc = blank_u

            net_v = iso.net_signal
            net_u = iso.net_unc if not math.isnan(iso.net_unc) else 0.0

            if not math.isnan(net_v) and not math.isnan(blank_v):
                iso.blank_corrected = net_v - blank_v
                iso.blank_corrected_unc = math.sqrt(net_u ** 2 + blank_u ** 2)
            else:
                iso.blank_corrected = net_v          # use as-is if no blank
                iso.blank_corrected_unc = net_u

    # ── Step 5: compute mean sensitivity from standard inlets ────────────────
    std_inlets = [ir for ir in inlet_results if ir.inlet_type == "standard"]

    for key in all_keys:
        sens_list: List[float] = []
        sens_unc_list: List[float] = []

        for ir in std_inlets:
            if key not in ir.isotopes:
                continue
            # Use per-isotope certified amount when available.
            # Priority: "device:isotope" key (repro refs) → bare "isotope" key
            # (ngreference certified amounts) → scalar reference_amount fallback.
            _species = key.split(":", 1)[1] if ":" in key else key
            ref_for_key = (
                ir.reference_amounts.get(key)
                if key in ir.reference_amounts
                else ir.reference_amounts.get(_species, ir.reference_amount)
            )
            ref_unc_for_key = (
                ir.reference_uncs.get(key)
                if key in ir.reference_uncs
                else ir.reference_uncs.get(_species, 0.0)
            )
            if ref_for_key <= 0:
                continue
            iso = ir.isotopes[key]
            bc = iso.blank_corrected
            bc_u = iso.blank_corrected_unc
            if math.isnan(bc) or bc <= 0:
                continue
            S = bc / ref_for_key
            rel_bc = bc_u / bc if bc != 0 and not math.isnan(bc_u) else 0.0
            rel_ref = ref_unc_for_key / ref_for_key if ref_for_key != 0 and not math.isnan(ref_unc_for_key) else 0.0
            S_u = S * math.sqrt(rel_bc ** 2 + rel_ref ** 2)
            # Store individual sensitivity for ALL standards (drift/linearity display needs it
            # even for user-excluded inlets so they can be shown on the plot as grey markers)
            iso.inlet_sensitivity = S
            iso.inlet_sensitivity_unc = S_u
            # Only non-excluded standards contribute to the mean sensitivity
            if not _std_ok(ir.seq_num, key, excluded_standards):
                continue
            sens_list.append(S)
            sens_unc_list.append(S_u)

        if not sens_list:
            continue

        n_s = len(sens_list)
        mean_s = sum(sens_list) / n_s
        if n_s > 1:
            se_s = (
                math.sqrt(sum((s - mean_s) ** 2 for s in sens_list) / (n_s - 1))
                / math.sqrt(n_s)
            )
        else:
            se_s = sens_unc_list[0] if sens_unc_list else 0.0

        result.sensitivities[key] = mean_s
        result.sensitivity_uncs[key] = se_s

    # ── Step 6: compute ccSTP for every inlet ────────────────────────────────
    for ir in inlet_results:
        for key, iso in ir.isotopes.items():
            S = result.sensitivities.get(key, float("nan"))
            S_u = result.sensitivity_uncs.get(key, 0.0)
            iso.sensitivity = S
            iso.sensitivity_unc = S_u

            bc = iso.blank_corrected
            bc_u = iso.blank_corrected_unc
            if math.isnan(bc) or math.isnan(S) or S <= 0:
                continue

            iso.ccSTP = bc / S
            rel_bc = bc_u / bc if bc != 0 and not math.isnan(bc_u) else 0.0
            rel_S = S_u / S if S != 0 and not math.isnan(S_u) else 0.0
            iso.ccSTP_unc = iso.ccSTP * math.sqrt(rel_bc ** 2 + rel_S ** 2)

    # ── Step 7: blank interpolation ──────────────────────────────────────────
    blank_inlets_for_fit = [ir for ir in inlet_results if ir.inlet_type == "blank"]

    for key in all_keys:
        gas_type = _get_gas_type(key)
        # ALL blanks with valid signal (including user-excluded) — kept for display
        _all_bi = [
            ir for ir in blank_inlets_for_fit
            if key in ir.isotopes and not math.isnan(ir.isotopes[key].net_signal)
        ]
        btimes_all     = [inlet_times.get(ir.seq_num, float("nan")) for ir in _all_bi]
        bsigs_all      = [ir.isotopes[key].net_signal for ir in _all_bi]
        bsig_uncs_all  = [ir.isotopes[key].net_unc for ir in _all_bi]
        bseqs_all      = [ir.seq_num for ir in _all_bi]
        uexcl_mask = [not _std_ok(sn, key, excluded_blanks) for sn in bseqs_all]

        # Fit subset: non-user-excluded only
        blank_inlets_fit = [ir for ir, e in zip(_all_bi, uexcl_mask) if not e]
        btimes_fit = [t for t, e in zip(btimes_all, uexcl_mask) if not e]
        bsigs_fit  = [s for s, e in zip(bsigs_all,  uexcl_mask) if not e]
        bseqs_fit  = [sn for sn, e in zip(bseqs_all, uexcl_mask) if not e]

        default_fit = None
        if btimes_fit:
            _b_clean_t, _b_clean_s, _b_mask = _reject_calibration_outliers(btimes_fit, bsigs_fit, config)
            # Un-flag any blanks the user explicitly force-included
            if force_included_blanks:
                for _fi, _fsn in enumerate(bseqs_fit):
                    if _is_force_included(_fsn, key, force_included_blanks):
                        _b_mask[_fi] = False
                _b_clean_t = [t for t, m in zip(btimes_fit, _b_mask) if not m]
                _b_clean_s = [s for s, m in zip(bsigs_fit, _b_mask) if not m]
            bmode = (blank_fit_overrides or {}).get(key, config.blank_interpolation)
            _blank_degree_map = {"linear": 1, "quadratic": 2, "cubic": 3}
            b_degree = _blank_degree_map.get(bmode, 0)

            if bmode == "auto":
                _b_chosen, coeffs, cov, r2 = _aicc_select_poly(
                    _b_clean_t, _b_clean_s,
                    [("mean", 0), ("linear", 1), ("quadratic", 2), ("cubic", 3)],
                )
                b_degree = _blank_degree_map.get(_b_chosen, 0)
                fit_type = f"auto→{_b_chosen}"
                eval_blank_fn_default = lambda t, _c=coeffs, _cov=cov: _polyval_with_unc(_c, _cov, t)
            elif bmode == "akima" and len(_b_clean_t) >= 3:
                eval_blank_fn_akima, r2 = _akima_build(_b_clean_t, _b_clean_s)
                coeffs, cov, fit_type = [], None, "akima"
                b_degree = 0
                buncs = [
                    ir.isotopes[key].net_unc
                    for ir, m in zip(blank_inlets_fit, _b_mask)
                    if key in ir.isotopes and not math.isnan(ir.isotopes[key].net_unc) and not m
                ]
                eval_blank_fn_default = lambda t, _eval=eval_blank_fn_akima, _bt=_b_clean_t, _bu=buncs: (
                    _eval(t),
                    _akima_unc_estimate(_bt, _bu, t)
                )
            elif bmode != "mean" and bmode != "akima" and len(_b_clean_t) >= max(2, b_degree + 1):
                coeffs, cov, r2 = _polyfit(_b_clean_t, _b_clean_s, degree=b_degree)
                fit_type = bmode
                eval_blank_fn_default = lambda t, _c=coeffs, _cov=cov: _polyval_with_unc(_c, _cov, t)
            else:
                mean_b = sum(_b_clean_s) / len(_b_clean_s)
                se_b = _se(_b_clean_s) if len(_b_clean_s) > 1 else 0.0
                coeffs = [mean_b]
                cov = [[se_b ** 2]] if not math.isnan(se_b) else [[0.0]]
                r2 = float("nan")
                fit_type = "mean"
                b_degree = 0
                eval_blank_fn_default = lambda t, _c=coeffs, _cov=cov: _polyval_with_unc(_c, _cov, t)

            # Compose full outlier_mask spanning all blanks (user-excluded points are not auto-outliers)
            _full_b_mask: List[bool] = []
            _fit_idx = 0
            for _excl in uexcl_mask:
                if _excl:
                    _full_b_mask.append(False)
                else:
                    _full_b_mask.append(_b_mask[_fit_idx] if _fit_idx < len(_b_mask) else False)
                    _fit_idx += 1

            _b_residuals: List[float] = []
            for _t, _s in zip(btimes_all, bsigs_all):
                if math.isnan(_t) or math.isnan(_s):
                    _b_residuals.append(0.0)
                else:
                    _fv = eval_blank_fn_default(_t)
                    _fit_s = _fv[0] if isinstance(_fv, tuple) else _fv
                    _b_residuals.append(abs(_s - _fit_s) if not math.isnan(_fit_s) else 0.0)

            default_fit = BlankFit(
                isotope_key=key,
                fit_type=fit_type,
                degree=b_degree,
                coeffs=coeffs,
                r_squared=r2,
                blank_times=btimes_all,
                blank_signals=bsigs_all,
                blank_signal_uncs=bsig_uncs_all,
                blank_fit_residuals=_b_residuals,
                blank_seq_nums=bseqs_all,
                cov=cov,
                outlier_mask=_full_b_mask,
                user_excluded_mask=uexcl_mask,
            )
            result.blank_fits[key] = default_fit
        else:
            eval_blank_fn_default = lambda t: (float("nan"), 0.0)

        for ir in inlet_results:
            if key not in ir.isotopes:
                continue
            t_inlet = inlet_times.get(ir.seq_num, float("nan"))
            iso = ir.isotopes[key]
            if math.isnan(t_inlet):
                continue

            eval_blank_fn = eval_blank_fn_default
            if gas_type in ("Helium", "Neon"):
                n_steps = ir.partition_steps.get(gas_type, 0)
                matching_blanks = [
                    b for b in blank_inlets_for_fit
                    if b.partition_steps.get(gas_type, 0) == n_steps
                    and key in b.isotopes and not math.isnan(b.isotopes[key].net_signal)
                ]
                if matching_blanks:
                    btimes_spec = [inlet_times.get(b.seq_num, float("nan")) for b in matching_blanks]
                    bsigs_spec = [b.isotopes[key].net_signal for b in matching_blanks]
                    bseqs_spec = [b.seq_num for b in matching_blanks]
                    _b_clean_t, _b_clean_s, _b_mask = _reject_calibration_outliers(btimes_spec, bsigs_spec, config)
                    if force_included_blanks:
                        for _fi, _fsn in enumerate(bseqs_spec):
                            if _is_force_included(_fsn, key, force_included_blanks):
                                _b_mask[_fi] = False
                        _b_clean_t = [t for t, m in zip(btimes_spec, _b_mask) if not m]
                        _b_clean_s = [s for s, m in zip(bsigs_spec, _b_mask) if not m]
                    bmode = (blank_fit_overrides or {}).get(key, config.blank_interpolation)
                    _blank_degree_map = {"linear": 1, "quadratic": 2, "cubic": 3}
                    b_degree = _blank_degree_map.get(bmode, 0)

                    if bmode == "auto":
                        _b_chosen, coeffs, cov, r2 = _aicc_select_poly(
                            _b_clean_t, _b_clean_s,
                            [("mean", 0), ("linear", 1), ("quadratic", 2), ("cubic", 3)],
                        )
                        eval_blank_fn = lambda t, _c=coeffs, _cov=cov: _polyval_with_unc(_c, _cov, t)
                    elif bmode == "akima" and len(_b_clean_t) >= 3:
                        eval_blank_fn_akima, r2 = _akima_build(_b_clean_t, _b_clean_s)
                        buncs = [
                            b.isotopes[key].net_unc
                            for b, m in zip(matching_blanks, _b_mask)
                            if not math.isnan(b.isotopes[key].net_unc) and not m
                        ]
                        eval_blank_fn = lambda t, _eval=eval_blank_fn_akima, _bt=_b_clean_t, _bu=buncs: (
                            _eval(t),
                            _akima_unc_estimate(_bt, _bu, t)
                        )
                    elif bmode != "mean" and bmode != "akima" and len(_b_clean_t) >= max(2, b_degree + 1):
                        coeffs, cov, r2 = _polyfit(_b_clean_t, _b_clean_s, degree=b_degree)
                        eval_blank_fn = lambda t, _c=coeffs, _cov=cov: _polyval_with_unc(_c, _cov, t)
                    else:
                        mean_b = sum(_b_clean_s) / len(_b_clean_s)
                        se_b = _se(_b_clean_s) if len(_b_clean_s) > 1 else 0.0
                        coeffs = [mean_b]
                        cov = [[se_b ** 2]] if not math.isnan(se_b) else [[0.0]]
                        eval_blank_fn = lambda t, _c=coeffs, _cov=cov: _polyval_with_unc(_c, _cov, t)

            res_blank = eval_blank_fn(t_inlet)
            if isinstance(res_blank, tuple):
                interp_blank, interp_blank_unc = res_blank
            else:
                interp_blank, interp_blank_unc = res_blank, float("nan")
            iso.interpolated_blank = interp_blank
            net_v = iso.net_signal
            net_u = iso.net_unc if not math.isnan(iso.net_unc) else 0.0
            blank_u = interp_blank_unc if not math.isnan(interp_blank_unc) else result.blank_uncs.get(key, 0.0)
            if not math.isnan(net_v):
                iso.interpolated_blank_corrected = net_v - interp_blank
                iso.interpolated_blank_corrected_unc = math.sqrt(net_u ** 2 + blank_u ** 2)

    # ── Step 7b: blank interpolation and correction for ratios ───────────────
    # For each ratio R = I_num/I_den, blank-correct using the mixing equation:
    #   a = I_den_blank_interp / I_den_meas
    #   R_bc = (R_meas − a · R_blank) / (1 − a)
    for ratio_name in all_ratio_names:
        rr_ref = next((ir.ratio_results[ratio_name] for ir in inlet_results if ratio_name in ir.ratio_results), None)
        if rr_ref is None:
            continue

        _blank_rr_inlets = [
            ir for ir in blank_inlets_for_fit
            if ratio_name in ir.ratio_results and not math.isnan(ir.ratio_results[ratio_name].raw_ratio)
        ]
        btimes_r = [inlet_times.get(ir.seq_num, float("nan")) for ir in _blank_rr_inlets]
        brats_r  = [ir.ratio_results[ratio_name].raw_ratio for ir in _blank_rr_inlets]

        if btimes_r:
            _br_clean_t, _br_clean_s, _brat_omask = _reject_calibration_outliers(btimes_r, brats_r, config)
            bmode_r = config.blank_interpolation
            _bdeg_map = {"linear": 1, "quadratic": 2, "cubic": 3}
            b_deg_r = _bdeg_map.get(bmode_r, 0)
            if bmode_r == "auto":
                _, coeffs_r, cov_r, _ = _aicc_select_poly(
                    _br_clean_t, _br_clean_s,
                    [("mean", 0), ("linear", 1), ("quadratic", 2), ("cubic", 3)],
                )
            elif bmode_r != "mean" and bmode_r != "akima" and len(_br_clean_t) >= max(2, b_deg_r + 1):
                coeffs_r, cov_r, _ = _polyfit(_br_clean_t, _br_clean_s, degree=b_deg_r)
            else:
                mean_br = sum(_br_clean_s) / len(_br_clean_s) if _br_clean_s else float("nan")
                se_br = _se(_br_clean_s) if len(_br_clean_s) > 1 else 0.0
                coeffs_r = [mean_br]
                cov_r = [[se_br ** 2]] if not math.isnan(se_br) else [[0.0]]
            eval_bratio_fn: Any = lambda t, _c=coeffs_r, _cov=cov_r: _polyval_with_unc(_c, _cov, t)

            _brat_resids: List[float] = []
            for _t, _s in zip(btimes_r, brats_r):
                if math.isnan(_t) or math.isnan(_s):
                    _brat_resids.append(0.0)
                else:
                    _fv = eval_bratio_fn(_t)
                    _brat_resids.append(abs(_s - (_fv[0] if isinstance(_fv, tuple) else _fv)))
            result.blank_fits[ratio_name] = BlankFit(
                isotope_key=ratio_name,
                fit_type=bmode_r,
                degree=b_deg_r,
                coeffs=list(coeffs_r),
                r_squared=float("nan"),
                blank_times=list(btimes_r),
                blank_signals=list(brats_r),
                blank_signal_uncs=[0.0] * len(brats_r),
                blank_fit_residuals=_brat_resids,
                blank_seq_nums=[ir.seq_num for ir in _blank_rr_inlets],
                cov=cov_r,
                outlier_mask=_brat_omask,
                user_excluded_mask=[False] * len(btimes_r),
            )
        else:
            eval_bratio_fn = lambda t: (float("nan"), 0.0)

        for ir in inlet_results:
            rr = ir.ratio_results.get(ratio_name)
            if rr is None:
                continue
            t_inlet = inlet_times.get(ir.seq_num, float("nan"))
            if math.isnan(t_inlet):
                continue

            res_br = eval_bratio_fn(t_inlet)
            R_blank = res_br[0] if isinstance(res_br, tuple) else float(res_br)
            R_blank_unc = res_br[1] if isinstance(res_br, tuple) else 0.0
            rr.blank_ratio = R_blank

            den_iso = ir.isotopes.get(rr.den_key)
            if den_iso is not None:
                I_den_blank = (
                    den_iso.interpolated_blank
                    if not math.isnan(den_iso.interpolated_blank)
                    else den_iso.blank_net
                )
                I_den_meas = den_iso.net_signal
                if not math.isnan(I_den_blank) and not math.isnan(I_den_meas) and I_den_meas != 0:
                    a = I_den_blank / I_den_meas
                else:
                    a = float("nan")
            else:
                a = float("nan")
            rr.blank_fraction = a

            R_meas = rr.raw_ratio
            if not math.isnan(R_meas) and not math.isnan(R_blank) and not math.isnan(a):
                denom = 1.0 - a
                if abs(denom) > 1e-10:
                    rr.blank_corrected = (R_meas - a * R_blank) / denom
                    r_unc = rr.raw_ratio_unc if not math.isnan(rr.raw_ratio_unc) else 0.0
                    rb_unc = R_blank_unc if not math.isnan(R_blank_unc) else 0.0
                    rr.blank_corrected_unc = math.sqrt(
                        (r_unc / denom) ** 2 + (a * rb_unc / denom) ** 2
                    )
            else:
                rr.blank_corrected = R_meas
                rr.blank_corrected_unc = rr.raw_ratio_unc

    # ── Step 8: drift correction ──────────────────────────────────────────────
    # Use R (repro/sensitivity) inlets only; fall back to all standards if none flagged.
    _repro_inlets = [ir for ir in inlet_results if ir.is_repro_ref]
    std_inlets_for_drift = _repro_inlets if _repro_inlets else [ir for ir in inlet_results if ir.inlet_type == "standard"]

    for key in all_keys:
        # ALL standards with valid sensitivity (including user-excluded) — kept for display
        _all_si = [
            ir for ir in std_inlets_for_drift
            if key in ir.isotopes and not math.isnan(ir.isotopes[key].inlet_sensitivity)
        ]
        stimes_all     = [inlet_times.get(ir.seq_num, float("nan")) for ir in _all_si]
        ssens_all      = [ir.isotopes[key].inlet_sensitivity for ir in _all_si]
        ssens_unc_all  = [ir.isotopes[key].inlet_sensitivity_unc for ir in _all_si]
        sseqs_all      = [ir.seq_num for ir in _all_si]
        d_uexcl_mask = [not _std_ok(sn, key, excluded_standards) for sn in sseqs_all]

        # Fit subset: non-user-excluded only
        std_inlets_drift_fit = [ir for ir, e in zip(_all_si, d_uexcl_mask) if not e]
        stimes = [t for t, e in zip(stimes_all, d_uexcl_mask) if not e]
        ssens  = [s for s, e in zip(ssens_all,  d_uexcl_mask) if not e]
        sseqs  = [sn for sn, e in zip(sseqs_all, d_uexcl_mask) if not e]

        if not stimes:
            continue

        # Outlier rejection on sensitivity series before fitting
        _d_clean_t, _d_clean_s, _d_mask = _reject_calibration_outliers(stimes, ssens, config)
        # Un-flag any standards the user explicitly force-included
        if force_included_standards:
            for _fi, _fsn in enumerate(sseqs):
                if _is_force_included(_fsn, key, force_included_standards):
                    _d_mask[_fi] = False
            _d_clean_t = [t for t, m in zip(stimes, _d_mask) if not m]
            _d_clean_s = [s for s, m in zip(ssens, _d_mask) if not m]

        dmode = (drift_fit_overrides or {}).get(key, config.drift_correction)
        _drift_degree_map = {"linear": 1, "quadratic": 2, "cubic": 3}
        d_degree = _drift_degree_map.get(dmode, 1)

        enough = len(_d_clean_t) >= config.min_std_for_drift

        if dmode == "none" or not enough:
            mean_s = sum(_d_clean_s) / len(_d_clean_s)
            se_s = _se(_d_clean_s) if len(_d_clean_s) > 1 else 0.0
            coeffs = [mean_s]
            cov = [[se_s ** 2]] if not math.isnan(se_s) else [[0.0]]
            r2 = float("nan")
            fit_type = "mean"
            d_degree = 0
            eval_drift_fn = lambda t, _c=coeffs, _cov=cov: _polyval_with_unc(_c, _cov, t)
        elif dmode == "auto":
            _d_chosen, coeffs, cov, r2 = _aicc_select_poly(
                _d_clean_t, _d_clean_s,
                [("mean", 0), ("linear", 1), ("quadratic", 2), ("cubic", 3)],
            )
            d_degree = _drift_degree_map.get(_d_chosen, 0)
            fit_type = f"auto→{_d_chosen}"
            eval_drift_fn = lambda t, _c=coeffs, _cov=cov: _polyval_with_unc(_c, _cov, t)
        elif dmode == "akima":
            if len(_d_clean_t) >= 3:
                eval_drift_fn_akima, r2 = _akima_build(_d_clean_t, _d_clean_s)
                coeffs, cov, fit_type, d_degree = [], None, "akima", 0
                suncs = [
                    ir.isotopes[key].inlet_sensitivity_unc
                    for ir, m in zip(std_inlets_drift_fit, _d_mask)
                    if key in ir.isotopes and not math.isnan(ir.isotopes[key].inlet_sensitivity_unc)
                    and not m
                ]
                eval_drift_fn = lambda t, _eval=eval_drift_fn_akima, _st=_d_clean_t, _su=suncs: (
                    _eval(t),
                    _akima_unc_estimate(_st, _su, t)
                )
            else:
                coeffs, cov, r2 = _polyfit(_d_clean_t, _d_clean_s, degree=min(1, len(_d_clean_t) - 1))
                fit_type = "akima→linear (too few stds)"
                d_degree = min(1, len(_d_clean_t) - 1)
                eval_drift_fn = lambda t, _c=coeffs, _cov=cov: _polyval_with_unc(_c, _cov, t)
        elif dmode == "exponential":
            coeffs, cov, r2 = _expfit(_d_clean_t, _d_clean_s)
            if not coeffs:
                mean_s = sum(_d_clean_s) / len(_d_clean_s)
                se_s = _se(_d_clean_s) if len(_d_clean_s) > 1 else 0.0
                coeffs = [mean_s]
                cov = [[se_s ** 2]] if not math.isnan(se_s) else [[0.0]]
                r2 = float("nan")
                fit_type = "mean"
                eval_drift_fn = lambda t, _c=coeffs, _cov=cov: _polyval_with_unc(_c, _cov, t)
            elif len(coeffs) == 2 and cov is not None:
                fit_type = "exponential"
                eval_drift_fn = lambda t, _c=coeffs, _cov=cov: _expval_with_unc(_c, _cov, t)
                d_degree = 0
            else:
                fit_type = "linear (exp. fallback)"
                d_degree = 1
                eval_drift_fn = lambda t, _c=coeffs, _cov=cov: _polyval_with_unc(_c, _cov, t)
        else:
            if len(_d_clean_t) >= d_degree + 1:
                coeffs, cov, r2 = _polyfit(_d_clean_t, _d_clean_s, degree=d_degree)
                fit_type = dmode
            else:
                coeffs, cov, r2 = _polyfit(_d_clean_t, _d_clean_s, degree=len(_d_clean_t) - 1)
                fit_type = f"{dmode} (reduced)"
                d_degree = len(_d_clean_t) - 1
            eval_drift_fn = lambda t, _c=coeffs, _cov=cov: _polyval_with_unc(_c, _cov, t)

        # Compose full outlier_mask spanning all standards (user-excluded not flagged as auto-outlier)
        _full_d_mask: List[bool] = []
        _d_fit_idx = 0
        for _excl in d_uexcl_mask:
            if _excl:
                _full_d_mask.append(False)
            else:
                _full_d_mask.append(_d_mask[_d_fit_idx] if _d_fit_idx < len(_d_mask) else False)
                _d_fit_idx += 1

        _d_residuals: List[float] = []
        for _t, _s in zip(stimes_all, ssens_all):
            if math.isnan(_t) or math.isnan(_s):
                _d_residuals.append(0.0)
            else:
                _fv = eval_drift_fn(_t)
                _fit_s = _fv[0] if isinstance(_fv, tuple) else _fv
                _d_residuals.append(abs(_s - _fit_s) if not math.isnan(_fit_s) else 0.0)

        result.drift_fits[key] = DriftFit(
            isotope_key=key,
            fit_type=fit_type,
            degree=d_degree,
            coeffs=coeffs,
            r_squared=r2,
            std_times=stimes_all,
            std_sensitivities=ssens_all,
            std_sensitivity_uncs=ssens_unc_all,
            std_fit_residuals=_d_residuals,
            std_seq_nums=sseqs_all,
            cov=cov,
            outlier_mask=_full_d_mask,
            user_excluded_mask=d_uexcl_mask,
        )

        for ir in inlet_results:
            if key not in ir.isotopes:
                continue
            t_inlet = inlet_times.get(ir.seq_num, float("nan"))
            iso = ir.isotopes[key]
            if math.isnan(t_inlet):
                continue
            res_drift = eval_drift_fn(t_inlet)
            if isinstance(res_drift, tuple):
                S_drift, S_drift_unc = res_drift
            else:
                S_drift, S_drift_unc = res_drift, float("nan")
            iso.drift_sensitivity = S_drift
            if S_drift <= 0 or math.isnan(S_drift):
                continue

            bc = (
                iso.interpolated_blank_corrected
                if not math.isnan(iso.interpolated_blank_corrected)
                else iso.blank_corrected
            )
            bc_u = (
                iso.interpolated_blank_corrected_unc
                if not math.isnan(iso.interpolated_blank_corrected_unc)
                else iso.blank_corrected_unc
            )
            if math.isnan(bc):
                continue
            rel_bc = bc_u / bc if bc != 0 and not math.isnan(bc_u) else 0.0
            rel_S = S_drift_unc / S_drift if S_drift != 0 and not math.isnan(S_drift_unc) else 0.0
            iso.drift_ccSTP = bc / S_drift
            iso.drift_ccSTP_unc = iso.drift_ccSTP * math.sqrt(rel_bc ** 2 + rel_S ** 2)

    # ── Step 8b: sensitivity and drift correction for ratios ─────────────────
    for ratio_name in all_ratio_names:
        # Per-standard inlet sensitivity: S = R_bc / R_certified
        for ir in std_inlets_for_drift:
            rr = ir.ratio_results.get(ratio_name)
            if rr is None:
                continue
            R_bc = rr.blank_corrected
            if math.isnan(R_bc):
                continue
            R_cert = ir.reference_amounts.get(ratio_name)
            if R_cert is None or R_cert <= 0:
                continue
            S = R_bc / R_cert
            rr.inlet_sensitivity = S
            R_cert_unc = ir.reference_uncs.get(ratio_name, 0.0)
            rel_bc = rr.blank_corrected_unc / R_bc if R_bc != 0 and not math.isnan(rr.blank_corrected_unc) else 0.0
            rel_cert = R_cert_unc / R_cert if R_cert != 0 and not math.isnan(R_cert_unc) else 0.0
            rr.inlet_sensitivity_unc = S * math.sqrt(rel_bc ** 2 + rel_cert ** 2)

        _all_ratio_std = [
            ir for ir in std_inlets_for_drift
            if ratio_name in ir.ratio_results and not math.isnan(ir.ratio_results[ratio_name].inlet_sensitivity)
        ]
        if not _all_ratio_std:
            continue

        stimes_r    = [inlet_times.get(ir.seq_num, float("nan")) for ir in _all_ratio_std]
        ssens_r     = [ir.ratio_results[ratio_name].inlet_sensitivity for ir in _all_ratio_std]
        ssens_r_unc = [ir.ratio_results[ratio_name].inlet_sensitivity_unc for ir in _all_ratio_std]
        sseqs_r     = [ir.seq_num for ir in _all_ratio_std]
        rd_uexcl    = [not _std_ok(sn, ratio_name, excluded_standards) for sn in sseqs_r]

        stimes_r_fit = [t for t, e in zip(stimes_r, rd_uexcl) if not e]
        ssens_r_fit  = [s for s, e in zip(ssens_r,  rd_uexcl) if not e]

        if not stimes_r_fit:
            continue

        _dr_clean_t, _dr_clean_s, _dr_mask = _reject_calibration_outliers(stimes_r_fit, ssens_r_fit, config)

        dmode = (drift_fit_overrides or {}).get(ratio_name, config.drift_correction)
        _drift_deg_map = {"linear": 1, "quadratic": 2, "cubic": 3}
        d_deg_r = _drift_deg_map.get(dmode, 1)
        fit_type_r = dmode

        enough_r = len(_dr_clean_t) >= config.min_std_for_drift

        if dmode == "none" or not enough_r:
            mean_sr = sum(_dr_clean_s) / len(_dr_clean_s)
            se_sr = _se(_dr_clean_s) if len(_dr_clean_s) > 1 else 0.0
            coeffs_rd = [mean_sr]; cov_rd = [[se_sr ** 2]] if not math.isnan(se_sr) else [[0.0]]
            fit_type_r = "mean"; d_deg_r = 0
            eval_drift_r: Any = lambda t, _c=coeffs_rd, _cov=cov_rd: _polyval_with_unc(_c, _cov, t)
        elif dmode == "auto":
            _, coeffs_rd, cov_rd, _ = _aicc_select_poly(
                _dr_clean_t, _dr_clean_s,
                [("mean", 0), ("linear", 1), ("quadratic", 2), ("cubic", 3)],
            )
            eval_drift_r = lambda t, _c=coeffs_rd, _cov=cov_rd: _polyval_with_unc(_c, _cov, t)
        elif dmode == "exponential":
            coeffs_rd, cov_rd, _ = _expfit(_dr_clean_t, _dr_clean_s)
            if not coeffs_rd:
                mean_sr = sum(_dr_clean_s) / len(_dr_clean_s)
                se_sr = _se(_dr_clean_s) if len(_dr_clean_s) > 1 else 0.0
                coeffs_rd = [mean_sr]; cov_rd = [[se_sr ** 2]] if not math.isnan(se_sr) else [[0.0]]
                eval_drift_r = lambda t, _c=coeffs_rd, _cov=cov_rd: _polyval_with_unc(_c, _cov, t)
            elif len(coeffs_rd) == 2 and cov_rd is not None:
                eval_drift_r = lambda t, _c=coeffs_rd, _cov=cov_rd: _expval_with_unc(_c, _cov, t)
            else:
                eval_drift_r = lambda t, _c=coeffs_rd, _cov=cov_rd: _polyval_with_unc(_c, _cov, t)
        else:
            deg = d_deg_r if len(_dr_clean_t) >= d_deg_r + 1 else len(_dr_clean_t) - 1
            coeffs_rd, cov_rd, _ = _polyfit(_dr_clean_t, _dr_clean_s, degree=deg)
            eval_drift_r = lambda t, _c=coeffs_rd, _cov=cov_rd: _polyval_with_unc(_c, _cov, t)

        _full_rd_mask: List[bool] = []
        _rd_fit_idx = 0
        for _excl in rd_uexcl:
            if _excl:
                _full_rd_mask.append(False)
            else:
                _full_rd_mask.append(_dr_mask[_rd_fit_idx] if _rd_fit_idx < len(_dr_mask) else False)
                _rd_fit_idx += 1

        _dr_residuals: List[float] = []
        for _t, _s in zip(stimes_r, ssens_r):
            if math.isnan(_t) or math.isnan(_s):
                _dr_residuals.append(0.0)
            else:
                _fv = eval_drift_r(_t)
                _fit_s = _fv[0] if isinstance(_fv, tuple) else _fv
                _dr_residuals.append(abs(_s - _fit_s) if not math.isnan(_fit_s) else 0.0)

        result.drift_fits[ratio_name] = DriftFit(
            isotope_key=ratio_name,
            fit_type=fit_type_r,
            degree=d_deg_r,
            coeffs=coeffs_rd,
            r_squared=float("nan"),
            std_times=stimes_r,
            std_sensitivities=ssens_r,
            std_sensitivity_uncs=ssens_r_unc,
            std_fit_residuals=_dr_residuals,
            std_seq_nums=sseqs_r,
            cov=cov_rd,
            outlier_mask=_full_rd_mask,
            user_excluded_mask=rd_uexcl,
        )

        for ir in inlet_results:
            rr = ir.ratio_results.get(ratio_name)
            if rr is None:
                continue
            t_inlet = inlet_times.get(ir.seq_num, float("nan"))
            if math.isnan(t_inlet):
                continue
            res_dr = eval_drift_r(t_inlet)
            S_dr = res_dr[0] if isinstance(res_dr, tuple) else float(res_dr)
            S_dr_unc = res_dr[1] if isinstance(res_dr, tuple) else float("nan")
            rr.drift_sensitivity = S_dr
            if math.isnan(S_dr) or S_dr <= 0:
                continue
            R_bc = rr.blank_corrected
            if math.isnan(R_bc):
                continue
            rel_bc = rr.blank_corrected_unc / R_bc if R_bc != 0 and not math.isnan(rr.blank_corrected_unc) else 0.0
            rel_S = S_dr_unc / S_dr if S_dr != 0 and not math.isnan(S_dr_unc) else 0.0
            rr.drift_corrected = R_bc / S_dr
            rr.drift_corrected_unc = rr.drift_corrected * math.sqrt(rel_bc ** 2 + rel_S ** 2)

    # ── Step 9: linearity correction ─────────────────────────────────────────
    # Fit sensitivity vs blank-corrected signal through standards.
    # Samples are then calibrated with S(bc_sample) instead of mean S.
    # Use L (linearity) inlets only; fall back to all standards if none flagged.
    _lin_inlets = [ir for ir in inlet_results if ir.is_lin_ref]
    std_inlets_lin = _lin_inlets if _lin_inlets else [ir for ir in inlet_results if ir.inlet_type == "standard"]

    for key in all_keys:
        # ALL standards with valid lin data (including user-excluded) — kept for display
        _all_li = [
            ir for ir in std_inlets_lin
            if key in ir.isotopes
            and not math.isnan(ir.isotopes[key].blank_corrected)
            and not math.isnan(ir.isotopes[key].inlet_sensitivity)
        ]
        sig_all      = [_linearity_x(ir, key, config.linearity_total_load_keys) for ir in _all_li]
        sens_all     = [ir.isotopes[key].inlet_sensitivity for ir in _all_li]
        sens_unc_all = [ir.isotopes[key].inlet_sensitivity_unc for ir in _all_li]
        seqs_all     = [ir.seq_num for ir in _all_li]
        l_uexcl_mask = [not _std_ok(sn, key, excluded_standards) for sn in seqs_all]

        # Fit subset: non-user-excluded only
        sig_levels = [s for s, e in zip(sig_all, l_uexcl_mask) if not e]
        sens_vals  = [s for s, e in zip(sens_all, l_uexcl_mask) if not e]
        lin_seqs   = [sn for sn, e in zip(seqs_all, l_uexcl_mask) if not e]

        if not sig_levels:
            continue

        # Multi-run historical data carries per-isotope x-values; skip when
        # total-load mode is active (x-axis is incompatible across runs).
        if config.linearity_mode == "multi" and multi_run_linearity and not config.linearity_total_load_keys:
            for mr in multi_run_linearity:
                if mr.isotope_key == key:
                    sig_levels.extend(mr.signal_levels)
                    sens_vals.extend(mr.sensitivities)
                    lin_seqs.extend([-rid for rid in mr.run_ids])
                    sig_all.extend(mr.signal_levels)
                    sens_all.extend(mr.sensitivities)
                    seqs_all.extend([-rid for rid in mr.run_ids])
                    l_uexcl_mask.extend([False] * len(mr.run_ids))

        lmode = (linearity_fit_overrides or {}).get(key, config.linearity_correction)
        _lin_deg = {"linear": 1, "quadratic": 2}
        l_degree = _lin_deg.get(lmode, 0)
        _apply_linearity = False

        # Linearity data spans a designed signal range — do NOT apply automatic
        # outlier removal here. High-signal standards with different sensitivities
        # are legitimate nonlinearity signal, not noise outliers. Remove them
        # manually via the Signals tab checkboxes if needed.
        if lmode == "auto":
            _lin_candidates = [("linear", 1), ("quadratic", 2)]
            if len(sig_levels) >= 3:
                _l_chosen, lin_coeffs, lin_cov, lin_r2 = _aicc_select_poly(
                    sig_levels, sens_vals, _lin_candidates)
            elif len(sig_levels) >= 2:
                _l_chosen = "linear"
                lin_coeffs, lin_cov, lin_r2 = _polyfit(sig_levels, sens_vals, degree=1)
            else:
                _l_chosen = "none"
                mean_s_lin = sum(sens_vals) / len(sens_vals)
                se_s_lin = _se(sens_vals) if len(sens_vals) > 1 else 0.0
                lin_coeffs = [mean_s_lin]
                lin_cov = [[se_s_lin ** 2]] if not math.isnan(se_s_lin) else [[0.0]]
                lin_r2 = float("nan")
            l_degree = _lin_deg.get(_l_chosen, 0)
            fit_type = f"auto→{_l_chosen}"
            eval_linearity_fn = lambda bc_val, _c=lin_coeffs, _cov=lin_cov: _polyval_with_unc(_c, _cov, bc_val)
            _apply_linearity = (_l_chosen != "none")
        elif lmode != "none" and len(sig_levels) >= max(2, l_degree + 1):
            lin_coeffs, lin_cov, lin_r2 = _polyfit(sig_levels, sens_vals, degree=l_degree)
            fit_type = lmode
            eval_linearity_fn = lambda bc_val, _c=lin_coeffs, _cov=lin_cov: _polyval_with_unc(_c, _cov, bc_val)
            _apply_linearity = True
        else:
            mean_s_lin = sum(sens_vals) / len(sens_vals)
            se_s_lin = _se(sens_vals) if len(sens_vals) > 1 else 0.0
            lin_coeffs = [mean_s_lin]
            lin_cov = [[se_s_lin ** 2]] if not math.isnan(se_s_lin) else [[0.0]]
            lin_r2 = float("nan")
            fit_type = "none"
            l_degree = 0
            eval_linearity_fn = lambda bc_val, _c=lin_coeffs, _cov=lin_cov: _polyval_with_unc(_c, _cov, bc_val)

        _l_residuals: List[float] = []
        for _x, _s in zip(sig_all, sens_all):
            if math.isnan(_x) or math.isnan(_s):
                _l_residuals.append(0.0)
            else:
                _fv = eval_linearity_fn(_x)
                _fit_s = _fv[0] if isinstance(_fv, tuple) else _fv
                _l_residuals.append(abs(_s - _fit_s) if not math.isnan(_fit_s) else 0.0)

        result.linearity_fits[key] = LinearityFit(
            isotope_key=key,
            fit_type=fit_type,
            degree=l_degree,
            coeffs=lin_coeffs,
            r_squared=lin_r2,
            signal_levels=sig_all,
            sensitivities=sens_all,
            sensitivity_uncs=sens_unc_all,
            sensitivity_fit_residuals=_l_residuals,
            std_seq_nums=seqs_all,
            cov=lin_cov,
            user_excluded_mask=l_uexcl_mask,
        )

        if not _apply_linearity:
            continue

        for ir in inlet_results:
            if key not in ir.isotopes:
                continue
            iso = ir.isotopes[key]
            # bc/bc_u: per-isotope signal — used as numerator in ccSTP = bc / S_lin
            bc = (
                iso.interpolated_blank_corrected
                if not math.isnan(iso.interpolated_blank_corrected)
                else iso.blank_corrected
            )
            bc_u = (
                iso.interpolated_blank_corrected_unc
                if not math.isnan(iso.interpolated_blank_corrected_unc)
                else iso.blank_corrected_unc
            )
            if math.isnan(bc):
                continue
            # x_lin: total-load sum when configured; otherwise same as bc
            x_lin = _linearity_x(ir, key, config.linearity_total_load_keys, prefer_interpolated=True)
            if math.isnan(x_lin):
                continue
            res_lin = eval_linearity_fn(x_lin)
            if isinstance(res_lin, tuple):
                S_lin, S_lin_unc = res_lin
            else:
                S_lin, S_lin_unc = res_lin, float("nan")
            iso.linearity_sensitivity = S_lin
            if S_lin <= 0 or math.isnan(S_lin):
                continue
            rel_bc = bc_u / bc if bc != 0 and not math.isnan(bc_u) else 0.0
            rel_S = S_lin_unc / S_lin if S_lin != 0 and not math.isnan(S_lin_unc) else 0.0
            iso.linearity_ccSTP = bc / S_lin
            iso.linearity_ccSTP_unc = iso.linearity_ccSTP * math.sqrt(rel_bc ** 2 + rel_S ** 2)

    # ── Step 9 (ratio linearity): S_ratio vs denominator signal level ─────────
    # x-axis = denominator blank-corrected signal; total-load is not used here
    # because ratio non-linearity is driven by the denominator detector, not
    # overall source pressure.
    for ratio_name in all_ratio_names:
        _rr_ref = next(
            (ir.ratio_results[ratio_name] for ir in inlet_results
             if ratio_name in ir.ratio_results), None)
        if _rr_ref is None:
            continue
        den_key = _rr_ref.den_key

        _all_rli = [
            ir for ir in std_inlets_lin
            if ratio_name in ir.ratio_results
            and not math.isnan(ir.ratio_results[ratio_name].inlet_sensitivity)
            and den_key in ir.isotopes
        ]
        if not _all_rli:
            continue

        def _rden_x(_ir: Any, _dk: str = den_key) -> float:
            _iso = _ir.isotopes.get(_dk)
            if _iso is None:
                return float("nan")
            return (_iso.interpolated_blank_corrected
                    if not math.isnan(_iso.interpolated_blank_corrected)
                    else _iso.blank_corrected)

        rsig_all      = [_rden_x(ir) for ir in _all_rli]
        rsens_all     = [ir.ratio_results[ratio_name].inlet_sensitivity for ir in _all_rli]
        rsens_unc_all = [ir.ratio_results[ratio_name].inlet_sensitivity_unc for ir in _all_rli]
        rseqs_all     = [ir.seq_num for ir in _all_rli]
        rl_uexcl_mask = [not _std_ok(sn, ratio_name, excluded_standards) for sn in rseqs_all]

        rsig_levels = [s for s, e in zip(rsig_all, rl_uexcl_mask) if not e]
        rsens_vals  = [s for s, e in zip(rsens_all, rl_uexcl_mask) if not e]

        if not rsig_levels:
            continue

        rlmode = (linearity_fit_overrides or {}).get(ratio_name, config.linearity_correction)
        _rl_deg_map = {"linear": 1, "quadratic": 2}
        rl_degree = _rl_deg_map.get(rlmode, 0)
        _apply_rlin = False

        if rlmode == "auto":
            if len(rsig_levels) >= 3:
                _rl_chosen, rlin_coeffs, rlin_cov, rlin_r2 = _aicc_select_poly(
                    rsig_levels, rsens_vals, [("linear", 1), ("quadratic", 2)])
            elif len(rsig_levels) >= 2:
                _rl_chosen = "linear"
                rlin_coeffs, rlin_cov, rlin_r2 = _polyfit(rsig_levels, rsens_vals, degree=1)
            else:
                _rl_chosen = "none"
                _rm = sum(rsens_vals) / len(rsens_vals)
                _rse = _se(rsens_vals) if len(rsens_vals) > 1 else 0.0
                rlin_coeffs = [_rm]; rlin_cov = [[_rse ** 2]]; rlin_r2 = float("nan")
            rl_degree = _rl_deg_map.get(_rl_chosen, 0)
            rfit_type = f"auto→{_rl_chosen}"
            eval_rlin_fn = lambda x, _c=rlin_coeffs, _cov=rlin_cov: _polyval_with_unc(_c, _cov, x)
            _apply_rlin = (_rl_chosen != "none")
        elif rlmode != "none" and len(rsig_levels) >= max(2, rl_degree + 1):
            rlin_coeffs, rlin_cov, rlin_r2 = _polyfit(rsig_levels, rsens_vals, degree=rl_degree)
            rfit_type = rlmode
            eval_rlin_fn = lambda x, _c=rlin_coeffs, _cov=rlin_cov: _polyval_with_unc(_c, _cov, x)
            _apply_rlin = True
        else:
            _rm = sum(rsens_vals) / len(rsens_vals)
            _rse = _se(rsens_vals) if len(rsens_vals) > 1 else 0.0
            rlin_coeffs = [_rm]; rlin_cov = [[_rse ** 2]]; rlin_r2 = float("nan")
            rfit_type = "none"; rl_degree = 0
            eval_rlin_fn = lambda x, _c=rlin_coeffs, _cov=rlin_cov: _polyval_with_unc(_c, _cov, x)

        _rl_residuals: List[float] = []
        for _rx, _rs in zip(rsig_all, rsens_all):
            if math.isnan(_rx) or math.isnan(_rs):
                _rl_residuals.append(0.0)
            else:
                _rfv = eval_rlin_fn(_rx)
                _rfit_s = _rfv[0] if isinstance(_rfv, tuple) else _rfv
                _rl_residuals.append(abs(_rs - _rfit_s) if not math.isnan(_rfit_s) else 0.0)

        result.linearity_fits[ratio_name] = LinearityFit(
            isotope_key=ratio_name,
            fit_type=rfit_type,
            degree=rl_degree,
            coeffs=rlin_coeffs,
            r_squared=rlin_r2,
            signal_levels=rsig_all,      # denominator signal levels
            sensitivities=rsens_all,
            sensitivity_uncs=rsens_unc_all,
            sensitivity_fit_residuals=_rl_residuals,
            std_seq_nums=rseqs_all,
            cov=rlin_cov,
            user_excluded_mask=rl_uexcl_mask,
        )

        if not _apply_rlin:
            continue

        for ir in inlet_results:
            rr = ir.ratio_results.get(ratio_name)
            if rr is None:
                continue
            x_den = _rden_x(ir)
            if math.isnan(x_den):
                continue
            res_rlin = eval_rlin_fn(x_den)
            if isinstance(res_rlin, tuple):
                S_rlin, S_rlin_unc = res_rlin
            else:
                S_rlin, S_rlin_unc = res_rlin, float("nan")
            if S_rlin <= 0 or math.isnan(S_rlin):
                continue
            rr.linearity_sensitivity = S_rlin
            R_bc = rr.blank_corrected
            if math.isnan(R_bc):
                continue
            rel_R = rr.blank_corrected_unc / R_bc if R_bc != 0 and not math.isnan(rr.blank_corrected_unc) else 0.0
            rel_Sl = S_rlin_unc / S_rlin if S_rlin != 0 and not math.isnan(S_rlin_unc) else 0.0
            rr.linearity_corrected = R_bc / S_rlin
            rr.linearity_corrected_unc = rr.linearity_corrected * math.sqrt(rel_R ** 2 + rel_Sl ** 2)

    # ── Step 9b: physical dilution correction ──────────────────────────────
    for ir in inlet_results:
        # Check if there is a manual per-inlet override in config
        override_d = config.dilution_factors.get(ir.seq_num)
        
        if override_d is not None:
            ir.dilution_factor = override_d
        else:
            he_n = ir.partition_steps.get("Helium", 0)
            ne_n = ir.partition_steps.get("Neon", 0)
            he_base = config.get_dilution_factor_for_gas("He")
            ne_base = config.get_dilution_factor_for_gas("Ne")
            he_tot = (he_base ** he_n) if he_n > 0 else 1.0
            ne_tot = (ne_base ** ne_n) if ne_n > 0 else 1.0
            ir.dilution_factor = max(he_tot, ne_tot)
            
        if config.dilution_enabled:
            for key, iso in ir.isotopes.items():
                if override_d is not None:
                    d_iso = override_d
                else:
                    gas = _get_gas_type(key)
                    if gas == "Helium":
                        n = ir.partition_steps.get("Helium", 0)
                        base = config.get_dilution_factor_for_gas("He")
                        d_iso = (base ** n) if n > 0 else 1.0
                    elif gas == "Neon":
                        n = ir.partition_steps.get("Neon", 0)
                        base = config.get_dilution_factor_for_gas("Ne")
                        d_iso = (base ** n) if n > 0 else 1.0
                    else:
                        d_iso = 1.0
                        
                if d_iso != 1.0 and not math.isnan(d_iso) and d_iso > 0:
                    if not math.isnan(iso.ccSTP):
                        iso.ccSTP *= d_iso
                        iso.ccSTP_unc *= d_iso
                    if not math.isnan(iso.drift_ccSTP):
                        iso.drift_ccSTP *= d_iso
                        iso.drift_ccSTP_unc *= d_iso
                    if not math.isnan(iso.linearity_ccSTP):
                        iso.linearity_ccSTP *= d_iso
                        iso.linearity_ccSTP_unc *= d_iso

    # ── Step 9c: isotope dilution (IDMS) ──────────────────────────────────
    if config.idms_enabled and config.idms_config:
        for target_isotope, idms_entry in config.idms_config.items():
            spike_iso = idms_entry.get("spike_isotope", "")
            sample_ratio = idms_entry.get("sample_ratio", 0.0)
            spike_per_inlet = idms_entry.get("spike_per_inlet", {})

            for ir in inlet_results:
                if ir.inlet_type != "sample":
                    continue
                spike_labid = spike_per_inlet.get(ir.seq_num)
                if spike_labid is None:
                    continue

                # Find device:isotope keys for both target and spike isotopes.
                # Both must share the same device.
                target_key = None
                spike_key = None
                for key in ir.isotopes:
                    bare = key.split(":", 1)[1] if ":" in key else key
                    if bare == target_isotope:
                        target_key = key
                    elif bare == spike_iso:
                        spike_key = key
                if target_key is None or spike_key is None:
                    continue
                # Validate same device
                if target_key.split(":", 1)[0] != spike_key.split(":", 1)[0]:
                    continue

                iso_target = ir.isotopes[target_key]
                iso_spike = ir.isotopes[spike_key]

                # Use interpolated blank-corrected if available
                bc_t = (
                    iso_target.interpolated_blank_corrected
                    if not math.isnan(iso_target.interpolated_blank_corrected)
                    else iso_target.blank_corrected
                )
                bc_s = (
                    iso_spike.interpolated_blank_corrected
                    if not math.isnan(iso_spike.interpolated_blank_corrected)
                    else iso_spike.blank_corrected
                )
                bc_t_u = (
                    iso_target.interpolated_blank_corrected_unc
                    if not math.isnan(iso_target.interpolated_blank_corrected_unc)
                    else iso_target.blank_corrected_unc
                )
                bc_s_u = (
                    iso_spike.interpolated_blank_corrected_unc
                    if not math.isnan(iso_spike.interpolated_blank_corrected_unc)
                    else iso_spike.blank_corrected_unc
                )
                if math.isnan(bc_t) or math.isnan(bc_s) or bc_t <= 0:
                    continue

                R_m = bc_s / bc_t
                rel_t = bc_t_u / bc_t if bc_t != 0 and not math.isnan(bc_t_u) else 0.0
                rel_s = bc_s_u / bc_s if bc_s != 0 and not math.isnan(bc_s_u) else 0.0
                R_m_unc = R_m * math.sqrt(rel_t ** 2 + rel_s ** 2)

                # Retrieve certified values for this specific spike_labid
                cert = config.spike_certified.get(str(spike_labid))
                if not cert:
                    # Fallback to the top-level values if present
                    A_sp = idms_entry.get("A_spike")
                    A_sp_unc = idms_entry.get("A_spike_unc", 0.0)
                    R_sp = idms_entry.get("R_spike")
                    R_sp_unc = idms_entry.get("R_spike_unc", 0.0)
                else:
                    if isinstance(cert, dict):
                        A_sp = cert.get("A_spike")
                        A_sp_unc = cert.get("A_spike_unc", 0.0)
                        R_sp = cert.get("R_spike")
                        R_sp_unc = cert.get("R_spike_unc", 0.0)
                    else:
                        A_sp = cert[0]
                        A_sp_unc = cert[1]
                        R_sp = cert[2]
                        R_sp_unc = cert[3]

                if A_sp is None or R_sp is None or math.isnan(A_sp) or math.isnan(R_sp):
                    continue

                # Apply IDMS: B_sample = A_spike * (R_spike - R_m) / (R_spike * (R_m - R_sample))
                b_sample, b_sample_unc = _compute_idms(
                    A_spike=A_sp,
                    A_spike_unc=A_sp_unc,
                    R_spike=R_sp,
                    R_spike_unc=R_sp_unc,
                    R_m=R_m, R_m_unc=R_m_unc,
                    R_sample=sample_ratio,
                )

                if b_sample is None or b_sample <= 0:
                    continue

                # Override the sensitivity-based ccSTP with the IDMS result.
                # The MEASURED value is B_sample (target isotope amount).
                iso_target.ccSTP = b_sample
                iso_target.ccSTP_unc = b_sample_unc
                iso_target.signal_fit_model = "IDMS"
                # Drift/linearity ccSTP are also overridden — they represent
                # sensitivity-corrected values that IDMS replaces entirely.
                iso_target.drift_ccSTP = float("nan")
                iso_target.drift_ccSTP_unc = float("nan")
                iso_target.linearity_ccSTP = float("nan")
                iso_target.linearity_ccSTP_unc = float("nan")

    # ── Step 9d: populate ir.ratios from ratio pipeline results ──────────────
    # The ratio pipeline (Steps 3b/7b/8b) computes ratios in signal space using
    # the proper mixing-equation blank correction. Use the best available value.
    if config.compute_ratios:
        for ir in inlet_results:
            ir.ratios = {}
            for ratio_name, rr in ir.ratio_results.items():
                if not math.isnan(rr.linearity_corrected):
                    val, unc = rr.linearity_corrected, rr.linearity_corrected_unc
                elif not math.isnan(rr.drift_corrected):
                    val, unc = rr.drift_corrected, rr.drift_corrected_unc
                elif not math.isnan(rr.blank_corrected):
                    val, unc = rr.blank_corrected, rr.blank_corrected_unc
                elif not math.isnan(rr.raw_ratio):
                    val, unc = rr.raw_ratio, rr.raw_ratio_unc
                else:
                    continue
                ir.ratios[ratio_name] = (val, unc)
                # Back-annotate onto individual isotope results for display
                num_iso = ir.isotopes.get(rr.num_key)
                den_iso = ir.isotopes.get(rr.den_key)
                if num_iso is not None:
                    num_iso.ratios[ratio_name] = (val, unc)
                if den_iso is not None:
                    den_iso.ratios[ratio_name] = (val, unc)

    # ── Step 10: activity / concentration conversion ──────────────────────
    #
    # Two distinct paths, selected by which optional dict is provided:
    #
    # A. ingrowth_data provided  (Helix SFT ³He → ³H, TU)
    #    Converts ccSTP of tritiogenic ³He to Tritium Units using:
    #
    #      n_³He  = ccSTP × Nₐ / Vmolar
    #      n_³H₀  = n_³He / (1 − e^(−λ·t))    ← ingrowth correction
    #      TU     = n_³H₀ / n_H × 10¹⁸
    #      n_H    = 2 × water_g / M_H₂O × Nₐ   ← H atoms in post-degassing water
    #
    #    Applied only to "3He" isotope keys; 4He and all other isotopes in
    #    the same run are left untouched (activity/concentration remain NaN).
    #
    # B. aliquot_volumes provided, no ingrowth_data  (noble-gas .protocol)
    #    concentration [ccSTP/mL] = ccSTP / vol_ml  for every isotope.
    #    This is the correct path for Ne, Xe, Kr, Ar, 4He from NobleControl.
    #
    if config.compute_activity:
        lambda_3h = math.log(2) / (config.tritium_half_life_years * _SECONDS_PER_YEAR)
        atoms_per_ccstp = _AVOGADRO / _MOLAR_VOLUME_STP

        for ir in inlet_results:
            if ir.inlet_type != "sample":
                continue

            for key, iso in ir.isotopes.items():
                # Pick best available ccSTP (drift > linearity > basic)
                c   = iso.ccSTP
                c_u = iso.ccSTP_unc
                if not math.isnan(iso.drift_ccSTP):
                    c, c_u = iso.drift_ccSTP, iso.drift_ccSTP_unc
                elif not math.isnan(iso.linearity_ccSTP):
                    c, c_u = iso.linearity_ccSTP, iso.linearity_ccSTP_unc
                if math.isnan(c) or c <= 0:
                    continue
                rel_u = c_u / c if c != 0 and not math.isnan(c_u) else 0.0

                # ── Path A: ³He ingrowth → TU ──────────────────────────
                if ingrowth_data is not None and "3He" in key:
                    info = ingrowth_data.get(ir.seq_num)
                    if info is None:
                        log.debug("Inlet %d: no IngrowthInfo — TU skipped", ir.seq_num)
                        continue
                    if info.water_mass_after_g <= 0:
                        log.warning(
                            "Inlet %d: water_mass_after_g=%g <= 0 — TU skipped",
                            ir.seq_num, info.water_mass_after_g,
                        )
                        continue
                    if info.t_ingrowth_seconds <= 0:
                        log.warning(
                            "Inlet %d: t_ingrowth_seconds=%g <= 0 — TU skipped",
                            ir.seq_num, info.t_ingrowth_seconds,
                        )
                        continue

                    from ngam_ingrowth_processor import (
                        ccstp_to_tritium_activity_from_seconds,
                    )
                    try:
                        iso.activity, iso.activity_unc = (
                            ccstp_to_tritium_activity_from_seconds(
                                c, c_u,
                                info.t_ingrowth_seconds,
                                info.water_mass_after_g,
                            )
                        )
                    except ValueError:
                        log.warning(
                            "Inlet %d: TU conversion failed", ir.seq_num,
                        )

                # ── Path B: noble-gas concentration ────────────────────
                elif ingrowth_data is None and aliquot_volumes:
                    vol_ml = aliquot_volumes.get(ir.seq_num)
                    if vol_ml is None or vol_ml <= 0:
                        continue
                    iso.concentration     = c / vol_ml
                    iso.concentration_unc = iso.concentration * rel_u

    # ── Step 11: extraction efficiency + dissolved concentration (water samples)
    #
    # Applied when extraction_info is provided — typically for samples that
    # passed through the NG Extraction line before being measured by SMS/QMS.
    #
    # For each sample inlet that has an ExtractionInfo entry:
    #   a. Pick the best available ccSTP (linearity > drift > basic).
    #   b. Correct for extraction efficiency:
    #        ccSTP_true = ccSTP_measured / η          (η = 1.0 if not measured)
    #   c. Normalise by water mass:
    #        ccSTP_per_g = ccSTP_true / water_mass_g
    #   d. Look up equilibrium solubility at (T, S, P) from ngam_solubility:
    #        c_eq = C_eq(isotope, T_c, S_ppt, P_atm)  [cm³ STP / g H₂O]
    #      stored for QC display; not subtracted from the data.
    #
    if extraction_info:
        try:
            from ngam_solubility import (
                isotope_cm3_per_g, pressure_from_altitude,
            )
            _solubility_ok = True
        except ImportError:
            log.warning("ngam_solubility not available — solubility reference skipped")
            _solubility_ok = False

        for ir in inlet_results:
            if ir.inlet_type != "sample":
                continue
            info = extraction_info.get(ir.seq_num)
            if info is None:
                continue
            if info.water_mass_g <= 0:
                log.warning(
                    "Inlet %d: water_mass_g=%g ≤ 0 — extraction correction skipped",
                    ir.seq_num, info.water_mass_g,
                )
                continue

            p_atm = (
                pressure_from_altitude(info.altitude_m)
                if _solubility_ok and info.altitude_m is not None
                else 1.0
            )

            for key, iso in ir.isotopes.items():
                # Pick best available ccSTP
                c   = iso.ccSTP
                c_u = iso.ccSTP_unc
                if not math.isnan(iso.linearity_ccSTP):
                    c, c_u = iso.linearity_ccSTP, iso.linearity_ccSTP_unc
                elif not math.isnan(iso.drift_ccSTP):
                    c, c_u = iso.drift_ccSTP, iso.drift_ccSTP_unc
                if math.isnan(c):
                    continue

                # Resolve η: per-element table > scalar fallback > 1.0
                # key format: "device:isotope" e.g. "QMSNe:20Ne"
                isotope_label = key.split(":", 1)[-1]
                element = next(
                    (el for el in ("He", "Ne", "Ar", "Kr", "Xe")
                     if isotope_label.endswith(el)),
                    None,
                )
                if element and element in info.element_efficiency:
                    eta = info.element_efficiency[element]
                    eta_unc = info.element_efficiency_unc.get(element)
                else:
                    eta = info.extraction_efficiency if info.extraction_efficiency else 1.0
                    eta_unc = None
                eta = max(eta, 1e-6)   # guard against division by zero

                iso.extraction_efficiency = eta
                iso.ccSTP_true     = c / eta
                # Propagate η uncertainty when available:
                # u(ccSTP_true)² = (u_c/η)² + (c·u_η/η²)²
                if not math.isnan(c_u):
                    var = (c_u / eta) ** 2
                    if eta_unc is not None:
                        var += (c * eta_unc / (eta * eta)) ** 2
                    iso.ccSTP_true_unc = math.sqrt(var)
                else:
                    iso.ccSTP_true_unc = float("nan")
                iso.ccSTP_per_g    = iso.ccSTP_true / info.water_mass_g
                iso.ccSTP_per_g_unc = (
                    iso.ccSTP_true_unc / info.water_mass_g
                    if not math.isnan(iso.ccSTP_true_unc) else float("nan")
                )

                # Equilibrium solubility reference
                if _solubility_ok and info.temperature_c is not None:
                    iso.c_eq_cm3_per_g = isotope_cm3_per_g(
                        isotope_label,
                        info.temperature_c,
                        info.salinity_ppt,
                        p_atm,
                    )

    # ── Step 11b: cross-inlet isotope ratios ──────────────────────────────
    # Cross-inlet ratios only apply when numerator and denominator isotopes
    # are measured in DIFFERENT inlets — i.e. they are never co-measured.
    # If the two isotopes appear together in any sample inlet, the
    # within-inlet ratio (Step 9d) already covers that pair and cross-inlet
    # is unnecessary.
    if config.compute_ratios:
        result.cross_ratios = {}
        sample_inlets = [ir for ir in inlet_results if ir.inlet_type == "sample"]

        for device, ratio_defs in NG_RATIOS.items():
            for ratio_name, num_iso, den_iso in ratio_defs:
                # Check whether these two isotopes are ever co-measured
                co_measured = False
                for ir in sample_inlets:
                    if ir.iso_key(device, num_iso) in ir.isotopes and ir.iso_key(device, den_iso) in ir.isotopes:
                        co_measured = True
                        break
                if co_measured:
                    continue  # within-inlet Step 9d already handles this

                num_entries: list = []   # (seq_num, ccSTP, unc, method)
                den_entries: list = []
                for ir in sample_inlets:
                    num_key = ir.iso_key(device, num_iso)
                    den_key = ir.iso_key(device, den_iso)
                    if num_key in ir.isotopes:
                        iso = ir.isotopes[num_key]
                        if not math.isnan(iso.ccSTP_per_g):
                            num_entries.append((ir.seq_num, iso.ccSTP_per_g, iso.ccSTP_per_g_unc, "ccSTP_per_g"))
                        else:
                            c, u = _pick_best_ccstp(iso)
                            if not math.isnan(c):
                                num_entries.append((ir.seq_num, c, u, "ccSTP_best"))
                    if den_key in ir.isotopes:
                        iso = ir.isotopes[den_key]
                        if not math.isnan(iso.ccSTP_per_g):
                            den_entries.append((ir.seq_num, iso.ccSTP_per_g, iso.ccSTP_per_g_unc, "ccSTP_per_g"))
                        else:
                            c, u = _pick_best_ccstp(iso)
                            if not math.isnan(c):
                                den_entries.append((ir.seq_num, c, u, "ccSTP_best"))

                cross_list: List[CrossInletRatio] = []
                for n_seq, c_num, u_num, n_method in num_entries:
                    for d_seq, c_den, u_den, d_method in den_entries:
                        r, u = _compute_ccstp_ratio(c_num, u_num, c_den, u_den)
                        if not math.isnan(r):
                            cross_list.append(CrossInletRatio(
                                ratio_name=ratio_name,
                                ratio_value=r,
                                ratio_unc=u,
                                num_seq_num=n_seq,
                                den_seq_num=d_seq,
                                method="ccSTP_per_g" if n_method == "ccSTP_per_g" and d_method == "ccSTP_per_g" else "ccSTP_best",
                            ))
                if cross_list:
                    result.cross_ratios[ratio_name] = cross_list

    # ── Gauge summary (SRG / pressure sensors from .InletState) ──────────────
    if seq.inlet_state is not None:
        try:
            from ngam_gauge_processor import compute_gauge_summary
            result.gauge_summary = compute_gauge_summary(
                seq,
                sigma=config.gauge_sigma,
                apply_qc_flags=config.gauge_qc_flags,
                plateau_max_he=config.gauge_plateau_max_he,
                plateau_max_ne=config.gauge_plateau_max_ne,
                plateau_max_ar=config.gauge_plateau_max_ar,
                baseline_max_he=config.gauge_baseline_max_he,
                baseline_max_ne=config.gauge_baseline_max_ne,
                baseline_max_ar=config.gauge_baseline_max_ar,
            )
        except Exception:
            log.exception("Gauge summary computation failed — skipping")

    # ── Gauge concentrations (He, Ne, Ar from SRG + certified amounts) ───────
    # Run even when gauge_summary is None — protocol-derived SRG values don't
    # require InletState data.
    try:
        _compute_gauge_concentrations(
            inlet_results, result.gauge_summary, seq,
            excluded_standards=excluded_standards,
            result=result, config=config, inlet_times=inlet_times,
            blank_fit_overrides=blank_fit_overrides,
            drift_fit_overrides=drift_fit_overrides,
        )
    except Exception:
        log.exception("Gauge concentration computation failed — skipping")

    # ── Gauge ccSTP/g — divide by water mass when extraction info is available ─
    if extraction_info:
        for ir in inlet_results:
            info = extraction_info.get(ir.seq_num)
            if info is None or info.water_mass_g <= 0:
                continue
            wm = info.water_mass_g
            for el, c in ir.gauge_conc.items():
                ir.gauge_conc_per_g[el] = c / wm
                u = ir.gauge_conc_unc.get(el, 0.0)
                ir.gauge_conc_per_g_unc[el] = u / wm

    return result
