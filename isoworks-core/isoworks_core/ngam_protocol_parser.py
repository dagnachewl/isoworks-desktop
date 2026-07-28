"""
ngam_protocol_parser.py
=======================
Pure parser for NobleControl .protocol files and associated MS measurement
files (.SMS, .QMSNe, .QMSAr, .QMSKrXe) and .InletState files.

No database or GUI dependencies.

Protocol file line format:
    YYYY-MM-DD HH:MM:SS; LV_TIMESTAMP; MESSAGE;

LV_TIMESTAMP is seconds since the LabVIEW epoch: 1904-01-01 00:00:00 UTC.

MS measurement files are semicolon-delimited CSV:
  SMS:    lvTime;Detector;Field;MassFaraday;MassMultiplier;Faraday;Multiplier;ActionToDo
  QMS*:   lvTime;Detector;Mass;Faraday;Multiplier;ActionToDo

InletState files are semicolon-delimited CSV with a header row.
"""
from __future__ import annotations

import logging
import math
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_LV_EPOCH = datetime(1904, 1, 1, tzinfo=timezone.utc)

_ENCODINGS = ["utf-8-sig", "utf-8", "latin-1"]

# Known MS device extensions / identifiers
_DEVICES = ("SMS", "QMSNe", "QMSAr", "QMSKrXe")

# Regex patterns for protocol message lines
_RE_SEQ_START       = re.compile(r"^Start of Sequence Measurement\s*$")
_RE_SEQ_END         = re.compile(r"^End of Sequence Measurement\s*$")
_RE_SEQ_DESC        = re.compile(r"Sequence Description:\s*(.*)")
_RE_INLET_START     = re.compile(r"^Start of InSequence Number\s+(\d+)\s*$")
_RE_INLET_END       = re.compile(r"^End of InSequence Number\s+(\d+)\s*$")
_RE_INLET_DESC      = re.compile(r"^Inlet Description of InSequence Number\s+(\d+):\s*(.*)")
_RE_REF_GAS         = re.compile(
    r"Reference Gas inlet of\s+([\d.eE+\-]+)\s*ccSTP\s+(\S+)\s+completed"
)
_RE_MS_PATH         = re.compile(
    r"Data for the measurement of InSequence Number\s+(\d+)\s+"
    r"measured in the\s+(\S+)\s+can be found in Path\s+(.*)"
)
_RE_MS_NUCLIDES     = re.compile(
    r"Data for the measurement of InSequence Number\s+(\d+)\s+"
    r"measured in the\s+(\S+)\s+can be evaluated for\s+(.*)"
)
_RE_PARTITION_STEPS = re.compile(
    r"Variable PartitioningSteps(Helium|Neon|Argon)\s+set\s+to\s+([\d.eE+\-]+)"
)

# SRG measurement events — used to derive per-inlet net SRG signals from the protocol log.
# He: "Desprption" is a known NobleControl typo (double-p); regex accepts any one-word
# variant ending in "tion" so it survives a potential future spelling correction.
_RE_SRG_HE_STEP = re.compile(
    r"^Helium \w+tion StepPressure of InSequence Number\s+(\d+)\s+"
    r"and SRG StepSize was:\s+([\d.eE+\-]+)"
)
_RE_SRG_HE_DRIFT_START = re.compile(
    r"^Start of Helium SRG drift of InSequence Number\s+(\d+)\s+and SRG reads:"
)
_RE_SRG_HE_RELEASE = re.compile(
    r"^Helium Release ready of InSequence Number\s+(\d+)\s+and SRG reads:"
)
_RE_SRG_NE_DRIFT_START = re.compile(
    r"^Start of Neon SRG drift of InSequence Number\s+(\d+)\s+and SRG reads:\s+([\d.eE+\-]+)"
)
_RE_SRG_NE_RELEASE = re.compile(
    r"^Neon Release ready of InSequence Number\s+(\d+)\s+and SRG reads:\s+([\d.eE+\-]+)"
)
_RE_SRG_AR_START = re.compile(
    r"^Start of Argon measurement of InSequence Number\s+(\d+)\s+and SRGAr reads:\s+([\d.eE+\-]+)"
)
_RE_SRG_AR_END = re.compile(
    r"^End of Argon SRG measurement of InSequence Number\s+(\d+)\s+and SRGAr reads:\s+([\d.eE+\-]+)"
)
_RE_AR_PUMPDOWN = re.compile(
    r"^Start of Argon PumpDown of InSequence Number\s+(\d+)\s+in QMSAr"
)


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def lv_to_datetime(lv_ts: float) -> datetime:
    """Convert a LabVIEW timestamp (seconds since 1904-01-01 UTC) to a UTC datetime."""
    return _LV_EPOCH + timedelta(seconds=lv_ts)


