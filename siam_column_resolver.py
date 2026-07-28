"""
siam_column_resolver.py
=======================
Unified column mapping and resolution for SIAM instruments.

Replaces the scattered alias methods in siam_processor_gui.py:
  - _alias_water_columns()
  - _alias_cn_columns()
  - _ensure_sample_id()
  - _ensure_timestamp_from_date_time()

Design
------
1. SIAM_TARGET_FIELDS: canonical field names by instrument type
2. COLUMN_ALIASES: all known vendor/alternate names for each canonical field
3. resolve_columns(): single function that applies protocol mappings + alias fallback
4. Protocol-aware: respects user-defined ColumnMapping overrides

Usage
-----
    from siam_column_resolver import resolve_columns, InstrumentType

    # Auto-detect (no protocol mappings)
    df_clean = resolve_columns(
        df,
        instrument=InstrumentType.PICARRO,
    )

    # With protocol override
    df_clean = resolve_columns(
        df,
        instrument=InstrumentType.LGR,
        protocol_mappings=[
            ColumnMapping("d18O", "Delta_18_16"),
            ColumnMapping("sample_id", "Vial"),
        ],
    )
"""

from __future__ import annotations

import re
import logging
from typing import Optional
from enum import Enum
from dataclasses import dataclass

import pandas as pd
import numpy as np


# ──────────────────────────────────────────────────────────────────────────────
# INSTRUMENT TYPES
# ──────────────────────────────────────────────────────────────────────────────

class InstrumentType(Enum):
    """Supported SIAM instrument types."""
    LGR      = "LGR"
    PICARRO  = "Picarro"
    IRMS_EA  = "IRMS (EA)"
    IRMS_DI  = "IRMS (Thermo DI)"
    LASER    = "Laser"  # generic catch-all for LGR/Picarro

    @classmethod
    def from_label(cls, label: str) -> Optional["InstrumentType"]:
        """Parse instrument type from GUI combo box label."""
        label_norm = (label or "").strip()
        if label_norm in ("LGR", "LGR-LWIA"):
            return cls.LGR
        if label_norm in ("Picarro", "Picarro L2130-i", "Picarro L2140-i"):
            return cls.PICARRO
        if "IRMS" in label_norm and "EA" in label_norm:
            return cls.IRMS_EA
        if ("IRMS" in label_norm and "DI" in label_norm) or "Thermo DI" in label_norm:
            return cls.IRMS_DI
        if "Laser" in label_norm:
            return cls.LASER
        return None


# ──────────────────────────────────────────────────────────────────────────────
# TARGET FIELDS BY INSTRUMENT
# ──────────────────────────────────────────────────────────────────────────────

SIAM_TARGET_FIELDS = {
    # ── Laser (LGR / Picarro) ────────────────────────────────────────────────
    InstrumentType.LGR: [
        "sample_id",      # sample identifier
        "timestamp",      # datetime of measurement
        "injection_no",   # injection sequence number
        "water_conc",     # water concentration (ppm)
        "d18O",           # δ18O value
        "dD",             # δD value
        "d17O",           # δ17O value (if equipped)
        "block_no",       # block/analysis grouping (optional)
        "Analysis",       # LIMS analysis ID (post-enrichment)
    ],
    InstrumentType.PICARRO: [
        "sample_id",
        "timestamp",
        "water_conc",
        "d18O",
        "dD",
        "d17O",
        "block_no",
        "Analysis",
    ],
    InstrumentType.LASER: [  # generic laser fallback
        "sample_id",
        "timestamp",
        "water_conc",
        "d18O",
        "dD",
        "d17O",
        "block_no",
        "Analysis",
    ],
    
    # ── IRMS EA (C/N isotopes) ───────────────────────────────────────────────
    InstrumentType.IRMS_EA: [
        "sample_id",
        "timestamp",
        "d13C",           # δ13C value
        "d15N",           # δ15N value
        "Area",           # peak area (mass 44/28)
        "Analysis",
    ],
    
    # ── IRMS DI (water isotopes) ─────────────────────────────────────────────
    InstrumentType.IRMS_DI: [
        "sample_id",
        "timestamp",
        "d18O",
        "dD",
        "d17O",
        "Analysis",
    ],
}


