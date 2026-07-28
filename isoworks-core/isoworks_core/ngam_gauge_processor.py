"""
ngam_gauge_processor.py
=======================
Spinning Rotor Gauge (SRG) and pressure sensor summary for one NobleControl
sequence.  Reads the .InletState file (parsed by ngam_protocol_parser),
slices it per inlet, σ-clips outliers, and computes a time-weighted mean ± std
for each pressure channel.

Public API
----------
compute_gauge_summary(seq, sigma, qc_flags) → GaugeSequenceSummary | None

Returns None (silently) when seq.inlet_state is absent or empty, so callers
never need to guard explicitly.
"""
from __future__ import annotations

import math
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Channel definitions
# ---------------------------------------------------------------------------

PRIMARY_CHANNELS = ["SRGHeNe", "SRGAr"]
SECONDARY_CHANNELS = [
    "BaratronInlet",
    "PiraniInletTurbo",
    "PiraniSRG",
    "PiraniQMSAr",
    "PiraniNGSep",
    "PiraniHeNeSep",
]
ALL_CHANNELS = PRIMARY_CHANNELS + SECONDARY_CHANNELS

# Inlet is flagged when its per-channel mean exceeds this multiple of the
# run median (computed across all inlets that have valid data for that channel).
_QC_FLAG_FACTOR = 3.0


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class ChannelSummary:
    """Time-weighted pressure summary for one channel within one inlet."""
    channel: str
    mean: float             # time-weighted mean of surviving readings
    sigma: float            # std dev of surviving readings (simple, not time-weighted)
    n_points: int           # surviving points after σ-clip
    n_outliers: int         # points removed by σ-clip
    # Surviving and outlier points, preserved for plotting
    t_vals: List[float]     # timestamps [s] relative to sequence start
    y_vals: List[float]     # readings (instrument unit)
    t_out: List[float]      # outlier timestamps
    y_out: List[float]      # outlier readings


@dataclass
class GaugePhaseSummary:
    """Baseline / sample split for one SRG element within one inlet."""
    element: str         # "He", "Ne", "Ar"
    channel: str         # "SRGHeNe" or "SRGAr"
    t_baseline: List[float]   # timestamps [s] rel. to sequence start
    v_baseline: List[float]   # signal readings for baseline phase
    t_sample: List[float]     # ALL ToSRG=1 readings (including admission transient)
    v_sample: List[float]
    t_plateau: List[float]    # stable plateau subset (used for net/concentration)
    v_plateau: List[float]
    baseline_mean: float
    baseline_err: float       # std / sqrt(n-1); 0 when n≤1
    sample_mean: float        # mean of ALL sample readings (for reference)
    sample_err: float
    plateau_mean: float       # mean of stable plateau only
    plateau_err: float
    net: float                # plateau_mean - baseline_mean
    net_err: float            # sqrt(baseline_err² + plateau_err²)
    n_baseline: int
    n_sample: int
    n_plateau: int


@dataclass
class InletGaugeSummary:
    """Gauge summary for one inlet of a sequence."""
    seq_num: int
    description: str
    inlet_type: str         # "blank" | "standard" | "sample"
    t_start: float          # absolute lvTime of inlet start
    t_end: float            # absolute lvTime of inlet end
    channels: Dict[str, ChannelSummary] = field(default_factory=dict)
    qc_flags: Dict[str, bool] = field(default_factory=dict)
    # LVT boundaries of the per-element SRG measurement phases (from protocol log)
    srg_windows: Dict[str, List[float]] = field(default_factory=dict)
    # Baseline/sample split for each element (He, Ne, Ar)
    gauge_phases: Dict[str, GaugePhaseSummary] = field(default_factory=dict)
    # True when SRG pressure was anomalously high (see _QC_FLAG_FACTOR)


@dataclass
class ChannelFit:
    """Polynomial fit through per-inlet means for one channel."""
    channel: str
    t_knots: List[float]   # inlet midpoint timestamps [s] relative to seq start
    y_knots: List[float]   # per-inlet time-weighted means at each knot
    degree: int            # polynomial degree actually used
    coeffs: List[float]    # highest-power-first coefficients (numpy convention)
    r_squared: float


