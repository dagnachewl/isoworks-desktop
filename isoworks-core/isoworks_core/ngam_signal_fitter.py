"""
ngam_signal_fitter.py
=====================
Background interpolation and curve fitting for Isotopx SMS signals.

Workflow for one isotope within one inlet
-----------------------------------------
1. parse_sms()          → SMSInletData  (ngam_sms_parser)
2. fit_inlet()          → SignalFitResult  (this module)
   - interpolate background at each signal cycle's timestamp
   - subtract background  →  net_signal
   - fit all 5 models against (t_rel, net_signal)
   - evaluate each fitted curve at t=0  →  initial signal + uncertainty

Available models
----------------
"Average"     – mean of net cycles; uncertainty = std / sqrt(n)
"Linear"      – y = a + b·t   (degree-1 polynomial)
"Poly2"       – y = a + b·t + c·t²
"Poly3"       – y = a + b·t + c·t² + d·t³
"Exponential" – y = A·exp(k·t); requires all net signals > 0

Time axis
---------
Raw times are seconds from t0 (= lvTime of first signal row, set by the
parser).  Fits are computed on t_norm = t_rel / t_max_signal so that the
x-range is [0, 1] for the signal cycles, improving numerical stability for
higher-degree polynomials.  Evaluating at t_norm = 0 gives the value at t=0
(extrapolated to measurement start).
"""
from __future__ import annotations

import bisect
import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from ngam_sms_parser import SMSInletData

log = logging.getLogger(__name__)

MODELS: List[str] = ["Auto", "Average", "Linear", "Poly2", "Poly3", "Exponential"]
DEFAULT_MODEL = "Auto"
_BASE_MODELS: List[str] = ["Average", "Linear", "Poly2", "Poly3", "Exponential"]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class FitResult:
    """Fit of one model to a single isotope's net-signal time series."""

    model: str
    value_at_t0: float      # fitted signal extrapolated to t = 0
    uncertainty: float      # standard error of the t=0 estimate
    r_squared: float        # coefficient of determination (nan for Average)
    n_points: int
    coefficients: List[float]
    coef_se: List[float]
    success: bool = True    # False when the fit could not be computed
    message: str = ""       # error description when not success
    aicc: float = float("nan")  # AICc model-selection criterion (lower = better)

    def evaluate(self, t_norm: float) -> float:
        """
        Evaluate the fitted curve at a normalised time.

        Parameters
        ----------
        t_norm : float
            0.0  = measurement start (t = 0 in raw seconds),
            1.0  = last signal cycle.
        """
        if not self.success or math.isnan(self.value_at_t0):
            return float("nan")
        if self.model == "Average":
            return self.coefficients[0]
        if self.model == "Exponential":
            k, A = self.coefficients       # [rate, amplitude]
            return A * math.exp(k * t_norm)
        # Polynomial: numpy convention — highest degree first
        result = 0.0
        for c in self.coefficients:
            result = result * t_norm + c
        return result