# ──────────────────────────────────────────────────────────────────────────────
# COLUMN ALIASES (vendor-specific names → canonical target)
# ──────────────────────────────────────────────────────────────────────────────

COLUMN_ALIASES = {
    # ── sample_id ────────────────────────────────────────────────────────────
    "sample_id": [
        "sample_id", "sampleid", "sample id",
        "identifier 1", "identifier1", "identifier", "identifier_1",
        "sample name", "samplename",
        "name",
        "id",
        "ref name", "refname", "ref_name",
        "vial",
        "ourlabid", "our lab id",
    ],
    
    # ── timestamp ────────────────────────────────────────────────────────────
    # Note: timestamp is often built from separate Date + Time columns
    "timestamp": [
        "timestamp", "time stamp",
        "datetime", "date time",
        "acquisition datetime", "acq datetime",
        "date_time",
    ],
    
    # ── injection_no ─────────────────────────────────────────────────────────
    "injection_no": [
        "injection_no", "injectionno", "injection no",
        "injection", "inj", "inj no",
        "peak", "peak nr", "peak no", "peakno",
    ],
    
    # ── water_conc ───────────────────────────────────────────────────────────
    "water_conc": [
        "water_conc", "waterconc", "water conc",
        "h2o", "h2o conc", "h2o_conc",
        "concentration", "conc",
        "amount",
    ],
    
    # ── block_no ─────────────────────────────────────────────────────────────
    "block_no": [
        "block_no", "blockno", "block no",
        "block",
        "run",
    ],
    
    # ── Analysis (LIMS ID) ───────────────────────────────────────────────────
    "Analysis": [
        "analysis", "analysisid", "analysis id",
        "lims id", "limsid",
    ],
    
    # ── d18O ─────────────────────────────────────────────────────────────────
    "d18O": [
        "d18o", "d 18o",
        "delta18o", "delta 18o", "delta_18o",
        "delta18", "delta 18",
        "delta18_16", "delta 18_16", "delta18/16",
        "d18o16", "d 18o16", "d18o/16o",
        "delta18o16", "delta 18o16", "delta18o/16o",
        "δ18o", "δ 18o", "δ18o16", "δ18o/16o",
    ],
    
    # ── dD ───────────────────────────────────────────────────────────────────
    "dD": [
        "dd", "d d",
        "delta2h", "delta 2h", "delta_2h",
        "delta2h1h", "delta 2h1h", "delta2h/1h",
        "delta_d_h", "delta d h",
        "d2h", "d 2h", "d2h/1h",
        "δ2h", "δ 2h", "δ2h/1h",
        "d_d",
    ],
    
    # ── d17O ─────────────────────────────────────────────────────────────────
    "d17O": [
        "d17o", "d 17o",
        "delta17o", "delta 17o", "delta_17o",
        "delta17_16", "delta 17_16", "delta17/16",
        "d17o16", "d 17o16", "d17o/16o",
        "δ17o", "δ 17o", "δ17o16", "δ17o/16o",
    ],
    
    # ── d13C ─────────────────────────────────────────────────────────────────
    "d13C": [
        "d13c", "d 13c",
        "delta13c", "delta 13c", "delta_13c",
        "d13c12c", "d 13c12c", "d13c/12c",
        "δ13c", "δ 13c", "δ13c/12c",
    ],
    
    # ── d15N ─────────────────────────────────────────────────────────────────
    "d15N": [
        "d15n", "d 15n",
        "delta15n", "delta 15n", "delta_15n",
        "d15n14n", "d 15n14n", "d15n/14n",
        "δ15n", "δ 15n", "δ15n/14n",
    ],
    
    # ── Area (EA peak area) ──────────────────────────────────────────────────
    "Area": [
        "area", "peak area",
        "area 44", "area44", "area_44",  # mass 44 (CO2)
        "area 28", "area28", "area_28",  # mass 28 (N2)
    ],
}