@dataclass
class GaugeSequenceSummary:
    """Complete gauge summary for a processed sequence."""
    inlets: List[InletGaugeSummary] = field(default_factory=list)
    channels_available: List[str] = field(default_factory=list)
    # Median of per-inlet means per channel, used as QC reference level
    run_medians: Dict[str, float] = field(default_factory=dict)
    # Full sequence raw time-series (for GaugeSignal-style continuous plots)
    raw_t: List[float] = field(default_factory=list)           # [s] rel. to seq start
    raw_data: Dict[str, List[float]] = field(default_factory=dict)  # ch → readings
    # Polynomial fits through per-inlet means
    channel_fits: Dict[str, ChannelFit] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _time_weighted_mean(t: List[float], y: List[float]) -> float:
    """
    Trapezoidal approximation of ∫y dt / Δt.

    Returns arithmetic mean when fewer than 2 points remain (degenerate).
    """
    n = len(t)
    if n == 0:
        return float("nan")
    if n == 1:
        return y[0]
    total_w = t[-1] - t[0]
    if total_w <= 0:
        return sum(y) / n
    num = sum(
        0.5 * (y[i] + y[i + 1]) * (t[i + 1] - t[i])
        for i in range(n - 1)
    )
    return num / total_w


def _sigma_clip(
    t: List[float],
    y: List[float],
    sigma: float,
) -> Tuple[List[float], List[float], List[float], List[float]]:
    """
    Remove points whose residuals from a linear detrend exceed sigma × std.

    Returns (t_good, y_good, t_bad, y_bad).  Falls back to all-good when
    fewer than 3 points exist or std is effectively zero.
    """
    n = len(t)
    if n < 3:
        return t, y, [], []

    # Linear detrend
    t_arr = t
    t0 = t[0]
    xs = [ti - t0 for ti in t_arr]
    n_f = len(xs)
    x_bar = sum(xs) / n_f
    y_bar = sum(y) / n_f
    sxx = sum((x - x_bar) ** 2 for x in xs)
    if sxx < 1e-30:
        residuals = [yi - y_bar for yi in y]
    else:
        sxy = sum((xs[i] - x_bar) * (y[i] - y_bar) for i in range(n_f))
        b = sxy / sxx
        a = y_bar - b * x_bar
        residuals = [y[i] - (a + b * xs[i]) for i in range(n_f)]

    std_res = math.sqrt(sum(r ** 2 for r in residuals) / n_f)
    if std_res < 1e-30:
        return t, y, [], []

    threshold = sigma * std_res
    t_good, y_good, t_bad, y_bad = [], [], [], []
    for ti, yi, ri in zip(t, y, residuals):
        if abs(ri) <= threshold:
            t_good.append(ti)
            y_good.append(yi)
        else:
            t_bad.append(ti)
            y_bad.append(yi)

    if not t_good:          # everything clipped — return all points unclipped
        return t, y, [], []

    return t_good, y_good, t_bad, y_bad


def _channel_summary(
    rows: List[Dict],
    channel: str,
    t_seq_start: float,
    sigma: float,
) -> Optional[ChannelSummary]:
    """Compute ChannelSummary for one channel from a sliced InletStateData row list."""
    pairs = [
        (r["lvTime"] - t_seq_start, r[channel])
        for r in rows
        if channel in r
        and not math.isnan(r.get("lvTime", float("nan")))
        and not math.isnan(r.get(channel, float("nan")))
    ]
    if not pairs:
        return None

    t_raw = [p[0] for p in pairs]
    y_raw = [p[1] for p in pairs]

    t_good, y_good, t_bad, y_bad = _sigma_clip(t_raw, y_raw, sigma)

    mean = _time_weighted_mean(t_good, y_good)
    n = len(y_good)
    std = (
        math.sqrt(sum((yi - mean) ** 2 for yi in y_good) / n)
        if n > 1 else 0.0
    )

    return ChannelSummary(
        channel=channel,
        mean=mean,
        sigma=std,
        n_points=n,
        n_outliers=len(y_bad),
        t_vals=t_good,
        y_vals=y_good,
        t_out=t_bad,
        y_out=y_bad,
    )


