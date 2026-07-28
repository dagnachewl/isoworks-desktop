"""
ngam_ingrowth_processor.py
==========================
Standalone ³H→³He ingrowth calculator.  No database or GUI dependencies.

Converts measured ³He concentration to original ³H activity using the
ingrowth decay equation:

    ³H₀ = ³He_measured / (1 − e^(−λ·t))

where λ = ln(2) / t½  and  t = ingrowth duration.

Constants
---------
t½(³H) = 12.32 years  (NIST / IAEA recommended value)
"""

from __future__ import annotations

import math

# ---------------------------------------------------------------------------
# Physical constants
# ---------------------------------------------------------------------------

_MOLAR_VOLUME_STP = 22413.969          # cm^3 / mol  (ideal gas at STP: 0 °C, 1 atm)
_AVOGADRO = 6.02214076e23              # atoms / mol
_SECONDS_PER_YEAR = 365.25 * 24.0 * 3600.0
_WATER_MOLAR_MASS = 18.015             # g / mol  (H₂O)
_H_ATOMS_PER_MOL_WATER = 2.0          # hydrogen atoms per water molecule
_TU_SCALE = 1e18                       # 1 TU = 1 ³H per 10¹⁸ H atoms

_HALF_LIFE_YEARS_DEFAULT = 12.32       # ³H half-life (years)

# Derived
_LAMBDA_DEFAULT = math.log(2) / (_HALF_LIFE_YEARS_DEFAULT * _SECONDS_PER_YEAR)  # s⁻¹


# ---------------------------------------------------------------------------
# Ingrowth correction
# ---------------------------------------------------------------------------

def compute_ingrowth_correction(
    ingrowth_days: float,
    half_life_years: float = _HALF_LIFE_YEARS_DEFAULT,
) -> float:
    """Return the multiplicative correction factor for ingrowth decay.

    Factor = 1 / (1 − e^(−λ·t))

    Multiply *measured* ³He by this factor to recover the *initial* ³H
    concentration at the start of ingrowth.

    Parameters
    ----------
    ingrowth_days : float
        Ingrowth duration in days (dtimeend − dtimestart).
    half_life_years : float
        ³H half-life in years (default 12.32).

    Returns
    -------
    float
        Correction factor.  Returns inf if ingrowth_days ≤ 0.

    Raises
    ------
    ValueError
        If ingrowth_days ≤ 0 or half_life_years ≤ 0.
    """
    if ingrowth_days <= 0:
        raise ValueError(f"ingrowth_days must be > 0, got {ingrowth_days}")
    if half_life_years <= 0:
        raise ValueError(f"half_life_years must be > 0, got {half_life_years}")

    lambda_h = math.log(2) / (half_life_years * _SECONDS_PER_YEAR)
    t_seconds = ingrowth_days * 86400.0
    ingrowth_fraction = 1.0 - math.exp(-lambda_h * t_seconds)

    if ingrowth_fraction <= 0:
        return float("inf")

    return 1.0 / ingrowth_fraction


def compute_ingrowth_correction_unc(
    ingrowth_days: float,
    ingrowth_days_unc: float,
    half_life_years: float = _HALF_LIFE_YEARS_DEFAULT,
) -> float:
    """Approximate uncertainty on the ingrowth correction factor.

    Uses first-order error propagation:  dF/dt × σ_t
    """
    if ingrowth_days <= 0 or ingrowth_days_unc <= 0:
        return float("nan")

    lambda_h = math.log(2) / (half_life_years * _SECONDS_PER_YEAR)
    t_seconds = ingrowth_days * 86400.0
    dt_seconds = ingrowth_days_unc * 86400.0

    e_lt = math.exp(-lambda_h * t_seconds)
    denom = 1.0 - e_lt
    if denom <= 0:
        return float("nan")

    # dF/dt = λ · e^(−λt) / (1 − e^(−λt))²
    dF_dt = lambda_h * e_lt / (denom * denom)
    return abs(dF_dt) * dt_seconds


def compute_ingrowth_correction_from_seconds(
    t_seconds: float,
    half_life_years: float = _HALF_LIFE_YEARS_DEFAULT,
) -> float:
    """Like compute_ingrowth_correction but accepts seconds directly."""
    if t_seconds <= 0:
        raise ValueError(f"t_seconds must be > 0, got {t_seconds}")

    lambda_h = math.log(2) / (half_life_years * _SECONDS_PER_YEAR)
    ingrowth_fraction = 1.0 - math.exp(-lambda_h * t_seconds)

    if ingrowth_fraction <= 0:
        return float("inf")

    return 1.0 / ingrowth_fraction


# ---------------------------------------------------------------------------
# ccSTP → Tritium Units (TU)
# ---------------------------------------------------------------------------

