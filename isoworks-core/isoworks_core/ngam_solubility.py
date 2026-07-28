"""
ngam_solubility.py
==================
Temperature- and salinity-dependent noble gas solubility in water.

Implements the Weiss (1971) / Benson & Krause (1980) equation family used
universally in noble gas hydrology:

    ln C_i = A1 + A2·(100/T) + A3·ln(T/100) + A4·(T/100)²
             + S · [B1 + B2·(T/100) + B3·(T/100)²]

where
  C_i  – solubility in cm³(STP) / g H₂O at 1 atm partial pressure of gas i
  T    – absolute temperature (K)
  S    – salinity (g / kg, i.e. ‰)

The equilibrium dissolved concentration at realistic atmospheric conditions is:

    C_eq(T, S, P) = C_i(T, S) × p_i(T, P)

where p_i(T, P) = x_i_dry × (P − p_H₂O(T)) is the partial pressure of gas i
in moist air at atmospheric pressure P (atm).

Public API
----------
solubility_cm3_per_g(element, T_c, S, p_partial_atm)
    → cm³ STP dissolved per gram of water at given partial pressure

equilibrium_cm3_per_g(element, T_c, S, P_atm)
    → cm³ STP / g at atmospheric equilibrium (accounts for water vapour)

equilibrium_table(T_range_c, S, P_atm)
    → dict {T_c: {element: C_eq}} — tabulated for display / export

isotope_cm3_per_g(isotope_label, T_c, S, P_atm)
    → cm³ STP / g for a specific isotope (e.g. "20Ne", "36Ar", "132Xe")

References
----------
Weiss, R.F. (1971). Solubility of helium and neon in water and seawater.
    J. Chem. Eng. Data, 16(2), 235-241.
Weiss, R.F. (1970). The solubility of nitrogen, oxygen and argon in water
    and seawater. Deep Sea Res., 17(4), 721-735.
Weiss, R.F. & Kyser, T.K. (1978). Solubility of krypton in water and
    seawater. J. Chem. Eng. Data, 23(1), 69-72.
Benson, B.B. & Krause, D. (1980). Isotopic fractionation of helium during
    solution: A probe for the liquid state. J. Solution Chem., 9(12), 895-909.
Smith, S.P. & Kennedy, B.M. (1983). The solubility of noble gases in water
    and NaCl brine. Geochim. Cosmochim. Acta, 47(3), 503-515.  [Xe]
Kipfer, R. et al. (2002). Noble gases in lakes and ground waters.
    Rev. Mineral. Geochem., 47, 615-700.  [comprehensive review]
"""
from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Weiss-equation coefficients
# ---------------------------------------------------------------------------
# Layout: (A1, A2, A3, A4, B1, B2, B3)
# C in cm³ STP / g at 1 atm partial pressure of the pure gas.
# T in Kelvin, S in g/kg.

_WEISS_COEFFS: Dict[str, Tuple[float, float, float, float, float, float, float]] = {
    # Weiss (1971)
    "He": (-167.2178,  216.3442,  139.2032, -22.6202,
           -0.044781,   0.023541,  -0.0034266),
    # Weiss (1971)
    "Ne": (-170.6018,  225.1946,  140.8863, -22.6290,
           -0.127113,   0.079277,  -0.0129095),
    # Weiss (1970)
    "Ar": (-178.1501,  251.8502,  145.2337, -22.2046,
           -0.038729,   0.017171,  -0.0021512),
    # Weiss & Kyser (1978)
    "Kr": (-112.6840,  153.5817,   74.4690, -10.0189,
           -0.011213,   0.001844,   0.0),
    # Smith & Kennedy (1983) — updated Xe coefficients
    "Xe": ( -74.7398,  105.2100,   27.4664,   0.0,
            -0.0139720,  0.0170648, -0.00297654),
}

ELEMENTS = list(_WEISS_COEFFS.keys())

