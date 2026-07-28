"""
ngam_ms_parser.py
=================
Pure parser for Thermo Fisher Helix SFT semicolon-delimited CSV files.
No database or GUI dependencies.

CSV layout (0-indexed rows):
  Row 0  : ;;;;Sample 1;Sample 2;...;Sample N;
  Row 1  : ;;;;description_1;description_2;...;description_N;
  Data rows: block;cycle_idx;detector;data_type;val0;val1;...
  Metadata : StartTime / StopTime / Info rows (col 0 key, col 1 sub-key)
"""
from __future__ import annotations

import re
import math
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

log = logging.getLogger(__name__)

# Detector identifiers
_HE4_DETECTOR = "1:Cup:4.003"
_HE3_DETECTOR = "1:SEM:3.016"

_ENCODINGS = ["utf-8-sig", "utf-8", "latin-1"]

# ISO 8601 timestamps from the Helix SFT can contain 7 fractional-second
# digits which datetime.fromisoformat() rejects on Python < 3.11.
_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\.(\d+)")

_SAMPLE_RE = re.compile(r"^Sample\s+(\d+)$", re.IGNORECASE)


def _parse_timestamp(raw: str) -> Optional[datetime]:
    """Parse ISO8601 string, truncating fractional seconds to 6 digits."""
    raw = raw.strip()
    if not raw:
        return None
    m = _TS_RE.match(raw)
    if m:
        frac = m.group(2)[:6].ljust(6, "0")
        raw = f"{m.group(1)}.{frac}"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        log.warning("Could not parse timestamp: %r", raw)
        return None


def _safe_float(s: str) -> float:
    """Convert string to float; return NaN on failure."""
    s = s.strip()
    if not s:
        return float("nan")
    try:
        return float(s)
    except ValueError:
        return float("nan")


@dataclass
class PositionData:
    """Data for a single sample position (column) in the Helix SFT run."""

    index: int                     # 0-based column index in CSV sample columns
    position_num: int              # 1-based (Sample 1 → 1)
    label: str                     # "Sample 1"
    description: str               # e.g. "Blank - full proc - v6"
    start_time: Optional[datetime]
    stop_time: Optional[datetime]
    is_valid: bool                 # False when description is empty
    he4_y: List[float] = field(default_factory=list)
    he3_y: List[float] = field(default_factory=list)
    he4_times: List[float] = field(default_factory=list)
    he3_times: List[float] = field(default_factory=list)

    # ── computed properties ────────────────────────────────────────────────────

    @property
    def mean_he4(self) -> float:
        vals = [v for v in self.he4_y if not math.isnan(v)]
        return sum(vals) / len(vals) if vals else float("nan")

    @property
    def mean_he3(self) -> float:
        vals = [v for v in self.he3_y if not math.isnan(v)]
        return sum(vals) / len(vals) if vals else float("nan")

    @property
    def se_he3(self) -> float:
        vals = [v for v in self.he3_y if not math.isnan(v)]
        n = len(vals)
        if n < 2:
            return float("nan")
        mean = sum(vals) / n
        variance = sum((v - mean) ** 2 for v in vals) / (n - 1)
        return math.sqrt(variance / n)

    @property
    def duration_s(self) -> Optional[float]:
        if self.start_time and self.stop_time:
            return (self.stop_time - self.start_time).total_seconds()
        return None


@dataclass
class HelixSFTRun:
    """Parsed result of a single Helix SFT CSV export."""

    filepath: str
    positions: List[PositionData]
    n_cycles: int

    @property
    def valid_positions(self) -> List[PositionData]:
        return [p for p in self.positions if p.is_valid]


# ─────────────────────────────── parser ───────────────────────────────────────