def lv_to_local_str(lv_ts: float) -> str:
    """Format a LabVIEW timestamp as 'YYYY-MM-DD HH:MM:SS' in local time."""
    utc_dt = lv_to_datetime(lv_ts)
    local_dt = utc_dt.astimezone()
    return local_dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class MSSignalRow:
    """One signal row from an MS measurement file."""
    lv_time: float
    action: str
    signal: float        # selected signal value (Faraday or Multiplier)
    on_faraday: bool     # True when Detector == 0


@dataclass
class MSMeasurementData:
    """Parsed data from one MS measurement file."""
    device: str                     # 'SMS' | 'QMSNe' | 'QMSAr' | 'QMSKrXe'
    original_path: str              # Windows path as written in the protocol
    resolved_path: Optional[str]    # Absolute local path, or None if not found
    inlet_num: int
    nuclides: List[str]
    signals: List[MSSignalRow]
    bg_proxy_choices: Dict[str, str] = field(default_factory=dict)

    @property
    def actions(self) -> List[str]:
        """Unique non-Scan ActionToDo values in order of first appearance."""
        seen: Dict[str, None] = {}
        for row in self.signals:
            if not row.action.lower().startswith("scan"):
                seen[row.action] = None
        return list(seen.keys())

    def signals_for_action(self, action: str) -> List[MSSignalRow]:
        """All signal rows for a given ActionToDo name."""
        return [r for r in self.signals if r.action == action]

    @property
    def blocks(self) -> Dict[str, bool]:
        """
        Mapping of action_name -> on_faraday for all non-Scan blocks.
        on_faraday is True when ALL signals in that block have on_faraday=True.
        """
        result: Dict[str, bool] = {}
        for action in self.actions:
            rows = self.signals_for_action(action)
            result[action] = all(r.on_faraday for r in rows)
        return result


@dataclass
class InletStateData:
    """Parsed data from a .InletState file."""
    columns: List[str]
    rows: List[Dict[str, float]]

    def slice(self, t_start: float, t_end: float) -> "InletStateData":
        """Return a new InletStateData containing only rows where t_start <= lvTime <= t_end."""
        sliced = [r for r in self.rows if t_start <= r.get("lvTime", float("nan")) <= t_end]
        return InletStateData(columns=list(self.columns), rows=sliced)