# ---------------------------------------------------------------------------
# Atmospheric dry-air mole fractions (NOAA/CODATA 2022 values)
# These are volume fractions in dry air, equal to mole fractions (ideal gas).
# ---------------------------------------------------------------------------
_X_DRY: Dict[str, float] = {
    "He": 5.24e-6,
    "Ne": 18.18e-6,
    "Ar": 9.34e-3,
    "Kr": 1.14e-6,
    "Xe": 8.7e-8,
}

# ---------------------------------------------------------------------------
# Atmospheric isotopic abundances (mole fraction of isotope in total element)
# Sources: Berglund & Wieser (2011), IUPAC 2016
# ---------------------------------------------------------------------------
_ISO_ABUNDANCE: Dict[str, float] = {
    # He
    "3He":   1.384e-6,       # ³He/He atmospheric ratio × total He
    "4He":   1.0 - 1.384e-6,
    # Ne
    "20Ne":  0.9048,
    "21Ne":  0.0027,
    "22Ne":  0.0925,
    # Ar
    "36Ar":  0.003336,
    "38Ar":  0.000629,
    "40Ar":  0.996035,
    # Kr
    "78Kr":  0.00355,
    "80Kr":  0.02286,
    "82Kr":  0.11593,
    "83Kr":  0.11500,
    "84Kr":  0.56987,
    "86Kr":  0.17279,
    # Xe
    "124Xe": 0.000952,
    "126Xe": 0.000890,
    "128Xe": 0.019102,
    "129Xe": 0.264006,
    "130Xe": 0.040710,
    "131Xe": 0.212324,
    "132Xe": 0.269086,
    "134Xe": 0.104357,
    "136Xe": 0.088573,
}

# Map each isotope label to its parent element
_ISO_TO_ELEMENT: Dict[str, str] = {}
for _iso, _ab in _ISO_ABUNDANCE.items():
    if _iso.endswith("He"):
        _ISO_TO_ELEMENT[_iso] = "He"
    elif _iso.endswith("Ne"):
        _ISO_TO_ELEMENT[_iso] = "Ne"
    elif _iso.endswith("Ar"):
        _ISO_TO_ELEMENT[_iso] = "Ar"
    elif _iso.endswith("Kr"):
        _ISO_TO_ELEMENT[_iso] = "Kr"
    elif _iso.endswith("Xe"):
        _ISO_TO_ELEMENT[_iso] = "Xe"


# ---------------------------------------------------------------------------
# Water vapour pressure (Antoine equation, Buck 1981)
# Returns p_H2O in atm for T in °C
# ---------------------------------------------------------------------------
def _water_vapour_pressure_atm(T_c: float) -> float:
    """Water vapour pressure in atm using Buck (1981) approximation."""
    # Result in kPa first, then convert
    p_kpa = 0.61121 * math.exp((18.678 - T_c / 234.5) * (T_c / (257.14 + T_c)))
    return p_kpa / 101.325   # atm


# ---------------------------------------------------------------------------
# Core Weiss equation
# ---------------------------------------------------------------------------
def solubility_cm3_per_g(
    element: str,
    T_c: float,
    S: float = 0.0,
    p_partial_atm: float = 1.0,
) -> float:
    """
    Equilibrium dissolved concentration (cm³ STP / g H₂O) of *element* at
    partial pressure *p_partial_atm* (atm).

    Parameters
    ----------
    element : str
        One of "He", "Ne", "Ar", "Kr", "Xe".
    T_c : float
        Temperature in °C.  Valid range: −2 to 40 °C.
    S : float
        Salinity in g/kg (‰).  0 for fresh water.
    p_partial_atm : float
        Partial pressure of the pure gas (atm).  Use 1.0 for the Weiss
        unit solubility; pass the actual atmospheric partial pressure to
        get the realistic dissolved amount.

    Returns
    -------
    float
        cm³(STP) dissolved per gram of water.  Returns nan for unknown element.
    """
    coeffs = _WEISS_COEFFS.get(element)
    if coeffs is None:
        return float("nan")
    A1, A2, A3, A4, B1, B2, B3 = coeffs
    T_k = T_c + 273.15
    t = T_k / 100.0
    ln_c = (A1 + A2 * (100.0 / T_k) + A3 * math.log(t) + A4 * t * t
            + S * (B1 + B2 * t + B3 * t * t))
    return math.exp(ln_c) * p_partial_atm