def ccstp_to_tritium_activity(
    ccstp: float,
    ccstp_unc: float,
    ingrowth_days: float,
    water_mass_g: float,
    t_sampling_days: float = 0.0,
    half_life_years: float = _HALF_LIFE_YEARS_DEFAULT,
) -> tuple:
    """Convert measured ³He ccSTP to initial ³H activity in Tritium Units.

    Full formula
    ------------
      n_³He     = ccSTP × N_A / V_molar                  (³He atoms measured)
      n_³H_extr = n_³He / (1 − e^(−λ·t_em))              (³H at extraction/degassing time)
      n_H       = 2 × water_mass_g / M_H₂O × N_A          (H atoms in sample)
      TU_extr   = n_³H_extr / n_H × 10¹⁸
      TU_samp   = TU_extr × e^(λ·t_se)                   (correct back to sampling date)

    where t_em = ingrowth period (extraction → measurement)
          t_se = storage period  (sampling → extraction)

    Parameters
    ----------
    ccstp : float
        ³He concentration in ccSTP.
    ccstp_unc : float
        Uncertainty on ccstp.
    ingrowth_days : float
        Ingrowth duration in days (t_em: extraction → measurement).
    water_mass_g : float
        Post-degassing water mass (g) — used for TU normalisation.
    t_sampling_days : float
        Storage duration in days (t_se: sampling → extraction).  Pass 0 to
        report TU at extraction date rather than original sampling date.
    half_life_years : float
        ³H half-life in years.

    Returns
    -------
    (activity_tu, activity_unc_tu)
        TU activity at the *sampling* date (or extraction date when
        t_sampling_days == 0).
    """
    if water_mass_g <= 0:
        raise ValueError(f"water_mass_g must be > 0, got {water_mass_g}")

    lambda_h = math.log(2) / (half_life_years * _SECONDS_PER_YEAR)
    correction = compute_ingrowth_correction(ingrowth_days, half_life_years)

    # ³He atoms per ccSTP
    atoms_per_ccstp = _AVOGADRO / _MOLAR_VOLUME_STP

    # ³He atoms measured
    n_3he = ccstp * atoms_per_ccstp

    # ³H atoms at extraction time (ingrowth-corrected)
    n_3h0 = n_3he * correction

    # Hydrogen atoms in sample water
    n_h = _H_ATOMS_PER_MOL_WATER * water_mass_g / _WATER_MOLAR_MASS * _AVOGADRO

    # TU at extraction time
    tu = n_3h0 / n_h * _TU_SCALE

    # Correct back to sampling date: e^(λ·t_se)
    if t_sampling_days > 0:
        tu *= math.exp(lambda_h * t_sampling_days * 86400.0)

    # Uncertainty (relative from ccSTP only — storage correction is exact)
    rel_u = ccstp_unc / ccstp if ccstp != 0 and not math.isnan(ccstp_unc) else 0.0
    tu_unc = tu * rel_u

    return tu, tu_unc


def ccstp_to_tritium_activity_from_seconds(
    ccstp: float,
    ccstp_unc: float,
    t_ingrowth_seconds: float,
    water_mass_g: float,
    half_life_years: float = _HALF_LIFE_YEARS_DEFAULT,
) -> tuple:
    """Like ccstp_to_tritium_activity but accepts seconds directly."""
    if water_mass_g <= 0:
        raise ValueError(f"water_mass_g must be > 0, got {water_mass_g}")

    correction = compute_ingrowth_correction_from_seconds(
        t_ingrowth_seconds, half_life_years
    )

    atoms_per_ccstp = _AVOGADRO / _MOLAR_VOLUME_STP
    n_3he = ccstp * atoms_per_ccstp
    n_3h0 = n_3he * correction
    n_h = _H_ATOMS_PER_MOL_WATER * water_mass_g / _WATER_MOLAR_MASS * _AVOGADRO
    tu = n_3h0 / n_h * _TU_SCALE

    rel_u = (
        ccstp_unc / ccstp
        if ccstp != 0 and not math.isnan(ccstp_unc)
        else 0.0
    )
    tu_unc = tu * rel_u

    return tu, tu_unc


def ccstp_to_concentration(
    ccstp: float,
    ccstp_unc: float,
    aliquot_ml: float,
) -> tuple:
    """Convert ccSTP to concentration per mL aliquot.

    Returns
    -------
    (concentration_ccstp_per_ml, concentration_unc)
    """
    if aliquot_ml <= 0:
        raise ValueError(f"aliquot_ml must be > 0, got {aliquot_ml}")

    conc = ccstp / aliquot_ml
    rel_u = (
        ccstp_unc / ccstp
        if ccstp != 0 and not math.isnan(ccstp_unc)
        else 0.0
    )
    conc_unc = conc * rel_u
    return conc, conc_unc


def decay_correct_activity(
    activity_tu: float,
    activity_unc_tu: float,
    ingrowth_days: float,
    half_life_years: float = _HALF_LIFE_YEARS_DEFAULT,
) -> tuple:
    """Apply ingrowth decay correction to a directly-measured activity value.

    Use this when you already have activity in TU (e.g. from Helix SFT
    sensitivity calibration) and only need to correct for ingrowth decay.

    activity_corrected = activity × 1 / (1 − e^(−λ·t))
    """
    correction = compute_ingrowth_correction(ingrowth_days, half_life_years)
    return activity_tu * correction, activity_unc_tu * correction


# ---------------------------------------------------------------------------
# Convenience: check whether an ingrowth period is usable
# ---------------------------------------------------------------------------

def is_ingrowth_valid(
    t_ingrowth_seconds: float,
    min_days: float = 0.01,
) -> bool:
    """Return True if the ingrowth period is long enough to be meaningful."""
    return t_ingrowth_seconds >= min_days * 86400.0


def ingrowth_warning(
    ingrowth_days: float,
    half_life_years: float = _HALF_LIFE_YEARS_DEFAULT,
) -> str | None:
    """Return a warning string if the ingrowth period is suspicious, else None."""
    if ingrowth_days <= 0:
        return "Ingrowth period is zero or negative."
    correction = compute_ingrowth_correction(ingrowth_days, half_life_years)
    if correction > 1000:
        return (
            f"Ingrowth period ({ingrowth_days:.1f} d) is very short ― "
            f"correction factor {correction:.0f}.  TU uncertainty will be large."
        )
    if correction > 100:
        return (
            f"Ingrowth period ({ingrowth_days:.1f} d) is short ― "
            f"correction factor {correction:.0f}."
        )
    return None