@dataclass
class InletPrep:
    """Data for one inlet/preparation within a sequence."""
    seq_num: int
    lv_time_start: float
    lv_time_end: Optional[float]
    inlet_string: str           # e.g. "Blank", "Standard_SpikeLarge4"
    is_reference: bool          # True if reference gas was introduced
    reference_gas: str
    reference_amount: float     # ccSTP
    ms_data: List[MSMeasurementData]
    dilution_factor: float = 1.0
    partition_steps: Dict[str, int] = field(default_factory=lambda: {"Helium": 0, "Neon": 0, "Argon": 0})

    # Per-isotope reference amounts: "{device}:{isotope}" → amount (same unit as sensitivity).
    # Set by the processing bridge for instrument-specific calibration (e.g. Helix SFT 3He
    # standards whose certified value is per-isotope, not a single scalar).
    # Takes lower priority than repro_references passed to process_sequence(), higher than
    # the scalar reference_amount.
    per_isotope_amounts: Dict[str, float] = field(default_factory=dict)

    # Set to True for inlets merged from other runs (used only for calibration)
    is_supplemental: bool = False
    source_protocol: str = ""   # path of the originating .protocol file
    source_seq_num: int = 0     # original seq_num within the source file

    # Per-inlet SRG net signals (Amperes) derived from the .protocol log.
    # He: "Desprtion StepPressure" StepSize — closest available proxy for XLSM GaugeSignals net.
    # Ne: "Neon Release ready SRG reads" minus "Start of Neon SRG drift SRG reads".
    # Ar: "End of Argon SRG measurement SRGAr reads" minus "Start of Argon measurement SRGAr reads".
    # nan when the relevant protocol events were not found.
    srg_he_step: float = field(default_factory=lambda: float("nan"))
    srg_ne_net: float = field(default_factory=lambda: float("nan"))
    srg_ar_net: float = field(default_factory=lambda: float("nan"))

    # LVT timestamps of per-inlet SRG measurement phase boundaries.
    # Used to highlight measurement windows on LVT vs SRG signal plots.
    # nan when the event was not found in the protocol log.
    t_he_srg_start: float = field(default_factory=lambda: float("nan"))
    t_he_srg_end: float = field(default_factory=lambda: float("nan"))
    t_ne_srg_start: float = field(default_factory=lambda: float("nan"))
    t_ne_srg_end: float = field(default_factory=lambda: float("nan"))
    t_ar_srg_start: float = field(default_factory=lambda: float("nan"))
    t_ar_srg_end: float = field(default_factory=lambda: float("nan"))
    t_ar_pumpdown: float = field(default_factory=lambda: float("nan"))

    @property
    def is_blank(self) -> bool:
        return "blank" in self.inlet_string.lower()

    @property
    def ms_by_device(self) -> Dict[str, MSMeasurementData]:
        return {ms.device: ms for ms in self.ms_data}

    def get_ms(self, device: str) -> Optional[MSMeasurementData]:
        return self.ms_by_device.get(device)


@dataclass
class ProtocolSequence:
    """Complete parsed result of a .protocol file."""
    protocol_path: str
    inlet_state_path: Optional[str]
    lv_time_start: float
    lv_time_end: Optional[float]
    description: str
    inlets: List[InletPrep]
    inlet_state: Optional[InletStateData]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _safe_float(s: str) -> float:
    """Convert a string to float; return 0.0 on failure or NaN."""
    s = s.strip()
    if not s:
        return 0.0
    try:
        val = float(s)
        return 0.0 if math.isnan(val) else val
    except (ValueError, OverflowError):
        return 0.0


def _read_lines(path: str) -> List[str]:
    """Read all lines from a file, trying multiple encodings."""
    for enc in _ENCODINGS:
        try:
            with open(path, "r", encoding=enc, errors="replace") as fh:
                return fh.readlines()
        except Exception as exc:
            log.debug("Encoding %s failed for %s: %s", enc, path, exc)
    raise IOError(f"Could not read file: {path}")
def _get_data_directories() -> List[str]:
    """Returns a list of candidate directories where the 'Data' folder might reside."""
    candidates = []
    
    # 1. Container mount: /workspace/Data
    candidates.append("/workspace/Data")
    
    # 2. Current working directory: os.getcwd()/Data
    candidates.append(os.path.join(os.getcwd(), "Data"))
    
    # 3. Main script entrypoint directory: sys.path[0]/Data (and its parent)
    if sys.path and sys.path[0]:
        candidates.append(os.path.join(sys.path[0], "Data"))
        candidates.append(os.path.join(os.path.dirname(sys.path[0]), "Data"))
        
    # 4. Fallback: package location site-packages/isoworks_core/Data
    parser_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(parser_dir, "Data"))
    
    # Return unique, existing folders
    seen = set()
    result = []
    for c in candidates:
        abs_c = os.path.abspath(c)
        if abs_c not in seen:
            seen.add(abs_c)
            result.append(abs_c)
    return result