# ──────────────────────────────────────────────────────────────────────────────
# SPECIAL-CASE COLUMNS: timestamp from Date + Time
# ──────────────────────────────────────────────────────────────────────────────

DATE_ALIASES = [
    "date",
    "acquisition date", "acq. date", "acq date", "acq_date",
    "run date",
]

TIME_ALIASES = [
    "time",
    "acquisition time", "acq. time", "acq time", "acq_time",
    "run time",
]


# ──────────────────────────────────────────────────────────────────────────────
# PROTOCOL OVERRIDE (for custom user mappings)
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ColumnMapping:
    """User-defined column mapping (from protocol)."""
    target_field: str
    source_header: str
    is_net: bool = False
    requires_background: bool = False
    uncertainty_column: Optional[str] = None
    transform_formula: Optional[str] = None
    display_order: int = 0


# ──────────────────────────────────────────────────────────────────────────────
# MAIN RESOLVER
# ──────────────────────────────────────────────────────────────────────────────

def resolve_columns(
    df: pd.DataFrame,
    instrument: InstrumentType | str,
    protocol_mappings: list[ColumnMapping] | None = None,
) -> pd.DataFrame:
    """
    Resolve vendor-specific column names to SIAM canonical names.
    
    Order of precedence:
      1. Protocol mappings (explicit user override)
      2. Alias lookup (automatic detection from COLUMN_ALIASES)
      3. Special-case builders (timestamp from Date + Time)
    
    Parameters
    ----------
    df                : raw DataFrame from instrument file
    instrument        : InstrumentType enum or string label
    protocol_mappings : list of ColumnMapping (from active protocol)
    
    Returns
    -------
    DataFrame with canonical column names (d18O, dD, sample_id, timestamp, ...)
    """
    if not isinstance(df, pd.DataFrame) or df.empty:
        return df
    
    # Parse instrument type
    if isinstance(instrument, str):
        inst = InstrumentType.from_label(instrument)
        if inst is None:
            logging.warning(f"Unknown instrument label '{instrument}', using LASER fallback")
            inst = InstrumentType.LASER
    else:
        inst = instrument
    
    out = df.copy()
    
    # ── 1) Apply protocol mappings (user overrides) ──────────────────────────
    if protocol_mappings:
        for mapping in protocol_mappings:
            src = mapping.source_header
            tgt = mapping.target_field
            if src in out.columns:
                out[tgt] = out[src]
                logging.info(f"Protocol mapping: {src} → {tgt}")
    
    # ── 2) Alias lookup for all target fields ────────────────────────────────
    target_fields = SIAM_TARGET_FIELDS.get(inst, SIAM_TARGET_FIELDS[InstrumentType.LASER])
    
    for target in target_fields:
        if target in out.columns:
            continue  # already present (from protocol or previous step)
        
        aliases = COLUMN_ALIASES.get(target, [])
        matched = _find_column(out.columns, aliases)
        if matched:
            out[target] = out[matched]
            logging.debug(f"Alias resolved: {matched} → {target}")
    
    # ── 3) Special case: timestamp from Date + Time OR datetime-in-Time ──────
    if "timestamp" not in out.columns:
        # Try Date + Time merge first
        date_col = _find_column(out.columns, DATE_ALIASES)
        time_col = _find_column(out.columns, TIME_ALIASES)
        
        if date_col and time_col:
            try:
                out["timestamp"] = pd.to_datetime(
                    out[date_col].astype(str).str.strip() + " " +
                    out[time_col].astype(str).str.strip(),
                    errors="coerce"
                )
                logging.debug(f"Built timestamp from {date_col} + {time_col}")
            except Exception as e:
                logging.warning(f"Failed to build timestamp: {e}")
        
        # Fallback: check if Time-like column contains full datetimes
        elif time_col:
            try:
                parsed = pd.to_datetime(out[time_col], errors="coerce")
                if parsed.notna().sum() > 0:
                    out["timestamp"] = parsed
                    logging.debug(f"Parsed timestamp from {time_col} (contains datetimes)")
            except Exception as e:

                logging.warning(f"Exception caught: {e}")
    
    # ── 4) Clean sample_id (strip, split by '/', remove floaty .0) ───────────
    if "sample_id" in out.columns:
        out["sample_id"] = (
            out["sample_id"]
            .astype(str)
            .str.split("/", n=1, expand=False).str[0]
            .str.strip()
            .str.replace(r"\s+", " ", regex=True)
            .apply(_defloat)
        )
    elif "Line" in out.columns:
        # Final fallback: synthesize from line number
        out["sample_id"] = (
            "L" + pd.to_numeric(out["Line"], errors="coerce")
            .fillna(-1).astype(int).astype(str)
        )
        logging.debug("Synthesized sample_id from Line column")
    else:
        out["sample_id"] = out.index.astype(str)
        logging.debug("Synthesized sample_id from row index")
    
    # ── 5) Type coercion for numeric/datetime columns ────────────────────────
    _coerce_types(out, inst)
    
    return out


