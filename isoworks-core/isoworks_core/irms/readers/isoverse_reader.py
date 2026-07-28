"""
irms/readers/isoverse_reader.py
================================
IRMSReader backed by the isoverse R packages (isoreader 1.4+ / isoprocessor 0.6+).

Handles every binary/archive format that pure-Python readers cannot parse:

  Continuous Flow  :  .dxf  (Isodat, newer)
                      .cf   (Isodat, older)
                      .iarc (Elementar ionOS)
  Dual Inlet       :  .did  (Isodat, newer)
                      .caf  (Isodat, older)
  Scan             :  .scn  (Isodat)

The reader is silently disabled (supports() returns False) when R or the
isoverse packages are not installed — lower-priority Python readers then take
over for any overlapping extensions.

Design
------
  1. IsoversReader.supports() checks R availability once (cached).
  2. read() calls `Rscript irms/r_scripts/read_irms.R <file> <tmpdir>`.
  3. The R script writes file_info.csv, vendor_table.csv, raw_signals.csv.
  4. Python reads those back, cleans up tmpdir, returns IRMSReadResult.

The vendor_table output for Isodat CF files is the isoprocessor standardised
peak table (iso_set_peak_table_from_isodat_vendor_data_table), so column names
match the existing siam_column_resolver.py aliases.  For non-Isodat formats
the raw vendor columns are preserved.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path

import pandas as pd

from ..base_reader import IRMSReader, IRMSReadResult
from ..registry import register

log = logging.getLogger(__name__)

_R_SCRIPT = Path(__file__).parent.parent / "r_scripts" / "read_irms.R"

# Extensions handled here — pure-Python readers keep .csv / .xls / .xlsx
ISOVERSE_SUFFIXES = (".dxf", ".cf", ".did", ".caf", ".iarc", ".scn")


# ── R availability (checked once per process) ─────────────────────────────────

@lru_cache(maxsize=1)
def _r_available() -> bool:
    """Return True if Rscript + isoreader + isoprocessor are on this machine."""
    rscript = shutil.which("Rscript")
    if not rscript:
        log.warning("IsoversReader: Rscript not found on PATH — disabled.")
        return False
    probe = (
        "suppressPackageStartupMessages({"
        "library(isoreader); library(isoprocessor)}); "
        "cat('ok')"
    )
    try:
        result = subprocess.run(
            [rscript, "-e", probe],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0 or "ok" not in result.stdout:
            log.warning(
                "IsoversReader: isoreader/isoprocessor not available in R:\n%s",
                result.stderr[:400],
            )
            return False
        return True
    except Exception as exc:
        log.warning("IsoversReader: R probe failed: %s", exc)
        return False


def r_available() -> bool:
    """Public helper — lets other code check without triggering a read."""
    return _r_available()


def reset_r_cache() -> None:
    """Force re-detection of R availability (useful in tests)."""
    _r_available.cache_clear()


# ── Reader ────────────────────────────────────────────────────────────────────

@register
class IsoversReader(IRMSReader):
    """
    Reads binary/archive IRMS files via the isoverse R packages.

    Returns an IRMSReadResult with:
      file_info    — one row per file: Analysis, Identifier 1, Method, etc.
      vendor_table — peak table (CF: isoprocessor-standardised; DI: per-cycle)
      raw_signals  — continuous mass traces (CF) or raw cycle data (DI)
    """
    suffixes: tuple[str, ...] = ISOVERSE_SUFFIXES

    @classmethod
    def supports(cls, path: str | Path) -> bool:
        return (
            Path(path).suffix.lower() in cls.suffixes
            and _r_available()
        )

    def read(self) -> IRMSReadResult:
        if not _r_available():
            raise RuntimeError(
                "R + isoverse packages not available — cannot read "
                f"binary IRMS file '{self.path.name}'.\n"
                "Install R and run:  install.packages(c('isoreader','isoprocessor'))"
            )

        tmpdir = Path(tempfile.mkdtemp(prefix="isoworks_irms_"))
        try:
            result = subprocess.run(
                ["Rscript", str(_R_SCRIPT), str(self.path), str(tmpdir)],
                capture_output=True,
                text=True,
                timeout=90,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip()
                raise RuntimeError(
                    f"isoreader failed for '{self.path.name}':\n{stderr}"
                )

            log.debug("IsoversReader: %s", result.stdout.strip())

            fi = _load_csv(tmpdir / "file_info.csv")
            return IRMSReadResult(
                file_info    = fi if fi is not None else pd.DataFrame(),
                vendor_table = _load_csv(tmpdir / "vendor_table.csv"),
                raw_signals  = _load_csv(tmpdir / "raw_signals.csv"),
            )

        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_csv(path: Path) -> pd.DataFrame | None:
    """Read a CSV written by the R script; return None if absent or empty."""
    if not path.exists() or path.stat().st_size == 0:
        return None
    try:
        df = pd.read_csv(path, low_memory=False)
        return df if not df.empty else None
    except Exception as exc:
        log.warning("IsoversReader: could not read %s: %s", path.name, exc)
        return None