def _resolve_path(windows_path: str, protocol_dir: str) -> Optional[str]:
    """
    Resolve a Windows path written in the protocol to an absolute local path.
    Only the basename is used; the file is searched for recursively inside the
    uploaded protocol/run workspace folder (protocol_dir).
    
    If not found, falls back to searching the local project workspace's Data directory.
    """
    windows_path = windows_path.strip().rstrip(";").strip()
    if not windows_path:
        return None
    basename = os.path.basename(windows_path.replace("\\", "/"))
    if not basename:
        return None

    # Check recursively inside the uploaded run workspace directory first
    if os.path.isdir(protocol_dir):
        for root, dirs, files in os.walk(protocol_dir):
            if basename in files:
                found = os.path.join(root, basename)
                return found

    # Sibling fallback: search in local workspace Data folders recursively
    for data_dir in _get_data_directories():
        if os.path.isdir(data_dir):
            for root, dirs, files in os.walk(data_dir):
                if basename in files:
                    found = os.path.join(root, basename)
                    log.info(f"Resolved sibling file from Data fallback ({data_dir}): {found}")
                    return found
                
    return None



# ---------------------------------------------------------------------------
# MS file loader
# ---------------------------------------------------------------------------

def load_ms_file(path: str, device: str) -> List[MSSignalRow]:
    """
    Read one MS measurement file and return signal rows.

    Scan* ActionToDo values are included in the raw list so that callers can
    filter them out when needed (they are excluded from MSMeasurementData.blocks
    and .actions but retained in .signals for completeness).

    Actually, per spec: load_ms_file returns all rows including Scan*.
    The blocks/actions properties of MSMeasurementData skip Scan* at query time.
    """
    lines = _read_lines(path)
    if not lines:
        return []

    rows: List[MSSignalRow] = []

    # Detect device type from the header line
    header_raw = lines[0].rstrip("\r\n")
    header_cols = [c.strip().lower() for c in header_raw.split(";")]

    # Determine column indices
    try:
        idx_lvtime   = header_cols.index("lvtime")
        idx_detector = header_cols.index("detector")
        idx_action   = header_cols.index("actiontodo")
    except ValueError as exc:
        log.warning("MS file %s missing expected header column: %s", path, exc)
        return []

    # Faraday and Multiplier column indices vary by device type
    if device == "SMS":
        # lvTime;Detector;Field;MassFaraday;MassMultiplier;Faraday;Multiplier;ActionToDo
        try:
            idx_faraday    = header_cols.index("faraday")
            idx_multiplier = header_cols.index("multiplier")
        except ValueError:
            log.warning("SMS file %s missing Faraday/Multiplier columns", path)
            return []
    else:
        # QMS* format: lvTime;Detector;Mass;Faraday;Multiplier;ActionToDo
        try:
            idx_faraday    = header_cols.index("faraday")
            idx_multiplier = header_cols.index("multiplier")
        except ValueError:
            log.warning("QMS file %s missing Faraday/Multiplier columns", path)
            return []

    for line_no, line in enumerate(lines[1:], start=2):
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        parts = line.split(";")

        def _col(idx: int) -> str:
            return parts[idx].strip() if idx < len(parts) else ""

        try:
            lv_time  = float(_col(idx_lvtime))  if _col(idx_lvtime)   else float("nan")
            detector = float(_col(idx_detector)) if _col(idx_detector) else 0.0
            action   = _col(idx_action)
        except (ValueError, IndexError):
            log.debug("MS file %s line %d: parse error, skipping", path, line_no)
            continue

        if math.isnan(lv_time):
            continue

        faraday_raw    = _col(idx_faraday)
        multiplier_raw = _col(idx_multiplier)
        faraday_val    = _safe_float(faraday_raw)
        multiplier_val = _safe_float(multiplier_raw)

        # Prefer Faraday when it carries a real value; fall back to Multiplier.
        # QMS files always store data in Faraday (Detector=4, Multiplier=NaN).
        # SMS 3He rows store data in Multiplier (Detector≠0, Faraday empty).
        if faraday_raw.strip() and not math.isnan(faraday_val):
            signal     = faraday_val
            on_faraday = True
        else:
            signal     = multiplier_val
            on_faraday = False

        rows.append(MSSignalRow(
            lv_time=lv_time,
            action=action,
            signal=signal,
            on_faraday=on_faraday,
        ))

    return rows