def _median(values: List[float]) -> float:
    vals = sorted(v for v in values if not math.isnan(v))
    if not vals:
        return float("nan")
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def _polyfit_means(
    t_knots: List[float],
    y_knots: List[float],
) -> ChannelFit:
    """
    Fit a polynomial through per-inlet means.

    Degree chosen automatically: 1 for ≤3 knots, 2 for ≤5, 3 for more.
    Falls back to lower degree when the system is under-determined.
    Returns a ChannelFit with coeffs=[] (degree=0, R²=nan) when <2 knots.
    """
    n = len(t_knots)
    if n < 2:
        return ChannelFit(
            channel="", t_knots=t_knots, y_knots=y_knots,
            degree=0, coeffs=[], r_squared=float("nan"),
        )

    target_degree = 1 if n <= 3 else (2 if n <= 5 else 3)
    degree = min(target_degree, n - 1)

    # Vandermonde (manual, no numpy dependency)
    # Build x = t_knots, y = y_knots, fit degree-d polynomial by normal equations
    xs = t_knots
    ys = y_knots

    # Centre x to improve numerical conditioning
    x0 = sum(xs) / n
    xc = [x - x0 for x in xs]
    d = degree

    # Build moment matrix A (d+1 × d+1) and rhs b (d+1)
    A = [[sum(xc[k] ** (i + j) for k in range(n)) for j in range(d + 1)] for i in range(d + 1)]
    b = [sum(ys[k] * xc[k] ** i for k in range(n)) for i in range(d + 1)]

    # Gaussian elimination with partial pivoting
    aug = [A[i] + [b[i]] for i in range(d + 1)]
    for col in range(d + 1):
        # Pivot
        max_row = max(range(col, d + 1), key=lambda r: abs(aug[r][col]))
        aug[col], aug[max_row] = aug[max_row], aug[col]
        if abs(aug[col][col]) < 1e-30:
            degree = max(degree - 1, 1)
            return _polyfit_means(t_knots, y_knots)  # retry with lower degree
        for row in range(d + 1):
            if row != col:
                factor = aug[row][col] / aug[col][col]
                aug[row] = [aug[row][j] - factor * aug[col][j] for j in range(d + 2)]
    coeffs_centred = [aug[i][d + 1] / aug[i][i] for i in range(d + 1)]
    # coeffs_centred[i] is coefficient of (x-x0)^i  (ascending powers)

    # Convert to standard form p(x) = c[0]*x^d + ... highest-power-first
    # Using binomial expansion: (x-x0)^k = Σ C(k,j)*x^j*(-x0)^(k-j)
    n_c = d + 1
    std_coeffs = [0.0] * n_c  # std_coeffs[i] → coefficient of x^(d-i)
    for i, ci in enumerate(coeffs_centred):   # ci*(x-x0)^i
        for j in range(i + 1):
            binom = 1
            for m in range(j):
                binom = binom * (i - m) // (m + 1)
            power_of_x = j                     # x^j term
            power_idx = d - j                  # index in highest-first array
            std_coeffs[power_idx] += ci * binom * ((-x0) ** (i - j))

    # R²
    y_bar = sum(ys) / n

    def _eval(x):
        v = 0.0
        for c in std_coeffs:
            v = v * x + c
        return v

    ss_res = sum((ys[k] - _eval(xs[k])) ** 2 for k in range(n))
    ss_tot = sum((ys[k] - y_bar) ** 2 for k in range(n))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-30 else 1.0

    return ChannelFit(
        channel="",
        t_knots=t_knots,
        y_knots=y_knots,
        degree=degree,
        coeffs=std_coeffs,
        r_squared=r2,
    )


# ---------------------------------------------------------------------------
# Per-inlet baseline/sample phase computation
# ---------------------------------------------------------------------------

def _phase_stats(vals: List[float]) -> Tuple[float, float]:
    """Return (mean, err) where err = std/sqrt(n-1), or (nan, 0) when empty."""
    n = len(vals)
    if n == 0:
        return float("nan"), 0.0
    m = sum(vals) / n
    if n == 1:
        return m, 0.0
    variance = sum((v - m) ** 2 for v in vals) / n
    std = math.sqrt(variance)
    return m, std / math.sqrt(n - 1)