@dataclass
class SignalFitResult:
    """Fitting results for one isotope within one inlet."""

    isotope: str            # "3He" or "4He"
    inlet_num: int
    description: str

    # Time arrays (seconds from t0 of the inlet)
    t_rel: List[float]          # per-cycle time of signal measurements
    t_max: float                # = max(t_rel), normalisation divisor

    # Signal arrays (one value per cycle, same length as t_rel)
    raw_signal: List[float]
    bg_interp: List[float]      # background interpolated at each t_rel
    net_signal: List[float]     # raw_signal − bg_interp

    # Outlier detection results (same length as t_rel)
    outlier_flags: List[bool] = field(default_factory=list)
    n_outliers: int = 0

    # Fits for all models
    fits: Dict[str, FitResult] = field(default_factory=dict)
    selected_model: str = DEFAULT_MODEL
    bg_used: str = "original"

    @property
    def selected_fit(self) -> FitResult:
        """Return the fit for the selected model; Auto resolves to best AICc."""
        if self.selected_model == "Auto":
            return self.fits[self.auto_chosen_model]
        return self.fits[self.selected_model]

    @property
    def auto_chosen_model(self) -> str:
        """The model that Auto would (or did) select — best AICc among valid fits."""
        return select_auto_model(self)

    @property
    def t_norm(self) -> List[float]:
        """Normalised time list matching t_rel."""
        if self.t_max == 0.0:
            return [0.0] * len(self.t_rel)
        return [t / self.t_max for t in self.t_rel]

    def curve_points(
        self, model: str, n_pts: int = 200
    ) -> Tuple[List[float], List[float]]:
        """
        Return (t_rel_values, signal_values) for plotting a fitted curve.

        The returned x-axis spans from 0 to t_max so the extrapolation to
        t=0 is always visible alongside the data.
        """
        fit = self.fits.get(model)
        if fit is None or not fit.success:
            return [], []
        t_rels = [self.t_max * i / (n_pts - 1) for i in range(n_pts)]
        t_norms = [tr / self.t_max for tr in t_rels] if self.t_max > 0 else [0.0] * n_pts
        signals = [fit.evaluate(tn) for tn in t_norms]
        return t_rels, signals


# ---------------------------------------------------------------------------
# AICc (Akaike Information Criterion, corrected) and Auto model selection
# ---------------------------------------------------------------------------

def _compute_aicc(n: int, k: int, rss: float) -> float:
    """
    AICc = n·ln(RSS/n) + 2k + 2k(k+1)/(n−k−1)

    Parameters
    ----------
    n   : number of data points used in the fit
    k   : number of free parameters (Average=1, Linear=2, Poly2=3,
          Poly3=4, Exponential=2)
    rss : residual sum of squares

    Returns nan when the correction term is undefined (n ≤ k+1).
    """
    if n <= 0 or rss < 0 or n <= k + 1:
        return float("nan")
    log_term = n * math.log(rss / n)
    penalty = 2 * k
    correction = 2 * k * (k + 1) / (n - k - 1)
    return log_term + penalty + correction


def _is_monotone_decreasing(fr: FitResult) -> bool:
    """
    Physical validity check: a gas signal being pumped away should only
    decrease (or stay flat) within the measurement window [t_norm=0, 1].

    Reject a fit if it reverses upward by more than 5 % of its t=0 value
    anywhere in the window.  Always passes for Average (constant).
    """
    if not fr.success or math.isnan(fr.value_at_t0) or fr.value_at_t0 <= 0:
        return fr.success  # can't evaluate; accept if fit succeeded
    if fr.model == "Average":
        return True
    v0 = fr.value_at_t0
    # Sample 50 points across [0, 1]
    vals = [fr.evaluate(i / 50.0) for i in range(51)]
    max_val = max((v for v in vals if not math.isnan(v)), default=v0)
    return max_val <= v0 * 1.05   # allow 5 % numerical wiggle


def select_auto_model(result: "SignalFitResult") -> str:
    """
    Choose the best model for *result* using AICc.

    Selection rules (in order):
    1. Only consider models whose fits succeeded.
    2. Among successful models, pick the one with the lowest AICc.
    3. Fall back to "Linear" if nothing succeeded or all AICc values are nan.

    Note: no monotonicity constraint is applied here — AICc already penalises
    extra parameters, so over-parameterised models that don't meaningfully
    reduce RSS are naturally disfavoured without needing a physical gate.
    """
    candidates = [m for m in _BASE_MODELS
                  if result.fits.get(m) and result.fits[m].success]
    if not candidates:
        return "Linear"

    def aicc_key(m: str) -> float:
        v = result.fits[m].aicc
        return v if not math.isnan(v) else float("inf")

    return min(candidates, key=aicc_key)


# ---------------------------------------------------------------------------
# Dead-time correction
# ---------------------------------------------------------------------------