# ---------------------------------------------------------------------------
# InletState loader
# ---------------------------------------------------------------------------

_INLET_STATE_COLS = [
    "lvTime", "BaratronInlet", "SRGHeNe", "SRGAr",
    "PumpSRG", "ToSRG",
    "PiraniInletTurbo", "PiraniSRG", "PiraniQMSAr",
    "PiraniNGSep", "PiraniHeNeSep",
]


def load_inlet_state(path: str) -> InletStateData:
    """Read a .InletState semicolon-delimited CSV file."""
    lines = _read_lines(path)
    if not lines:
        return InletStateData(columns=list(_INLET_STATE_COLS), rows=[])

    header_raw = lines[0].rstrip("\r\n")
    all_cols = [c.strip() for c in header_raw.split(";")]

    # Build index map for known columns (case-insensitive)
    col_lower = {c.lower(): i for i, c in enumerate(all_cols)}
    wanted_indices: Dict[str, int] = {}
    for col in _INLET_STATE_COLS:
        idx = col_lower.get(col.lower())
        if idx is not None:
            wanted_indices[col] = idx
        else:
            log.debug("InletState file %s: column %r not found", path, col)

    present_cols = [c for c in _INLET_STATE_COLS if c in wanted_indices]
    result_rows: List[Dict[str, float]] = []

    for line in lines[1:]:
        line = line.rstrip("\r\n")
        if not line.strip():
            continue
        parts = line.split(";")
        row_dict: Dict[str, float] = {}
        for col in present_cols:
            idx = wanted_indices[col]
            raw = parts[idx].strip() if idx < len(parts) else ""
            try:
                row_dict[col] = float(raw)
            except (ValueError, OverflowError):
                row_dict[col] = float("nan")
        result_rows.append(row_dict)

    return InletStateData(columns=present_cols, rows=result_rows)


# ---------------------------------------------------------------------------
# Protocol parser
# ---------------------------------------------------------------------------