# ──────────────────────────────────────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _find_column(columns: list[str], aliases: list[str]) -> Optional[str]:
    """
    Find the first column that matches any alias (case/space/underscore-insensitive).
    Returns the actual column name from `columns`, or None.
    """
    norm = lambda s: re.sub(r"[\s_]+", "", str(s or "")).lower()
    alias_set = {norm(a) for a in aliases}
    for col in columns:
        if norm(col) in alias_set:
            return col
    return None


def _defloat(s: str) -> str:
    """Convert '123.0' → '123', leave everything else untouched."""
    s = str(s or "").strip()
    if not s or s in ("nan", "None", "NaT", ""):
        return ""
    if re.fullmatch(r"\d+\.0+", s):
        return str(int(float(s)))
    return s


def _coerce_types(df: pd.DataFrame, inst: InstrumentType) -> None:
    """
    Apply type coercion to canonical columns (in-place).
    
    - Numeric: d18O, dD, d17O, d13C, d15N, water_conc, injection_no, block_no
    - Datetime: timestamp
    - String: sample_id
    """
    # Isotope delta values
    for iso in ("d18O", "dD", "d17O", "d13C", "d15N"):
        if iso in df.columns:
            df[iso] = pd.to_numeric(df[iso], errors="coerce")

    # Other numeric fields
    for col in ("water_conc", "injection_no", "block_no", "Area"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    
    # Integer fields (block_no, injection_no, Analysis)
    for col in ("block_no", "injection_no"):
        if col in df.columns:
            df[col] = df[col].fillna(0).astype("Int64")
    
    # Datetime
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
    
    # String (sample_id is already cleaned in resolve_columns)
    # Analysis can be string or int depending on LIMS
    if "Analysis" in df.columns and df["Analysis"].dtype == object:
        # Try to coerce to int, keep as string if fails
        try:
            df["Analysis"] = pd.to_numeric(df["Analysis"], errors="coerce").fillna(-1).astype(int)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")


# ──────────────────────────────────────────────────────────────────────────────
# CONVENIENCE: get target fields for an instrument (for GUI display)
# ──────────────────────────────────────────────────────────────────────────────

def get_target_fields(instrument: InstrumentType | str) -> list[str]:
    """
    Return the list of canonical target fields for the given instrument.
    Used by protocol GUI to populate the target field dropdown.
    """
    if isinstance(instrument, str):
        inst = InstrumentType.from_label(instrument)
        if inst is None:
            inst = InstrumentType.LASER
    else:
        inst = instrument
    return SIAM_TARGET_FIELDS.get(inst, [])


def get_aliases_for_field(target_field: str) -> list[str]:
    """
    Return all known aliases for a canonical target field.
    Used for documentation / auto-complete hints in protocol GUI.
    """
    return COLUMN_ALIASES.get(target_field, [])