def _correct_dead_time(values: List[float], tau: float) -> List[float]:
    """
    Non-paralyzable (Type I) dead-time correction applied to raw SEM counts.

        n_true = n_meas / (1 − n_meas · τ)

    Parameters
    ----------
    values : list of float
        Raw signal values from the SEM/multiplier channel (signal or background).
    tau : float
        Effective dead-time in the SAME unit system as `values`.
        If values are in counts/s (CPS), τ is in seconds.
        If the NobleControl outputs in a scaled unit (e.g. values ≈ 0.1–0.2 for
        a large standard), τ must be scaled accordingly — see ProcessingConfig
        documentation.  Set τ = 0 to disable (default).

    Returns
    -------
    list of float
        Dead-time-corrected values, or the original list unchanged when τ ≤ 0.
    """
    if tau <= 0.0:
        return values
    corrected = []
    for n in values:
        denom = 1.0 - n * tau
        # Guard against saturated detector (n·τ ≥ 1): return uncorrected value.
        corrected.append(n / denom if denom > 1e-9 else n)
    return corrected


def is_bg_unreliable(bg_s: List[float], bg_t: List[float], isotope: str) -> bool:
    """
    Evaluate if the background measurement for an isotope is corrupted or unstable.
    """
    if not bg_s or len(bg_s) < 2:
        return True
    
    # Calculate standard deviation and mean
    std_val = np.std(bg_s)
    mean_val = np.mean(bg_s)
    
    # 1. Extreme scatter/noise check
    # Faraday backgrounds (4He) are around 1e-15 to 1e-13 A.
    # A healthy standard deviation is typically < 1e-15 A.
    # If the standard deviation is > 5e-15 A, it is highly unstable/noisy.
    if isotope == "4He":
        if std_val > 5.0e-15:
            return True
        # Check for negative resistor spikes or clipping
        if any(v < -2.0e-14 for v in bg_s):
            return True
        # Check for extreme positive outlier spikes (e.g. > 1e-12 A when typical is 1e-14)
        if any(v > 1.0e-12 for v in bg_s):
            return True
            
    # 2. General RSD check (only if background is at a significant level)
    if abs(mean_val) > 1.0e-14:
        rsd = std_val / abs(mean_val)
        if rsd > 0.25:  # more than 25% relative variation on background is anomalous
            return True
            
    return False


# Background interpolation
# ---------------------------------------------------------------------------

def _interpolate_bg(
    t_signal: List[float],
    bg_t: List[float],
    bg_sig: List[float],
) -> List[float]:
    """
    Linearly interpolate background at each signal cycle time.

    Times outside the background measurement range are extrapolated flat
    (nearest endpoint).  If no background measurements are available the
    function returns zeros.
    """
    if not bg_t:
        return [0.0] * len(t_signal)

    result: List[float] = []
    for t in t_signal:
        if t <= bg_t[0]:
            result.append(bg_sig[0])
        elif t >= bg_t[-1]:
            result.append(bg_sig[-1])
        else:
            idx = bisect.bisect_right(bg_t, t) - 1
            t0, t1 = bg_t[idx], bg_t[idx + 1]
            s0, s1 = bg_sig[idx], bg_sig[idx + 1]
            frac = (t - t0) / (t1 - t0) if (t1 - t0) > 0 else 0.0
            result.append(s0 + frac * (s1 - s0))
    return result


# ---------------------------------------------------------------------------
# Individual model fitters (all operate on t_norm ∈ [0, 1])
# ---------------------------------------------------------------------------