def _plateau_start(
    v_sample: List[float],
    baseline_mean: float,
    min_plateau: int = 6,
    settle_frac: float = 0.20,
) -> int:
    """
    Return the index into v_sample where the stable plateau begins.

    Uses the last `min_plateau` readings as a reference tail, then finds the
    last reading whose deviation from the tail mean exceeds `settle_frac` of
    the net signal (tail_mean - baseline_mean).  The plateau starts at the
    index after that point.

    When the net signal is negligible (< 5 % of the signal level) — i.e. blank
    or near-zero inlets — no transient exists and 0 is returned so that all
    readings are treated as plateau.
    """
    n = len(v_sample)
    if n <= min_plateau:
        return 0
    tail = v_sample[-min_plateau:]
    tail_mean = sum(tail) / min_plateau
    net = tail_mean - baseline_mean
    signal_scale = max(abs(baseline_mean), abs(tail_mean), 1e-30)
    # No meaningful net: blank or negligible-signal inlet — entire sample is plateau.
    if abs(net) < 0.05 * signal_scale:
        return 0
    threshold = settle_frac * abs(net)
    tail_noise = math.sqrt(sum((v - tail_mean) ** 2 for v in tail) / min_plateau)
    if threshold < tail_noise:
        # Settle_frac threshold is below the noise floor.
        # If the net rise is itself within 3σ, the transient is not detectable —
        # treat the entire sample as plateau (e.g., blank inlets).
        # If the net rise IS significant (> 3σ), bump the threshold up to 3σ so
        # that rows clearly below the plateau level are still trimmed.
        if abs(net) < 3.0 * tail_noise:
            return 0
        threshold = 3.0 * tail_noise
    # Only scan the prefix (not the tail itself) — the tail is always kept.
    last_transient = -1
    for i in range(n - min_plateau):
        if abs(v_sample[i] - tail_mean) > threshold:
            last_transient = i
    return last_transient + 1


