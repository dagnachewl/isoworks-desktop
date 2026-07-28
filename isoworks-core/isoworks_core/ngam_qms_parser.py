"""
ngam_qms_parser.py
==================
Pure parser for Isotopx NobleControl QMS (Quadrupole Mass Spectrometer) files.
No database or GUI dependencies.

QMS file format (semicolon-delimited, one row per acquisition event):
    lvTime;Detector;Mass;Faraday;Multiplier;ActionToDo

Signal is always in the Faraday column (Multiplier is always NaN for QMS).

Supported file extensions / devices:
    .QMSAr   – Argon isotopes
    .QMSNe   – Neon isotopes
    .QMSKrXe – Krypton + Xenon isotopes

File naming convention:
    YYYY-MM-DD_HH-MM-SS {InletNum} {Description}.{Extension}
    e.g. "2013-07-11_11-16-04 2 Standard_SpikeLarge2.QMSAr"
"""
from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

_ENCODINGS = ["utf-8-sig", "utf-8", "latin-1"]

_FNAME_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\s+(\d+)\s+(.*?)\.(QMSAr|QMSNe|QMSKrXe)$",
    re.IGNORECASE,
)

# Signal ActionToDo values per device (Faraday column only)
_SIGNAL_ACTIONS: Dict[str, frozenset] = {
    "QMSAr":   frozenset({"36Ar", "38Ar", "40Ar"}),
    "QMSNe":   frozenset({"20Ne", "21Ne", "22Ne"}),
    "QMSKrXe": frozenset({
        "84Kr", "82Kr", "83Kr", "86Kr",
        "132Xe", "129Xe", "131Xe", "134Xe", "136Xe",
    }),
}

# Background ActionToDo value per device
_BG_ACTIONS: Dict[str, str] = {
    "QMSAr":   "QMSArBackground",
    "QMSNe":   "NeBackground",
    "QMSKrXe": "QMSKrXeBackground",
}


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class QMSInletData:
    """Parsed signal data from a single Isotopx QMS file (one inlet)."""

    filepath: str
    device: str          # "QMSAr" | "QMSNe" | "QMSKrXe"
    inlet_num: int
    description: str
    t0_lv: float

    # Per-isotope signal arrays: isotope → (times_rel, signals)
    signals: Dict[str, Tuple[List[float], List[float]]] = field(
        default_factory=dict
    )

    # Background (shared across all isotopes in the file)
    bg_t:   List[float] = field(default_factory=list)
    bg_sig: List[float] = field(default_factory=list)

    @property
    def isotopes(self) -> List[str]:
        """Sorted list of available signal isotopes."""
        return sorted(self.signals.keys())


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(s: str) -> float:
    s = s.strip()
    if not s or s.lower() == "nan":
        return float("nan")
    try:
        return float(s)
    except ValueError:
        return float("nan")


def _read_lines(path: str) -> List[str]:
    for enc in _ENCODINGS:
        try:
            with open(path, "r", encoding=enc, errors="replace") as fh:
                return fh.readlines()
        except Exception as exc:
            log.debug("Encoding %s failed for %s: %s", enc, path, exc)
    raise IOError(f"Could not read file: {path}")


def _parse_filename(path: str) -> Tuple[str, int, str]:
    """
    Extract (device, inlet_num, description) from QMS filename.
    Falls back to (guessed_device, 0, basename) when the pattern doesn't match.
    """
    basename = os.path.basename(path)
    m = _FNAME_RE.match(basename)
    if m:
        device = m.group(4)
        # Normalise extension capitalisation to canonical form
        for key in _SIGNAL_ACTIONS:
            if key.lower() == device.lower():
                device = key
                break
        return device, int(m.group(2)), m.group(3).strip()

    ext = os.path.splitext(basename)[1].lstrip(".").upper()
    for key in _SIGNAL_ACTIONS:
        if key.upper() == ext:
            log.warning("QMS filename does not match expected pattern: %s", basename)
            return key, 0, os.path.splitext(basename)[0]

    log.warning("QMS filename does not match expected pattern: %s", basename)
    return "QMSAr", 0, os.path.splitext(basename)[0]


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

def parse_qms(filepath: str) -> QMSInletData:
    """
    Parse one Isotopx QMS file and return a QMSInletData object.

    Parameters
    ----------
    filepath : str
        Absolute path to a .QMSAr, .QMSNe, or .QMSKrXe file.

    Returns
    -------
    QMSInletData
        Populated with per-isotope signal arrays and a shared background
        array.  Times are seconds from t0 (minimum lvTime of signal rows).

    Raises
    ------
    IOError
        If the file cannot be read.
    ValueError
        If the file header is missing required columns.
    """
    device, inlet_num, description = _parse_filename(filepath)
    lines = _read_lines(filepath)
    if not lines:
        raise IOError(f"Empty file: {filepath}")

    header_cols = [c.strip().lower() for c in lines[0].rstrip("\r\n").split(";")]

    def _require(name: str) -> int:
        try:
            return header_cols.index(name)
        except ValueError:
            raise ValueError(f"QMS file {filepath}: missing column '{name}'")

    idx_lv      = _require("lvtime")
    idx_faraday = _require("faraday")
    idx_mult    = _require("multiplier")
    idx_action  = _require("actiontodo")

    sig_actions = _SIGNAL_ACTIONS[device]
    bg_action   = _BG_ACTIONS[device]

    # Collect all relevant rows
    raw_rows: List[Tuple[float, float, str]] = []

    for line_no, line in enumerate(lines[1:], start=2):
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        parts = line.split(";")

        def _col(idx: int) -> str:
            return parts[idx].strip() if idx < len(parts) else ""

        action = _col(idx_action)
        if action not in sig_actions and action != bg_action:
            continue

        lv_time = _safe_float(_col(idx_lv))
        if math.isnan(lv_time):
            log.debug("QMS %s line %d: invalid lvTime, skipping", filepath, line_no)
            continue

        # Use whichever of Faraday/Multiplier is non-NaN (prefer Faraday)
        signal = _safe_float(_col(idx_faraday))
        if math.isnan(signal):
            signal = _safe_float(_col(idx_mult))
        raw_rows.append((lv_time, signal, action))

    if not raw_rows:
        log.warning("QMS %s: no signal rows found", filepath)
        return QMSInletData(
            filepath=filepath,
            device=device,
            inlet_num=inlet_num,
            description=description,
            t0_lv=0.0,
        )

    t0 = min(r[0] for r in raw_rows)

    sig_dict: Dict[str, Tuple[List[float], List[float]]] = {}
    bg_t: List[float] = []
    bg_sig: List[float] = []

    for lv_time, signal, action in raw_rows:
        t_rel = lv_time - t0
        if math.isnan(signal):
            continue

        if action == bg_action:
            bg_t.append(t_rel)
            bg_sig.append(signal)
        else:
            if action not in sig_dict:
                sig_dict[action] = ([], [])
            sig_dict[action][0].append(t_rel)
            sig_dict[action][1].append(signal)

    log.debug(
        "QMS %s (inlet %d %s): isotopes=%s  bg_pts=%d",
        os.path.basename(filepath),
        inlet_num,
        description,
        sorted(sig_dict.keys()),
        len(bg_t),
    )

    return QMSInletData(
        filepath=filepath,
        device=device,
        inlet_num=inlet_num,
        description=description,
        t0_lv=t0,
        signals=sig_dict,
        bg_t=bg_t,
        bg_sig=bg_sig,
    )
