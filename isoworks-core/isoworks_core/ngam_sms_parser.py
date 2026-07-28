"""
ngam_sms_parser.py
==================
Pure parser for Isotopx NobleControl SMS (Sector Mass Spectrometer) files.
No database or GUI dependencies.

SMS file format (semicolon-delimited, one row per acquisition event):
    lvTime;Detector;Field;MassFaraday;MassMultiplier;Faraday;Multiplier;ActionToDo

Column selection:
    Detector == 0  →  Faraday channel   (4He signal)
    Detector == 1  →  Multiplier/SEM channel (3He signal)

Signal phases retained:
    "3He"            → 3He sample signal (Multiplier)
    "4He"            → 4He sample signal (Faraday)
    "3HeBackGround"  → 3He background (Multiplier)
    "4HeBackGround"  → 4He background (Faraday)

All other ActionToDo phases are discarded (Inlet4He, PeakCenter*, PeakRaw*,
PumpDown4He, etc.).

File naming convention:
    YYYY-MM-DD_HH-MM-SS {InletNum} {Description}.SMS
    e.g. "2013-07-10_19-51-00 2 Standard_SpikeLarge2.SMS"
"""
from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

log = logging.getLogger(__name__)

_ENCODINGS = ["utf-8-sig", "utf-8", "latin-1"]

_FNAME_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2})\s+(\d+)\s+(.*?)\.SMS$",
    re.IGNORECASE,
)

# ActionToDo values that carry real signal data
_SIGNAL_ACTIONS = {"3He", "4He", "3HeBackGround", "4HeBackGround"}


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclass
class SMSInletData:
    """Parsed signal data from a single Isotopx SMS file (one inlet)."""

    filepath: str
    inlet_num: int       # parsed from filename (1-based)
    description: str     # e.g. "Standard_SpikeLarge2", "Blank", "Sample1"
    t0_lv: float         # absolute LabVIEW timestamp of first signal row

    # 3He sample signal (Multiplier column, ActionToDo="3He")
    he3_t:   List[float] = field(default_factory=list)   # seconds from t0
    he3_sig: List[float] = field(default_factory=list)   # SEM signal

    # 4He sample signal (Faraday column, ActionToDo="4He")
    he4_t:   List[float] = field(default_factory=list)   # seconds from t0
    he4_sig: List[float] = field(default_factory=list)   # Faraday signal

    # 3He background (ActionToDo="3HeBackGround")
    he3_bg_t:   List[float] = field(default_factory=list)
    he3_bg_sig: List[float] = field(default_factory=list)

    # 4He background (ActionToDo="4HeBackGround")
    he4_bg_t:   List[float] = field(default_factory=list)
    he4_bg_sig: List[float] = field(default_factory=list)

    @property
    def n_he3_cycles(self) -> int:
        return len(self.he3_t)

    @property
    def n_he4_cycles(self) -> int:
        return len(self.he4_t)

    @property
    def n_bg_groups(self) -> int:
        """Number of background measurement groups (4He backgrounds, usually 4-5)."""
        return len(self.he4_bg_t)

    @property
    def is_blank(self) -> bool:
        return "blank" in self.description.lower()

    @property
    def is_standard(self) -> bool:
        d = self.description.lower()
        return "standard" in d or "spike" in d

    @property
    def is_sample(self) -> bool:
        return not self.is_blank and not self.is_standard


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(s: str) -> float:
    """Convert string to float; return NaN on empty or invalid input."""
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


def _parse_filename(path: str) -> tuple[int, str]:
    """
    Extract (inlet_num, description) from SMS filename.
    Falls back to (0, basename) when the pattern doesn't match.
    """
    basename = os.path.basename(path)
    m = _FNAME_RE.match(basename)
    if m:
        return int(m.group(2)), m.group(3).strip()
    log.warning("SMS filename does not match expected pattern: %s", basename)
    return 0, os.path.splitext(basename)[0]


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