def _compute_gauge_phases(
    prep,
    all_rows: List[Dict],
    t_seq_start: float,
    plateau_max_he: Optional[int] = None,
    plateau_max_ne: Optional[int] = None,
    plateau_max_ar: Optional[int] = None,
    baseline_max_he: Optional[int] = None,
    baseline_max_ne: Optional[int] = None,
    baseline_max_ar: Optional[int] = None,
) -> Dict[str, GaugePhaseSummary]:
    """
    Split InletState rows into baseline / sample phases for He, Ne, Ar.

    Mirrors the XLSM GaugeSignals worksheet logic:

      He / Ne (SRGHeNe channel):
        Baseline = PumpSRG=1 & ToSRG=0  — gas pumped through rotor (early phase)
        Sample   = ToSRG=1              — gas admitted to rotor (late phase)

      Ar (SRGAr channel):
        The window contains three ordered sub-phases:
          [background: quiet P=0,T=0 at low SRGAr]
          [transition: ToSRG=1, gas floods in, SRGAr spikes then settles]
          [plateau: P=0,T=0 or P=1, SRGAr stable at elevated level]
        Baseline = all rows BEFORE the first ToSRG=1 row (quiet background)
        Sample   = all rows AFTER the last ToSRG=1 row (stable plateau)
        Transition rows are recorded separately for plotting only.
    """
    phases: Dict[str, GaugePhaseSummary] = {}

    def _extract_hene(t0_attr: str, t1_attr: str, element: str) -> Optional[GaugePhaseSummary]:
        # t0/t1 are the SRG measurement window (t_he_srg_start / t_he_srg_end).
        # This window contains: baseline (PumpSRG=1 & ToSRG=0) + plateau (ToSRG=1).
        # "Sample, all" is wider: it includes the gas-rise phase (ToSRG=1 rows
        # BEFORE t0) found by scanning backward until the first ToSRG=0 row.
        t0 = getattr(prep, t0_attr, float("nan"))
        t1 = getattr(prep, t1_attr, float("nan"))
        if math.isnan(t0) or math.isnan(t1):
            return None
        channel = "SRGHeNe"

        # SRG window → baseline + plateau
        srg_window = [
            r for r in all_rows
            if t0 <= r.get("lvTime", float("nan")) <= t1
            and not math.isnan(r.get(channel, float("nan")))
        ]
        if not srg_window:
            return None

        t_b, v_b, t_p, v_p = [], [], [], []
        for r in srg_window:
            t_rel = r["lvTime"] - t_seq_start
            val = r[channel]
            if r.get("PumpSRG") == 1 and r.get("ToSRG") == 0:
                t_b.append(t_rel); v_b.append(val)
            elif r.get("ToSRG") == 1:
                t_p.append(t_rel); v_p.append(val)

        if not v_b or not v_p:
            return None

        # Gas-rise phase: scan backward from t0, collecting contiguous ToSRG=1
        # rows until the first ToSRG=0 (bounded by inlet start).
        pre_srg = sorted(
            [r for r in all_rows
             if prep.lv_time_start <= r.get("lvTime", float("nan")) < t0
             and not math.isnan(r.get(channel, float("nan")))],
            key=lambda r: r["lvTime"],
        )
        rise_rows = []
        for r in reversed(pre_srg):
            if r.get("ToSRG") == 1:
                rise_rows.append(r)
            else:
                break
        rise_rows.reverse()

        # "Sample, all" = rise phase + all ToSRG=1 rows (chronological order)
        t_s = [r["lvTime"] - t_seq_start for r in rise_rows] + t_p
        v_s = [r[channel] for r in rise_rows] + v_p

        # B-Max: cap baseline to last N readings (closest to gas admission)
        bm = baseline_max_he if element == "He" else baseline_max_ne
        if bm is not None and len(v_b) > bm:
            t_b = t_b[-bm:]
            v_b = v_b[-bm:]

        b_mean, b_err = _phase_stats(v_b)

        # Plateau = stable tail of the sample window (skip admission transient)
        ps = _plateau_start(v_s, b_mean)
        t_plat = t_s[ps:]
        v_plat = v_s[ps:]
        pm = plateau_max_he if element == "He" else plateau_max_ne
        if pm is not None and len(v_plat) > pm:
            t_plat = t_plat[-pm:]
            v_plat = v_plat[-pm:]

        s_mean, s_err = _phase_stats(v_s)
        p_mean, p_err = _phase_stats(v_plat) if v_plat else (float("nan"), 0.0)
        net = p_mean - b_mean
        return GaugePhaseSummary(
            element=element, channel=channel,
            t_baseline=t_b, v_baseline=v_b,
            t_sample=t_s, v_sample=v_s,
            t_plateau=t_plat, v_plateau=v_plat,
            baseline_mean=b_mean, baseline_err=b_err,
            sample_mean=s_mean, sample_err=s_err,
            plateau_mean=p_mean, plateau_err=p_err,
            net=net, net_err=math.sqrt(b_err ** 2 + p_err ** 2),
            n_baseline=len(v_b), n_sample=len(t_s), n_plateau=len(v_plat),
        )

    for el, t0a, t1a in (("He", "t_he_srg_start", "t_he_srg_end"),
                          ("Ne", "t_ne_srg_start", "t_ne_srg_end")):
        ph = _extract_hene(t0a, t1a, el)
        if ph is not None:
            phases[el] = ph

    # Ar: The measurement window [t_ar_srg_start, t_ar_srg_end] spans the full
    # SRG recording: gas starts at background level, is admitted to the SRG
    # partway through the window, then stabilises at an elevated level.
    # t_ar_srg_end is logged when the SRG reads the plateau value just before
    # gas is sent to the QMS — it is the natural end of the measurement.
    #
    # Baseline = background-level rows within the window (SRGAr < 3 × min).
    # Sample   = elevated rows within the SAME window (SRGAr >= threshold).
    #            We do NOT extend beyond t_ar_srg_end: the NGSepTrap pump-out
    #            that follows (~10–20 min later) also shows elevated SRGAr
    #            but is sharply decreasing and must not be included.
    # Plateau  = stable tail of sample (via _plateau_start).
    t0_ar = getattr(prep, "t_ar_srg_start", float("nan"))
    t1_ar = getattr(prep, "t_ar_srg_end", float("nan"))
    if not (math.isnan(t0_ar) or math.isnan(t1_ar)):
        channel = "SRGAr"

        # Collect all window rows; derive background threshold from minimum.
        win_rows_all = sorted(
            [r for r in all_rows
             if t0_ar <= r.get("lvTime", float("nan")) <= t1_ar
             and not math.isnan(r.get(channel, float("nan")))],
            key=lambda r: r["lvTime"]
        )
        if not win_rows_all:
            return phases

        win_min = min(r[channel] for r in win_rows_all)
        bg_threshold = max(3.0 * win_min, 1e-8)

        # Baseline: quiet (background-level) rows within the window.
        baseline_rows = [r for r in win_rows_all if r[channel] < bg_threshold]

        # Sample (all): elevated rows within the measurement window only.
        sample_rows_all = [r for r in win_rows_all if r[channel] >= bg_threshold]

        if baseline_rows and sample_rows_all:
            t_b = [r["lvTime"] - t_seq_start for r in baseline_rows]
            v_b = [r[channel] for r in baseline_rows]
            t_s = [r["lvTime"] - t_seq_start for r in sample_rows_all]
            v_s = [r[channel] for r in sample_rows_all]

            # B-Max: cap baseline to last N readings (closest to gas admission)
            if baseline_max_ar is not None and len(v_b) > baseline_max_ar:
                t_b = t_b[-baseline_max_ar:]
                v_b = v_b[-baseline_max_ar:]

            b_mean, b_err = _phase_stats(v_b)
            s_mean, s_err = _phase_stats(v_s)

            # Stable plateau: skip admission transient using same algorithm as He/Ne
            ps = _plateau_start(v_s, b_mean)
            t_plat = t_s[ps:]
            v_plat = v_s[ps:]
            if plateau_max_ar is not None and len(v_plat) > plateau_max_ar:
                t_plat = t_plat[-plateau_max_ar:]
                v_plat = v_plat[-plateau_max_ar:]

            p_mean, p_err = _phase_stats(v_plat) if v_plat else (float("nan"), 0.0)
            net = p_mean - b_mean
            phases["Ar"] = GaugePhaseSummary(
                element="Ar", channel=channel,
                t_baseline=t_b, v_baseline=v_b,
                t_sample=t_s, v_sample=v_s,
                t_plateau=t_plat, v_plateau=v_plat,
                baseline_mean=b_mean, baseline_err=b_err,
                sample_mean=s_mean, sample_err=s_err,
                plateau_mean=p_mean, plateau_err=p_err,
                net=net, net_err=math.sqrt(b_err ** 2 + p_err ** 2),
                n_baseline=len(v_b), n_sample=len(v_s), n_plateau=len(v_plat),
            )

    return phases


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_gauge_summary(
    seq: "ProtocolSequence",                  # noqa: F821
    sigma: float = 3.0,
    apply_qc_flags: bool = True,
    plateau_max_he: Optional[int] = None,
    plateau_max_ne: Optional[int] = None,
    plateau_max_ar: Optional[int] = None,
    baseline_max_he: Optional[int] = None,
    baseline_max_ne: Optional[int] = None,
    baseline_max_ar: Optional[int] = None,
) -> Optional[GaugeSequenceSummary]:
    """
    Compute per-inlet, per-channel gauge summaries for *seq*.

    Parameters
    ----------
    sigma : float
        σ-clipping threshold for outlier removal within each inlet window.
    plateau_max_he / plateau_max_ne / plateau_max_ar : int or None
        Cap the plateau for each element to at most the last N readings after
        convergence detection trims the front.  None = use all stable readings.
    baseline_max_he / baseline_max_ne / baseline_max_ar : int or None
        Cap the baseline for each element to at most the last N readings.
        None = use all baseline readings.
    apply_qc_flags : bool
        When True, flag inlets whose SRG mean exceeds _QC_FLAG_FACTOR × the
        run median for that channel.
    """
    if seq.inlet_state is None or not seq.inlet_state.rows:
        return None

    available = [c for c in ALL_CHANNELS if c in seq.inlet_state.columns]
    if not available:
        log.debug("compute_gauge_summary: no known gauge channels in InletState")
        return None

    t_seq_start = seq.lv_time_start
    summary = GaugeSequenceSummary(channels_available=available)

    for prep in seq.inlets:
        t_start = prep.lv_time_start
        t_end = prep.lv_time_end if prep.lv_time_end is not None else float("inf")

        sliced = seq.inlet_state.slice(t_start, t_end)
        if not sliced.rows:
            continue

        inlet_type = (
            "blank" if prep.is_blank
            else "standard" if prep.is_reference and prep.reference_amount > 0
            else "sample"
        )

        # Build SRG measurement-phase windows relative to sequence start
        srg_windows: Dict[str, List[float]] = {}
        for key, (t0_attr, t1_attr) in {
            "He":  ("t_he_srg_start", "t_he_srg_end"),
            "Ne":  ("t_ne_srg_start", "t_ne_srg_end"),
            "Ar":  ("t_ar_srg_start", "t_ar_srg_end"),
        }.items():
            t0 = getattr(prep, t0_attr, float("nan"))
            t1 = getattr(prep, t1_attr, float("nan"))
            if not math.isnan(t0) and not math.isnan(t1):
                srg_windows[key] = [t0 - t_seq_start, t1 - t_seq_start]

        gauge_phases = _compute_gauge_phases(
            prep, seq.inlet_state.rows, t_seq_start,
            plateau_max_he=plateau_max_he,
            plateau_max_ne=plateau_max_ne,
            plateau_max_ar=plateau_max_ar,
            baseline_max_he=baseline_max_he,
            baseline_max_ne=baseline_max_ne,
            baseline_max_ar=baseline_max_ar,
        )

        ig = InletGaugeSummary(
            seq_num=prep.seq_num,
            description=prep.inlet_string,
            inlet_type=inlet_type,
            t_start=t_start,
            t_end=t_end,
            srg_windows=srg_windows,
            gauge_phases=gauge_phases,
        )

        for ch in available:
            cs = _channel_summary(sliced.rows, ch, t_seq_start, sigma)
            if cs is not None:
                ig.channels[ch] = cs

        summary.inlets.append(ig)

    if not summary.inlets:
        return None

    # Run-level medians per channel (for QC thresholds)
    for ch in available:
        means = [
            ig.channels[ch].mean
            for ig in summary.inlets
            if ch in ig.channels and not math.isnan(ig.channels[ch].mean)
        ]
        summary.run_medians[ch] = _median(means)

    # QC flags: inlet mean > factor × run median
    if apply_qc_flags:
        for ig in summary.inlets:
            for ch in PRIMARY_CHANNELS:
                if ch not in ig.channels:
                    continue
                med = summary.run_medians.get(ch, float("nan"))
                if math.isnan(med) or med <= 0:
                    continue
                ig.qc_flags[ch] = ig.channels[ch].mean > _QC_FLAG_FACTOR * med

    # Full-sequence raw time-series for GaugeSignal-style continuous plots
    raw_rows = seq.inlet_state.rows
    summary.raw_t = [
        r["lvTime"] - t_seq_start
        for r in raw_rows
        if not math.isnan(r.get("lvTime", float("nan")))
    ]
    for ch in available:
        summary.raw_data[ch] = [
            r.get(ch, float("nan"))
            for r in raw_rows
            if not math.isnan(r.get("lvTime", float("nan")))
        ]

    # Polynomial fits through per-inlet means (GaugeSignal fit lines)
    FITTED_CHANNELS = ["BaratronInlet"] + PRIMARY_CHANNELS
    for ch in available:
        if ch not in FITTED_CHANNELS:
            continue
        knot_t, knot_y = [], []
        for ig in summary.inlets:
            if ch not in ig.channels:
                continue
            cs = ig.channels[ch]
            if math.isnan(cs.mean):
                continue
            t_mid = (ig.t_start + (ig.t_end if ig.t_end != float("inf") else ig.t_start)) / 2.0 - t_seq_start
            knot_t.append(t_mid)
            knot_y.append(cs.mean)
        if len(knot_t) >= 2:
            fit = _polyfit_means(knot_t, knot_y)
            fit.channel = ch
            summary.channel_fits[ch] = fit

    return summary