def _r_squared(y: np.ndarray, y_pred: np.ndarray) -> float:
    ss_res = float(np.sum((y - y_pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    if ss_tot == 0.0:
        return 1.0 if ss_res == 0.0 else 0.0
    return max(0.0, 1.0 - ss_res / ss_tot)


def _failed(model: str, msg: str) -> FitResult:
    return FitResult(
        model=model,
        value_at_t0=float("nan"),
        uncertainty=float("nan"),
        r_squared=float("nan"),
        n_points=0,
        coefficients=[],
        coef_se=[],
        success=False,
        message=msg,
        aicc=float("nan"),
    )


def _fit_average(t_norm: np.ndarray, net: np.ndarray) -> FitResult:
    n = len(net)
    if n == 0:
        return _failed("Average", "no data points")
    mean_val = float(np.mean(net))
    if n > 1:
        std = float(np.std(net, ddof=1))
        se = std / math.sqrt(n)
    else:
        se = float("nan")
    rss = float(np.sum((net - mean_val) ** 2))
    return FitResult(
        model="Average",
        value_at_t0=mean_val,
        uncertainty=se,
        r_squared=float("nan"),
        n_points=n,
        coefficients=[mean_val],
        coef_se=[se],
        aicc=_compute_aicc(n, 1, rss),
    )


def _fit_poly(
    model: str, deg: int, t_norm: np.ndarray, net: np.ndarray
) -> FitResult:
    n = len(net)
    if n < deg + 1:
        return _failed(model, f"need ≥ {deg+1} points for {model}, have {n}")
    try:
        coef, cov = np.polyfit(t_norm, net, deg, cov=True)
    except (np.linalg.LinAlgError, ValueError) as exc:
        return _failed(model, str(exc))

    value_at_t0 = float(coef[-1])         # constant term = value at t_norm=0
    se_t0 = float(math.sqrt(abs(cov[-1, -1])))
    y_pred = np.polyval(coef, t_norm)
    r2 = _r_squared(net, y_pred)
    rss = float(np.sum((net - y_pred) ** 2))
    coef_se = [float(math.sqrt(abs(cov[i, i]))) for i in range(len(coef))]
    k = deg + 1   # number of free parameters

    return FitResult(
        model=model,
        value_at_t0=value_at_t0,
        uncertainty=se_t0,
        r_squared=r2,
        n_points=n,
        coefficients=list(map(float, coef)),
        coef_se=coef_se,
        aicc=_compute_aicc(n, k, rss),
    )


def _fit_exponential(t_norm: np.ndarray, net: np.ndarray) -> FitResult:
    """
    Fit  y = A · exp(k · t_norm)  via log-linearisation.

    Requires all net-signal values to be strictly positive (realistic for
    4He signals; 3He signals on blanks may be near zero and will trigger the
    fallback message).
    """
    n = len(net)
    if n < 2:
        return _failed("Exponential", f"need ≥ 2 points, have {n}")

    if np.any(net <= 0):
        return _failed(
            "Exponential",
            "net signal contains zero / negative values — cannot log-linearise",
        )

    try:
        log_net = np.log(net)
        coef, cov = np.polyfit(t_norm, log_net, 1, cov=True)
    except (np.linalg.LinAlgError, ValueError) as exc:
        return _failed("Exponential", str(exc))

    k = float(coef[0])          # growth rate in normalised time
    log_A = float(coef[1])      # ln(amplitude)
    A = math.exp(log_A)

    # Uncertainty of A via delta method: SE_A ≈ A · SE_log_A
    se_log_A = float(math.sqrt(abs(cov[1, 1])))
    se_A = A * se_log_A

    y_pred = A * np.exp(k * t_norm)
    r2 = _r_squared(net, y_pred)
    rss = float(np.sum((net - y_pred) ** 2))

    return FitResult(
        model="Exponential",
        value_at_t0=A,
        uncertainty=se_A,
        r_squared=r2,
        n_points=n,
        # Store as [k, A] — rate first, amplitude second (matches evaluate())
        coefficients=[k, A],
        coef_se=[float(math.sqrt(abs(cov[0, 0]))), se_A],
        aicc=_compute_aicc(n, 2, rss),
    )


# ---------------------------------------------------------------------------
# Outlier detection
# ---------------------------------------------------------------------------

def _detect_outliers(
    t_norm: np.ndarray,
    net: np.ndarray,
    nsigma: float,
) -> np.ndarray:
    """
    Return a boolean mask (True = outlier) using N-sigma on residuals.

    Strategy: fit a preliminary linear model to remove the signal trend,
    then flag cycles whose residual exceeds nsigma × σ(residuals).
    This avoids penalising the genuine signal decay at early/late times.
    Requires at least 4 points; returns all-False for shorter arrays.
    """
    n = len(net)
    if n < 4 or nsigma <= 0:
        return np.zeros(n, dtype=bool)
    try:
        coef = np.polyfit(t_norm, net, 1)
        residuals = net - np.polyval(coef, t_norm)
    except Exception:
        residuals = net - float(np.mean(net))
    std_r = float(np.std(residuals, ddof=1))
    if std_r == 0:
        return np.zeros(n, dtype=bool)
    return np.abs(residuals) > nsigma * std_r


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fit_inlet(
    sms_data: SMSInletData,
    isotope: str,
    selected_model: str = DEFAULT_MODEL,
    outlier_nsigma: float = 3.0,
    dead_time_tau: float = 0.0,
    bg_proxy_map: Optional[Dict[str, str]] = None,
    bg_proxy_factors: Optional[Dict[str, float]] = None,
    bg_proxy_mode: str = "always",
    forced_outlier_flags: Optional[List[bool]] = None,
) -> SignalFitResult:
    """
    Interpolate backgrounds and fit all models for one isotope in one inlet.

    Parameters
    ----------
    sms_data : SMSInletData
        Parsed SMS file from ngam_sms_parser.parse_sms().
    isotope : str
        ``"3He"`` or ``"4He"``.
    selected_model : str
        Which model to mark as the user's choice (must be in MODELS).
        Use ``"Auto"`` to let AICc pick the best model automatically.
    dead_time_tau : float
        Effective dead-time coefficient τ for the SEM/multiplier channel
        (``"3He"`` only — Faraday is unaffected).  Applied as
        ``n_true = n / (1 − n·τ)`` before background subtraction.
        Units must match those of the raw signal values.  Default 0 (disabled).
    bg_proxy_map : dict, optional
        Map of isotope to proxy isotope (e.g. {"4He": "3He"})
    bg_proxy_factors : dict, optional
        Map of isotope to baseline scaling factor (e.g. {"4He": 100.0})
    bg_proxy_mode : str
        Background proxy mode: "always", "auto", or "off". Default is "always".

    Returns
    -------
    SignalFitResult
        Contains background-corrected data and fit results for all models.
        Switch models via result.selected_model (no re-computation needed).
    """
    if selected_model not in MODELS:
        raise ValueError(f"selected_model must be one of {MODELS}")

    if isotope == "3He":
        t_sig = sms_data.he3_t
        raw   = _correct_dead_time(sms_data.he3_sig, dead_time_tau)
        bg_t  = sms_data.he3_bg_t
        bg_s  = _correct_dead_time(sms_data.he3_bg_sig, dead_time_tau)
    elif isotope == "4He":
        t_sig = sms_data.he4_t
        raw   = sms_data.he4_sig          # Faraday — no dead-time correction
        bg_t  = sms_data.he4_bg_t
        bg_s  = sms_data.he4_bg_sig
    else:
        raise ValueError(f"isotope must be '3He' or '4He', got {isotope!r}")

    # Keep track of original background
    orig_bg_t = bg_t
    orig_bg_s = bg_s

    # -- Resolve proxy background if configured ----------------------------
    proxy_iso = None
    if bg_proxy_map:
        proxy_iso = bg_proxy_map.get(isotope) or bg_proxy_map.get(f"SMS:{isotope}")

    bg_used = "original"
    use_proxy = False

    if proxy_iso and proxy_iso in ("3He", "4He"):
        if bg_proxy_mode == "always":
            use_proxy = True
        elif bg_proxy_mode == "auto":
            if is_bg_unreliable(orig_bg_s, orig_bg_t, isotope):
                use_proxy = True
                log.info(f"Inlet {sms_data.inlet_num}: Original background for {isotope} is unreliable. Falling back to proxy.")
            else:
                log.info(f"Inlet {sms_data.inlet_num}: Original background for {isotope} is reliable. Using original background.")

    if use_proxy and proxy_iso:
        factor = 1.0
        if bg_proxy_factors:
            factor = bg_proxy_factors.get(isotope) or bg_proxy_factors.get(f"SMS:{isotope}") or 1.0
        
        # Load the proxy background
        if proxy_iso == "3He":
            p_bg_t  = sms_data.he3_bg_t
            p_bg_s  = _correct_dead_time(sms_data.he3_bg_sig, dead_time_tau)
        else: # "4He"
            p_bg_t  = sms_data.he4_bg_t
            p_bg_s  = sms_data.he4_bg_sig

        bg_t = p_bg_t
        bg_s = [val * factor for val in p_bg_s]
        bg_used = f"{proxy_iso} (proxy)"
        log.info(f"Inlet {sms_data.inlet_num}: Using {proxy_iso} as background proxy for {isotope} with factor {factor}")

    # -- background interpolation -------------------------------------------
    bg_interp = _interpolate_bg(t_sig, bg_t, bg_s)
    net = [r - b for r, b in zip(raw, bg_interp)]

    # -- prepare arrays for fitting -----------------------------------------
    t_max = max(t_sig) if t_sig else 1.0
    t_arr = np.array(t_sig, dtype=float)
    t_norm = t_arr / t_max if t_max > 0 else t_arr
    net_arr = np.array(net, dtype=float)

    # -- outlier detection on residuals from initial linear fit -------------
    if forced_outlier_flags is not None and len(forced_outlier_flags) == len(net_arr):
        outlier_mask = np.array(forced_outlier_flags, dtype=bool)
    else:
        outlier_mask = _detect_outliers(t_norm, net_arr, outlier_nsigma)
    n_outliers = int(outlier_mask.sum())
    clean_t   = t_norm[~outlier_mask]
    clean_net = net_arr[~outlier_mask]

    # -- compute all fits on clean (non-outlier) data ----------------------
    fits: Dict[str, FitResult] = {
        "Average":     _fit_average(clean_t, clean_net),
        "Linear":      _fit_poly("Linear",      1, clean_t, clean_net),
        "Poly2":       _fit_poly("Poly2",       2, clean_t, clean_net),
        "Poly3":       _fit_poly("Poly3",       3, clean_t, clean_net),
        "Exponential": _fit_exponential(clean_t, clean_net),
    }

    log.debug(
        "fit_inlet inlet=%d %s %s: %d outlier(s) removed; t0 values → %s",
        sms_data.inlet_num, sms_data.description, isotope, n_outliers,
        {m: f"{r.value_at_t0:.4e}" if r.success else "FAIL" for m, r in fits.items()},
    )

    return SignalFitResult(
        isotope=isotope,
        inlet_num=sms_data.inlet_num,
        description=sms_data.description,
        t_rel=list(t_sig),
        t_max=t_max,
        raw_signal=list(raw),
        bg_interp=bg_interp,
        net_signal=net,
        outlier_flags=outlier_mask.tolist(),
        n_outliers=n_outliers,
        fits=fits,
        selected_model=selected_model,
        bg_used=bg_used,
    )


def available_models(result: SignalFitResult) -> List[str]:
    """Return the subset of base models (excluding Auto) that produced a successful fit."""
    return [m for m in _BASE_MODELS if result.fits[m].success]


# ---------------------------------------------------------------------------
# QMS support
# ---------------------------------------------------------------------------

QMS_RATIOS: Dict[str, List[Tuple[str, str, str]]] = {
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


def fit_qms_isotope(
    qms_data: "QMSInletData",
    isotope: str,
    selected_model: str = DEFAULT_MODEL,
    outlier_nsigma: float = 3.0,
    bg_proxy_map: Optional[Dict[str, str]] = None,
    bg_proxy_factors: Optional[Dict[str, float]] = None,
    bg_proxy_mode: str = "always",
    forced_outlier_flags: Optional[List[bool]] = None,
) -> SignalFitResult:
    """
    Interpolate QMS background and fit all models for one isotope.

    Works identically to fit_inlet() but reads from QMSInletData.
    The shared background array is interpolated at each signal cycle time.
    Use ``selected_model="Auto"`` to let AICc pick the best model.
    """
    from ngam_qms_parser import QMSInletData  # noqa: F401 — imported for type check

    if selected_model not in MODELS:
        raise ValueError(f"selected_model must be one of {MODELS}")

    if isotope not in qms_data.isotopes:
        raise ValueError(
            f"Isotope {isotope!r} not in QMSInletData (available: {qms_data.isotopes})"
        )

    t_sig, raw = qms_data.signals[isotope]

    bg_t = qms_data.bg_t
    bg_s = qms_data.bg_sig

    # Keep track of original background
    orig_bg_t = bg_t
    orig_bg_s = bg_s

    # -- Resolve proxy background if configured ----------------------------
    proxy_iso = None
    if bg_proxy_map:
        proxy_iso = bg_proxy_map.get(isotope) or bg_proxy_map.get(f"{qms_data.device}:{isotope}")

    bg_used = "original"
    use_proxy = False

    if proxy_iso and proxy_iso in qms_data.isotopes:
        if bg_proxy_mode == "always":
            use_proxy = True
        elif bg_proxy_mode == "auto":
            if is_bg_unreliable(orig_bg_s, orig_bg_t, isotope):
                use_proxy = True
                log.info(f"Inlet {qms_data.inlet_num} QMS: Original background for {isotope} is unreliable. Falling back to proxy.")
            else:
                log.info(f"Inlet {qms_data.inlet_num} QMS: Original background for {isotope} is reliable. Using original background.")

    if use_proxy and proxy_iso:
        factor = 1.0
        if bg_proxy_factors:
            factor = bg_proxy_factors.get(isotope) or bg_proxy_factors.get(f"{qms_data.device}:{isotope}") or 1.0
        
        bg_s = [val * factor for val in qms_data.bg_sig]
        bg_used = f"{proxy_iso} (proxy)"
        log.info(f"Inlet {qms_data.inlet_num} QMS: Scaling shared background for {isotope} with factor {factor} as proxy")

    bg_interp = _interpolate_bg(t_sig, bg_t, bg_s)
    net = [r - b for r, b in zip(raw, bg_interp)]

    t_max = max(t_sig) if t_sig else 1.0
    t_arr = np.array(t_sig, dtype=float)
    t_norm = t_arr / t_max if t_max > 0 else t_arr
    net_arr = np.array(net, dtype=float)

    if forced_outlier_flags is not None and len(forced_outlier_flags) == len(net_arr):
        outlier_mask = np.array(forced_outlier_flags, dtype=bool)
    else:
        outlier_mask = _detect_outliers(t_norm, net_arr, outlier_nsigma)
    n_outliers = int(outlier_mask.sum())
    clean_t   = t_norm[~outlier_mask]
    clean_net = net_arr[~outlier_mask]

    fits: Dict[str, FitResult] = {
        "Average":     _fit_average(clean_t, clean_net),
        "Linear":      _fit_poly("Linear",      1, clean_t, clean_net),
        "Poly2":       _fit_poly("Poly2",       2, clean_t, clean_net),
        "Poly3":       _fit_poly("Poly3",       3, clean_t, clean_net),
        "Exponential": _fit_exponential(clean_t, clean_net),
    }

    log.debug(
        "fit_qms_isotope inlet=%d %s %s %s: %d outlier(s); t0 values → %s",
        qms_data.inlet_num, qms_data.description, qms_data.device, isotope, n_outliers,
        {m: f"{r.value_at_t0:.4e}" if r.success else "FAIL" for m, r in fits.items()},
    )

    return SignalFitResult(
        isotope=isotope,
        inlet_num=qms_data.inlet_num,
        description=qms_data.description,
        t_rel=list(t_sig),
        t_max=t_max,
        raw_signal=list(raw),
        bg_interp=bg_interp,
        net_signal=net,
        outlier_flags=outlier_mask.tolist(),
        n_outliers=n_outliers,
        fits=fits,
        selected_model=selected_model,
        bg_used=bg_used,
    )


def compute_qms_ratios(
    fits: Dict[str, SignalFitResult],
    device: str,
) -> Dict[str, Tuple[float, float]]:
    """
    Compute isotope ratios from fitted t=0 amplitudes.

    Returns {ratio_name: (value, uncertainty)}.
    Uses QMS_RATIOS table.  Propagates uncertainty as quadrature sum of
    relative errors.  Returns (nan, nan) when either fit failed or
    amplitude is zero/nan.
    """
    result: Dict[str, Tuple[float, float]] = {}
    for ratio_name, num_iso, den_iso in QMS_RATIOS.get(device, []):
        nan_pair: Tuple[float, float] = (float("nan"), float("nan"))

        fr_num = fits.get(num_iso)
        fr_den = fits.get(den_iso)
        if fr_num is None or fr_den is None:
            result[ratio_name] = nan_pair
            continue

        A = fr_num.selected_fit.value_at_t0
        B = fr_den.selected_fit.value_at_t0
        sA = fr_num.selected_fit.uncertainty
        sB = fr_den.selected_fit.uncertainty

        if (
            not fr_num.selected_fit.success
            or not fr_den.selected_fit.success
            or math.isnan(A) or math.isnan(B)
            or B == 0.0
        ):
            result[ratio_name] = nan_pair
            continue

        ratio = A / B
        rel_unc = math.sqrt((sA / A) ** 2 + (sB / B) ** 2) if A != 0.0 else float("nan")
        result[ratio_name] = (ratio, ratio * rel_unc)

    return result


# ---------------------------------------------------------------------------
# Quick-look GUI (run this file directly to test)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import glob
    import os

    from PyQt5.QtWidgets import (
        QApplication, QDialog, QVBoxLayout, QHBoxLayout,
        QComboBox, QLabel, QSplitter,
    )
    from PyQt5.QtCore import Qt
    from ngam_sms_parser import parse_sms_sequence
    from ngam_signal_fit_widget import SMSFitPanel

    app = QApplication(sys.argv)

    # ---- find SMS files next to this script --------------------------------
    here = os.path.dirname(os.path.abspath(__file__))
    sms_files = sorted(glob.glob(
        os.path.join(here, "Data", "Noble Gas", "**", "*.SMS"),
        recursive=True,
    ))
    if not sms_files:
        print("No .SMS files found under Data/Noble Gas/ — provide a path as argv[1]")
        sys.exit(1)

    sequence = parse_sms_sequence(sms_files)

    # ---- main window -------------------------------------------------------
    dlg = QDialog()
    dlg.setWindowTitle("SMS Signal Fit Viewer")
    dlg.resize(1050, 600)
    layout = QVBoxLayout(dlg)

    # inlet + isotope selectors
    sel_row = QHBoxLayout()
    sel_row.addWidget(QLabel("Inlet:"))
    cb_inlet = QComboBox()
    for d in sequence:
        flag = "BLK" if d.is_blank else ("STD" if d.is_standard else "SMP")
        cb_inlet.addItem(f"[{flag}] {d.inlet_num}: {d.description}", d)
    sel_row.addWidget(cb_inlet)
    sel_row.addSpacing(16)
    sel_row.addWidget(QLabel("Isotope:"))
    cb_iso = QComboBox()
    cb_iso.addItems(["4He", "3He"])
    sel_row.addWidget(cb_iso)
    sel_row.addStretch()
    layout.addLayout(sel_row)

    # two panels side-by-side (4He left, 3He right initially)
    splitter = QSplitter(Qt.Horizontal)
    panel_4 = SMSFitPanel()
    panel_3 = SMSFitPanel()
    splitter.addWidget(panel_4)
    splitter.addWidget(panel_3)
    layout.addWidget(splitter, stretch=1)

    def _reload():
        sms_data = cb_inlet.currentData()
        if sms_data is None:
            return
        r4 = fit_inlet(sms_data, "4He", "Auto")
        r3 = fit_inlet(sms_data, "3He", "Auto")
        panel_4.load(r4)
        panel_3.load(r3)

    cb_inlet.currentIndexChanged.connect(lambda _: _reload())
    _reload()

    dlg.exec_()
    sys.exit(0)