def parse_protocol(
    path: str,
    load_ms_files: bool = True,
    read_inlet_state: bool = True,
) -> ProtocolSequence:
    """
    Parse a NobleControl .protocol file and return a ProtocolSequence.

    Parameters
    ----------
    path : str
        Absolute path to the .protocol file.
    load_ms_files : bool
        If True, read each MS measurement file referenced by the protocol.
    read_inlet_state : bool
        If True, read the companion .InletState file (same basename, different extension).

    Returns
    -------
    ProtocolSequence
    """
    protocol_dir = os.path.dirname(os.path.abspath(path))
    lines = _read_lines(path)

    # Derived .InletState path
    inlet_state_path = os.path.splitext(path)[0] + ".InletState"
    if not os.path.isfile(inlet_state_path):
        inlet_state_path_result = None
        basename = os.path.basename(inlet_state_path)
        for data_dir in _get_data_directories():
            if os.path.isdir(data_dir):
                for root, dirs, files in os.walk(data_dir):
                    if basename in files:
                        inlet_state_path_result = os.path.join(root, basename)
                        log.info(f"Resolved sibling InletState file from Data fallback ({data_dir}): {inlet_state_path_result}")
                        break
                if inlet_state_path_result:
                    break
    else:
        inlet_state_path_result = inlet_state_path
    # --- state collected during single-pass parse ---
    seq_lv_start: float        = 0.0
    seq_lv_end: Optional[float] = None
    description: str           = ""
    current_seq_num: Optional[int] = None
    inlet_partition_steps: Dict[int, Dict[str, int]] = {}

    # Per-inlet accumulators (keyed by seq_num int)
    inlet_starts:   Dict[int, float]       = {}
    inlet_ends:     Dict[int, float]       = {}
    inlet_names:    Dict[int, str]         = {}
    inlet_ref_gas:  Dict[int, str]         = {}
    inlet_ref_amt:  Dict[int, float]       = {}
    inlet_is_ref:   Dict[int, bool]        = {}

    # MS data accumulators: (inlet_num, device) -> MSMeasurementData (partially built)
    # We store original_path, resolved_path, nuclides first; signals loaded after parse
    ms_paths:     Dict[tuple, str]        = {}   # (inlet_num, device) -> original windows path
    ms_nuclides:  Dict[tuple, List[str]]  = {}   # (inlet_num, device) -> nuclides list

    # SRG signal accumulators (from protocol log events)
    inlet_srg_he_step:    Dict[int, float] = {}   # He: Desprtion StepPressure StepSize (A)
    inlet_srg_he_drift_t: Dict[int, float] = {}   # He: LVT of "Start of Helium SRG drift"
    inlet_srg_he_rel_t:   Dict[int, float] = {}   # He: LVT of "Helium Release ready"
    inlet_srg_ne_drift:   Dict[int, float] = {}   # Ne: Start of Neon SRG drift reads (A)
    inlet_srg_ne_drift_t: Dict[int, float] = {}   # Ne: LVT of "Start of Neon SRG drift"
    inlet_srg_ne_rel:     Dict[int, float] = {}   # Ne: Neon Release ready reads (A)
    inlet_srg_ne_rel_t:   Dict[int, float] = {}   # Ne: LVT of "Neon Release ready"
    inlet_srg_ar_start:    Dict[int, float] = {}   # Ar: Start of Argon measurement SRGAr (A)
    inlet_srg_ar_start_t:  Dict[int, float] = {}   # Ar: LVT of "Start of Argon measurement"
    inlet_srg_ar_end:      Dict[int, float] = {}   # Ar: End of Argon SRG measurement SRGAr (A)
    inlet_srg_ar_end_t:    Dict[int, float] = {}   # Ar: LVT of "End of Argon SRG measurement"
    inlet_srg_ar_pumpdown: Dict[int, float] = {}   # Ar: LVT of "Start of Argon PumpDown"

    def _parse_line(raw: str):
        """Parse one protocol line; updates the mutable dicts above."""
        nonlocal seq_lv_start, seq_lv_end, description, current_seq_num

        raw = raw.rstrip("\r\n")
        if not raw.strip():
            return

        parts = raw.split(";", 2)
        if len(parts) < 3:
            return

        lv_ts_str = parts[1].strip()
        msg       = parts[2].strip().rstrip(";").strip()

        try:
            lv_ts = float(lv_ts_str)
        except ValueError:
            lv_ts = 0.0

        # --- Sequence start / end ---
        if _RE_SEQ_START.match(msg):
            seq_lv_start = lv_ts
            return

        if _RE_SEQ_END.match(msg):
            seq_lv_end = lv_ts
            return

        # --- Sequence description ---
        m = _RE_SEQ_DESC.search(msg)
        if m:
            description = m.group(1).strip()
            return

        # --- Inlet start ---
        m = _RE_INLET_START.match(msg)
        if m:
            n = int(m.group(1))
            inlet_starts[n] = lv_ts
            current_seq_num = n
            return

        # --- Inlet end ---
        m = _RE_INLET_END.match(msg)
        if m:
            n = int(m.group(1))
            inlet_ends[n] = lv_ts
            if current_seq_num == n:
                current_seq_num = None
            return

        # --- Inlet description ---
        m = _RE_INLET_DESC.match(msg)
        if m:
            n    = int(m.group(1))
            name = m.group(2).strip()
            inlet_names[n] = name
            return

        # --- Reference gas ---
        m = _RE_REF_GAS.search(msg)
        if m:
            amount   = float(m.group(1))
            gas_name = m.group(2).strip()
            # Assign to the last inlet whose lv_time_start <= lv_ts
            target_n: Optional[int] = None
            target_t: float        = -1.0
            for n, t in inlet_starts.items():
                if t <= lv_ts and t > target_t:
                    target_n = n
                    target_t = t
            if target_n is not None:
                inlet_ref_gas[target_n] = gas_name
                inlet_ref_amt[target_n] = amount
                inlet_is_ref[target_n]  = True
            return

        # --- MS file path ---
        m = _RE_MS_PATH.match(msg)
        if m:
            n       = int(m.group(1))
            device  = m.group(2).strip()
            win_path = m.group(3).strip()
            if win_path:  # ignore empty paths (protocol writes blank line first)
                key = (n, device)
                ms_paths[key] = win_path
            return

        # --- MS nuclides ---
        m = _RE_MS_NUCLIDES.match(msg)
        if m:
            n       = int(m.group(1))
            device  = m.group(2).strip()
            nuc_raw = m.group(3).strip()
            # Nuclides are listed as \nuc1\nuc2\... (backslash-delimited)
            nuclides = [x.strip() for x in nuc_raw.split("\\") if x.strip()]
            key = (n, device)
            ms_nuclides[key] = nuclides
            return

        # --- Partitioning steps ---
        if current_seq_num is not None:
            m_part = _RE_PARTITION_STEPS.search(msg)
            if m_part:
                gas = m_part.group(1)
                try:
                    val = int(float(m_part.group(2)))
                except (ValueError, TypeError):
                    val = 0
                if current_seq_num not in inlet_partition_steps:
                    inlet_partition_steps[current_seq_num] = {"Helium": 0, "Neon": 0, "Argon": 0}
                inlet_partition_steps[current_seq_num][gas] = max(
                    inlet_partition_steps[current_seq_num].get(gas, 0), val
                )

        # --- SRG measurement events ---
        m = _RE_SRG_HE_STEP.match(msg)
        if m:
            inlet_srg_he_step[int(m.group(1))] = float(m.group(2))
            return
        m = _RE_SRG_HE_DRIFT_START.match(msg)
        if m:
            inlet_srg_he_drift_t[int(m.group(1))] = lv_ts
            return
        m = _RE_SRG_HE_RELEASE.match(msg)
        if m:
            inlet_srg_he_rel_t[int(m.group(1))] = lv_ts
            return
        m = _RE_SRG_NE_DRIFT_START.match(msg)
        if m:
            n = int(m.group(1))
            inlet_srg_ne_drift[n] = float(m.group(2))
            inlet_srg_ne_drift_t[n] = lv_ts
            return
        m = _RE_SRG_NE_RELEASE.match(msg)
        if m:
            n = int(m.group(1))
            inlet_srg_ne_rel[n] = float(m.group(2))
            inlet_srg_ne_rel_t[n] = lv_ts
            return
        m = _RE_SRG_AR_START.match(msg)
        if m:
            n = int(m.group(1))
            inlet_srg_ar_start[n] = float(m.group(2))
            inlet_srg_ar_start_t[n] = lv_ts
            return
        m = _RE_SRG_AR_END.match(msg)
        if m:
            n = int(m.group(1))
            inlet_srg_ar_end[n] = float(m.group(2))
            inlet_srg_ar_end_t[n] = lv_ts
            return
        m = _RE_AR_PUMPDOWN.match(msg)
        if m:
            n = int(m.group(1))
            inlet_srg_ar_pumpdown[n] = lv_ts
            return

    # Single pass
    for line in lines:
        _parse_line(line)

    # --- Build MSMeasurementData objects ---
    # Collect all (inlet_num, device) keys
    all_ms_keys = set(ms_paths.keys()) | set(ms_nuclides.keys())

    ms_by_inlet: Dict[int, List[MSMeasurementData]] = {}

    for (inlet_num, device) in sorted(all_ms_keys, key=lambda k: (k[0], k[1])):
        win_path   = ms_paths.get((inlet_num, device), "")
        nuclides   = ms_nuclides.get((inlet_num, device), [])
        resolved   = _resolve_path(win_path, protocol_dir) if win_path else None

        signals: List[MSSignalRow] = []
        if load_ms_files and resolved is not None:
            try:
                signals = load_ms_file(resolved, device)
            except Exception as exc:
                log.warning(
                    "Could not load MS file %s for inlet %d device %s: %s",
                    resolved, inlet_num, device, exc
                )

        ms_obj = MSMeasurementData(
            device=device,
            original_path=win_path,
            resolved_path=resolved,
            inlet_num=inlet_num,
            nuclides=nuclides,
            signals=signals,
        )
        ms_by_inlet.setdefault(inlet_num, []).append(ms_obj)

    # --- Assemble InletPrep list ---
    all_inlet_nums = sorted(
        set(inlet_starts.keys())
        | set(inlet_ends.keys())
        | set(inlet_names.keys())
    )

    inlets: List[InletPrep] = []
    for n in all_inlet_nums:
        # Compute per-inlet SRG net signals
        ne_drift = inlet_srg_ne_drift.get(n, float("nan"))
        ne_rel   = inlet_srg_ne_rel.get(n, float("nan"))
        srg_ne   = ne_rel - ne_drift if not (math.isnan(ne_drift) or math.isnan(ne_rel)) else float("nan")
        ar_s     = inlet_srg_ar_start.get(n, float("nan"))
        ar_e     = inlet_srg_ar_end.get(n, float("nan"))
        srg_ar   = ar_e - ar_s if not (math.isnan(ar_s) or math.isnan(ar_e)) else float("nan")

        prep = InletPrep(
            seq_num=n,
            lv_time_start=inlet_starts.get(n, 0.0),
            lv_time_end=inlet_ends.get(n),
            inlet_string=inlet_names.get(n, ""),
            is_reference=inlet_is_ref.get(n, False),
            reference_gas=inlet_ref_gas.get(n, ""),
            reference_amount=inlet_ref_amt.get(n, 0.0),
            ms_data=ms_by_inlet.get(n, []),
            dilution_factor=1.0,
            partition_steps=inlet_partition_steps.get(n, {"Helium": 0, "Neon": 0, "Argon": 0}),
            srg_he_step=inlet_srg_he_step.get(n, float("nan")),
            srg_ne_net=srg_ne,
            srg_ar_net=srg_ar,
            t_he_srg_start=inlet_srg_he_drift_t.get(n, float("nan")),
            t_he_srg_end=inlet_srg_he_rel_t.get(n, float("nan")),
            t_ne_srg_start=inlet_srg_ne_drift_t.get(n, float("nan")),
            t_ne_srg_end=inlet_srg_ne_rel_t.get(n, float("nan")),
            t_ar_srg_start=inlet_srg_ar_start_t.get(n, float("nan")),
            t_ar_srg_end=inlet_srg_ar_end_t.get(n, float("nan")),
            t_ar_pumpdown=inlet_srg_ar_pumpdown.get(n, float("nan")),
        )
        inlets.append(prep)

    # --- Load InletState ---
    inlet_state_data: Optional[InletStateData] = None
    if read_inlet_state and inlet_state_path_result is not None:
        try:
            inlet_state_data = load_inlet_state(inlet_state_path_result)
        except Exception as exc:
            log.warning("Could not load InletState file %s: %s", inlet_state_path_result, exc)

    return ProtocolSequence(
        protocol_path=os.path.abspath(path),
        inlet_state_path=inlet_state_path_result,
        lv_time_start=seq_lv_start,
        lv_time_end=seq_lv_end,
        description=description,
        inlets=inlets,
        inlet_state=inlet_state_data,
    )


# ---------------------------------------------------------------------------
# Multi-run merging
# ---------------------------------------------------------------------------

import dataclasses as _dc


def merge_sequences(
    primary: ProtocolSequence,
    supplemental: List[InletPrep],
) -> ProtocolSequence:
    """
    Return a new ProtocolSequence where *supplemental* inlets (blanks/standards
    from other runs) are inserted in chronological order alongside the primary
    run's inlets.

    Supplemental inlets are renumbered so their seq_nums don't collide with
    the primary run.  The original seq_num is preserved in InletPrep.source_seq_num.
    The resulting sequence keeps the primary run's metadata (path, timestamps, etc.).
    """
    max_primary = max((p.seq_num for p in primary.inlets), default=0)
    extra: List[InletPrep] = []
    for i, prep in enumerate(supplemental):
        extra.append(
            _dc.replace(
                prep,
                seq_num=max_primary + i + 1,
                is_supplemental=True,
                source_seq_num=prep.seq_num if prep.source_seq_num == 0 else prep.source_seq_num,
            )
        )

    merged = sorted(
        list(primary.inlets) + extra,
        key=lambda p: p.lv_time_start,
    )
    return _dc.replace(primary, inlets=merged)