def parse_helix_sft(filepath: str) -> HelixSFTRun:
    """
    Parse a Helix SFT semicolon-delimited CSV export.

    Parameters
    ----------
    filepath : str
        Absolute path to the CSV file.

    Returns
    -------
    HelixSFTRun
        Fully populated run object.
    """
    raw_lines: List[str] = []
    for enc in _ENCODINGS:
        try:
            with open(filepath, "r", encoding=enc, errors="replace") as fh:
                raw_lines = fh.readlines()
            break
        except Exception as exc:
            log.debug("Encoding %s failed for %s: %s", enc, filepath, exc)

    if not raw_lines:
        raise IOError(f"Could not read file: {filepath}")

    # Split all rows on semicolons, stripping trailing newlines
    rows: List[List[str]] = [
        line.rstrip("\r\n").split(";") for line in raw_lines
    ]

    # ── Row 0: locate sample columns ──────────────────────────────────────────
    # Format: ;;;;Sample 1;Sample 2;...
    if len(rows) < 2:
        raise ValueError("File too short; expected at least 2 header rows.")

    header_row = rows[0]
    desc_row = rows[1]

    # Find all "Sample N" columns; collect (csv_col_index, position_num, label)
    sample_cols: List[tuple] = []  # (csv_col_idx, position_num, label)
    for col_idx, cell in enumerate(header_row):
        m = _SAMPLE_RE.match(cell.strip())
        if m:
            sample_cols.append((col_idx, int(m.group(1)), cell.strip()))

    if not sample_cols:
        raise ValueError("No 'Sample N' columns found in row 0.")

    n_positions = len(sample_cols)

    # ── Build PositionData skeletons ──────────────────────────────────────────
    positions: List[PositionData] = []
    for idx, (csv_col, pos_num, label) in enumerate(sample_cols):
        desc = desc_row[csv_col].strip() if csv_col < len(desc_row) else ""
        pos = PositionData(
            index=idx,
            position_num=pos_num,
            label=label,
            description=desc,
            start_time=None,
            stop_time=None,
            is_valid=bool(desc),
        )
        positions.append(pos)

    # Map csv_col_idx → PositionData for fast lookup
    col_to_pos = {csv_col: pos for (csv_col, _, _), pos in zip(sample_cols, positions)}
    # Also build index-within-samples → PositionData for ordered access
    # (used for metadata rows where values start at col 4)

    # ── Parse data and metadata rows ──────────────────────────────────────────
    max_cycle: int = -1

    for row in rows[2:]:
        if not row:
            continue
        block = row[0].strip()

        # ── metadata rows ─────────────────────────────────────────────────────
        if block in ("StartTime", "StopTime", "Info"):
            # values start at col index 4 (one per sample position)
            for i, (csv_col, _, _) in enumerate(sample_cols):
                val_idx = 4 + i
                val = row[val_idx].strip() if val_idx < len(row) else ""
                pos = positions[i]
                if block == "StartTime":
                    pos.start_time = _parse_timestamp(val)
                elif block == "StopTime":
                    pos.stop_time = _parse_timestamp(val)
                # "Info" rows are informational only — skip
            continue

        # ── only process MainRuns blocks ──────────────────────────────────────
        if block != "MainRuns":
            continue

        # Expect at minimum: block, cycle_idx, detector, data_type
        if len(row) < 4:
            continue

        cycle_str = row[1].strip()
        detector = row[2].strip()
        data_type = row[3].strip()

        if data_type not in ("Y", "Time"):
            continue
        if detector not in (_HE4_DETECTOR, _HE3_DETECTOR):
            continue

        try:
            cycle_idx = int(cycle_str)
        except ValueError:
            continue

        max_cycle = max(max_cycle, cycle_idx)

        # Read one value per sample column
        for i, (csv_col, _, _) in enumerate(sample_cols):
            val_idx = 4 + i
            fval = _safe_float(row[val_idx]) if val_idx < len(row) else float("nan")
            pos = positions[i]

            if detector == _HE4_DETECTOR:
                if data_type == "Y":
                    # Extend list to accommodate this cycle index
                    while len(pos.he4_y) <= cycle_idx:
                        pos.he4_y.append(float("nan"))
                    pos.he4_y[cycle_idx] = fval
                else:  # Time
                    while len(pos.he4_times) <= cycle_idx:
                        pos.he4_times.append(float("nan"))
                    pos.he4_times[cycle_idx] = fval
            else:  # SEM 3He
                if data_type == "Y":
                    while len(pos.he3_y) <= cycle_idx:
                        pos.he3_y.append(float("nan"))
                    pos.he3_y[cycle_idx] = fval
                else:  # Time
                    while len(pos.he3_times) <= cycle_idx:
                        pos.he3_times.append(float("nan"))
                    pos.he3_times[cycle_idx] = fval

    n_cycles = max_cycle + 1 if max_cycle >= 0 else 0

    # Pad all lists to n_cycles for consistency
    for pos in positions:
        for lst in (pos.he4_y, pos.he3_y, pos.he4_times, pos.he3_times):
            while len(lst) < n_cycles:
                lst.append(float("nan"))

    return HelixSFTRun(
        filepath=filepath,
        positions=positions,
        n_cycles=n_cycles,
    )