def equilibrium_cm3_per_g(
    element: str,
    T_c: float,
    S: float = 0.0,
    P_atm: float = 1.0,
) -> float:
    """
    Equilibrium dissolved concentration (cm³ STP / g H₂O) of *element* in
    water in contact with moist air at total atmospheric pressure *P_atm*.

    The water-vapour correction reduces the effective dry-gas partial pressure:
        p_i = x_i_dry × (P_atm − p_H₂O(T))

    Parameters
    ----------
    element : str
        One of "He", "Ne", "Ar", "Kr", "Xe".
    T_c : float
        Water temperature (°C).
    S : float
        Salinity (g/kg).
    P_atm : float
        Total atmospheric pressure (atm).  Sea level ≈ 1.0.
        Use P_atm = exp(−altitude_m / 8500) as a first approximation.

    Returns
    -------
    float
        cm³ STP / g at atmospheric equilibrium.
    """
    x_dry = _X_DRY.get(element)
    if x_dry is None:
        return float("nan")
    p_h2o = _water_vapour_pressure_atm(T_c)
    p_dry = max(P_atm - p_h2o, 0.0)
    p_partial = x_dry * p_dry
    return solubility_cm3_per_g(element, T_c, S, p_partial)


def isotope_cm3_per_g(
    isotope_label: str,
    T_c: float,
    S: float = 0.0,
    P_atm: float = 1.0,
) -> float:
    """
    Equilibrium dissolved concentration (cm³ STP / g H₂O) for a specific
    isotope (e.g. "20Ne", "36Ar", "132Xe", "4He").

    Computed as:
        C_isotope = C_element × abundance_isotope_in_air

    Parameters
    ----------
    isotope_label : str
        Isotope string matching keys in _ISO_ABUNDANCE, e.g. "20Ne".
    T_c, S, P_atm : float
        Same as equilibrium_cm3_per_g.

    Returns
    -------
    float
        cm³ STP / g.  Returns nan for unknown isotope.
    """
    element = _ISO_TO_ELEMENT.get(isotope_label)
    abundance = _ISO_ABUNDANCE.get(isotope_label)
    if element is None or abundance is None:
        return float("nan")
    c_elem = equilibrium_cm3_per_g(element, T_c, S, P_atm)
    return c_elem * abundance


# ---------------------------------------------------------------------------
# Convenience: full table for all elements at a range of temperatures
# ---------------------------------------------------------------------------
def equilibrium_table(
    T_range_c: List[float],
    S: float = 0.0,
    P_atm: float = 1.0,
) -> Dict[float, Dict[str, float]]:
    """
    Build a temperature × element table of equilibrium concentrations.

    Returns
    -------
    dict
        {T_c: {"He": C_He, "Ne": C_Ne, "Ar": C_Ar, "Kr": C_Kr, "Xe": C_Xe}}
        concentrations in cm³ STP / g H₂O.
    """
    return {
        T: {el: equilibrium_cm3_per_g(el, T, S, P_atm) for el in ELEMENTS}
        for T in T_range_c
    }


def pressure_from_altitude(altitude_m: float) -> float:
    """
    Barometric pressure (atm) from altitude in metres.
    Uses the standard-atmosphere barometric formula.
    """
    return math.exp(-altitude_m / 8500.0)


# ---------------------------------------------------------------------------
# Salinity correction factor (unitless)
# For a quick estimate: what fraction does adding salinity reduce solubility?
# ---------------------------------------------------------------------------
def salinity_correction_factor(element: str, T_c: float, S: float) -> float:
    """
    Ratio C(T, S) / C(T, 0) — the fractional reduction in solubility at
    salinity S relative to fresh water.  Always ≤ 1.0 for noble gases.
    """
    c_fresh = solubility_cm3_per_g(element, T_c, S=0.0, p_partial_atm=1.0)
    c_salty = solubility_cm3_per_g(element, T_c, S=S,   p_partial_atm=1.0)
    if c_fresh < 1e-30:
        return 1.0
    return c_salty / c_fresh