def parse_sms(filepath: str) -> SMSInletData:
    """
    Parse one Isotopx SMS file and return an SMSInletData object.

    Parameters
    ----------
    filepath : str
        Absolute path to the .SMS file.

    Returns
    -------
    SMSInletData
        Populated with per-phase signal arrays.  Times are seconds from the
        first signal row (t0).

    Raises
    ------
    IOError
        If the file cannot be read.
    ValueError
        If the file header is missing required columns.
    """
    inlet_num, description = _parse_filename(filepath)
    lines = _read_lines(filepath)
    if not lines:
        raise IOError(f"Empty file: {filepath}")

    # -- parse header ---------------------------------------------------------
    header_cols = [c.strip().lower() for c in lines[0].rstrip("\r\n").split(";")]

    def _require(name: str) -> int:
        try:
            return header_cols.index(name)
        except ValueError:
            raise ValueError(f"SMS file {filepath}: missing column '{name}'")

    idx_lv      = _require("lvtime")
    idx_det     = _require("detector")
    idx_faraday = _require("faraday")
    idx_mult    = _require("multiplier")
    idx_action  = _require("actiontodo")

    # -- first pass: collect signal rows in chronological order ---------------
    # Accumulate (lv_time, signal, action) for signal phases only
    raw_rows: List[tuple] = []   # (lv_time: float, signal: float, action: str)

    for line_no, line in enumerate(lines[1:], start=2):
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        parts = line.split(";")

        def _col(idx: int) -> str:
            return parts[idx].strip() if idx < len(parts) else ""

        action = _col(idx_action)
        if action not in _SIGNAL_ACTIONS:
            continue

        lv_time = _safe_float(_col(idx_lv))
        if math.isnan(lv_time):
            log.debug("SMS %s line %d: invalid lvTime, skipping", filepath, line_no)
            continue

        # Use ActionToDo name (not Detector field) to select the channel.
        # 4HeBackGround rows have Detector=1 but signal is still in Faraday.
        if "4he" in action.lower():
            # 4He or 4HeBackGround → Faraday column
            signal = _safe_float(_col(idx_faraday))
        else:
            # 3He or 3HeBackGround → Multiplier/SEM column
            signal = _safe_float(_col(idx_mult))

        raw_rows.append((lv_time, signal, action))

    if not raw_rows:
        log.warning("SMS %s: no signal rows found", filepath)
        # Return empty but valid object
        return SMSInletData(
            filepath=filepath,
            inlet_num=inlet_num,
            description=description,
            t0_lv=0.0,
        )

    # -- t0: earliest timestamp among all signal rows -------------------------
    t0 = min(r[0] for r in raw_rows)

    # -- dispatch into per-phase arrays ---------------------------------------
    inlet = SMSInletData(
        filepath=filepath,
        inlet_num=inlet_num,
        description=description,
        t0_lv=t0,
    )

    for lv_time, signal, action in raw_rows:
        t_rel = lv_time - t0
        # Skip NaN signals (some background rows are all-zero or NaN)
        if math.isnan(signal):
            continue

        if action == "3He":
            inlet.he3_t.append(t_rel)
            inlet.he3_sig.append(signal)
        elif action == "4He":
            inlet.he4_t.append(t_rel)
            inlet.he4_sig.append(signal)
        elif action == "3HeBackGround":
            inlet.he3_bg_t.append(t_rel)
            inlet.he3_bg_sig.append(signal)
        elif action == "4HeBackGround":
            inlet.he4_bg_t.append(t_rel)
            inlet.he4_bg_sig.append(signal)

    log.debug(
        "SMS %s (inlet %d %s): %d 3He, %d 4He, %d 3HeBG, %d 4HeBG cycles",
        os.path.basename(filepath),
        inlet_num,
        description,
        inlet.n_he3_cycles,
        inlet.n_he4_cycles,
        len(inlet.he3_bg_t),
        len(inlet.he4_bg_t),
    )
    return inlet


# ---------------------------------------------------------------------------
# Sequence loader
# ---------------------------------------------------------------------------

def parse_sms_sequence(sms_paths: List[str]) -> List[SMSInletData]:
    """
    Parse multiple SMS files (one per inlet in a sequence).

    Parameters
    ----------
    sms_paths : list of str
        Absolute paths to .SMS files, in any order.  The returned list is
        sorted by inlet_num ascending.

    Returns
    -------
    list of SMSInletData
        One entry per file, sorted by inlet_num.
    """
    results: List[SMSInletData] = []
    for path in sms_paths:
        try:
            results.append(parse_sms(path))
        except Exception as exc:
            log.error("Failed to parse SMS file %s: %s", path, exc)
    results.sort(key=lambda x: x.inlet_num)
    return results
