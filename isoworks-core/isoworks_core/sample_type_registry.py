"""
sample_type_registry.py — Single source of truth for public.tblsampletype.

Provides:
  MediaCode        — mediacode integer constants, one per analytical module
  STYPE_*          — named integer constants for every tblsampletype row
  _FALLBACK_COLORS_HEX  — static hex colour map (str key → hex string)
  SampleTypeRegistry    — loads from DB, provides label/short/color/predicate API
  registry         — module-level singleton; call registry.load(conn) once at startup
"""
from __future__ import annotations
try:
    from PyQt5.QtGui import QColor
except ImportError:
    # Server-side/headless fallback to avoid needing heavy GUI libraries
    class QColor:
        def __init__(self, val=None):
            self.val = val
        def name(self) -> str:
            return str(self.val)


# ── Analytical-module mediacode constants ────────────────────────────────────

class MediaCode:
    LSC       = 200   # J-prefix — Tritium / LSC
    IRMS      = 1     # W-prefix — Water isotopes / IRMS
    INGROWTH  = 202   # J-prefix — 3He ingrowth
    NITROGEN  = 58    # N-prefix — 15N / 18O
    NOBLE_GAS = 300   # G-prefix — Noble gas MS


# ── Named sampletype integer constants (mirror tblsampletype rows) ───────────

# Universal sentinel — "this position is a sample slot to be filled"
STYPE_SAMPLE_SLOT   = 0

# LSC / Tritium  (mediacode 200, prefix J)
STYPE_LSC_DW        = 1    # Dead Water            — LSC background
STYPE_LSC_STD       = 2    # Standard              — LSC efficiency standard
STYPE_LSC_SPIKE_ENR = 3    # Spike for Enrichment  — enrichment spike
STYPE_LSC_SPIKE_LSC = 4    # Spike for LSC
STYPE_LSC_AIR       = 5    # Lab Air moisture
STYPE_LSC_DWEN      = 6    # Dead Water for Enrichment
STYPE_LSC_TAPW      = 7    # Tap Water
STYPE_LSC_HDXAS     = 8    # HIDEX Active Standard
STYPE_LSC_HDXBG     = 9    # Hidex Blank Sample
STYPE_LSC_CTRL      = 10   # QC Sample

# IRMS / Water isotopes  (mediacode 1, prefix W)
STYPE_IRMS_SMOW     = 21   # Certified Standard
STYPE_IRMS_SMOW2    = 22   # Certified Standard V2
STYPE_IRMS_IHSTD    = 23   # In-house Standard
STYPE_IRMS_SCTRL    = 24   # SI Control
STYPE_IRMS_DRFTC    = 25   # Drift Control
STYPE_IRMS_AMLIN    = 26   # Amount Linearity Control
STYPE_IRMS_DIWSH    = 27   # DI Wash Sample

# 3He Ingrowth  (mediacode 202, prefix J)
STYPE_ING_BLANK     = 31   # ING Blank
STYPE_ING_AIR       = 32   # ING Air Standard
STYPE_ING_3HE_SPIKE = 33   # 3He Spike

# Nitrogen / dual-isotope  (mediacode 58, prefix N)
STYPE_N_IHSTD       = 41   # 15N_18O Standard
STYPE_N_NCTRL       = 42   # 15N_18O Control

# Noble gas MS  (mediacode 300, prefix G)
STYPE_NG_BLANK      = 51   # NG Blank
STYPE_NG_SPIKE      = 52   # NG Spike
STYPE_NG_AIR        = 53   # NG Air
STYPE_NG_DUMMY      = 54   # Pump-out / dummy run  (migration 012)


# ── Static fallback colour map ───────────────────────────────────────────────
# Used by ui_delegates and visual_tray_widgets before/without a DB load.
# Keys are str(intsampletype); values are hex colour strings.

_FALLBACK_COLORS_HEX: dict[str, str] = {
    "0":  "#E0F2FE",  # sample slot       — light blue
    # LSC
    "1":  "#F5EADD",  "2":  "#D1FAE5",  "3":  "#F5EADD",
    "4":  "#FEE2E2",  "5":  "#F5EADD",  "6":  "#D1FAE5",
    "7":  "#F5EADD",  "8":  "#FEE2E2",  "9":  "#F5EADD",
    "10": "#D1FAE5",
    # IRMS
    "21": "#F5EADD",  "22": "#D1FAE5",  "23": "#F5EADD",
    "24": "#D1FAE5",  "25": "#F5EADD",  "26": "#F5EADD",
    "27": "#FEE2E2",
    # Ingrowth
    "31": "#EDE7F6",  "32": "#E8EAF6",  "33": "#F3E5F5",
    # Nitrogen
    "41": "#E8F5E9",  "42": "#F1F8E9",
    # Noble gas
    "51": "#FFF8E1",  "52": "#FFF3E0",  "53": "#FFF8E1",
    "54": "#B8C0CC",  # DUMMY pump-out   — grey
}

_FALLBACK_COLORS_QCOLOR: dict[str, QColor] = {
    k: QColor(v) for k, v in _FALLBACK_COLORS_HEX.items()
}
_DEFAULT_COLOR_HEX    = "#E0E0E0"
_DEFAULT_COLOR_QCOLOR = QColor(_DEFAULT_COLOR_HEX)


# ── Registry class ───────────────────────────────────────────────────────────

class SampleTypeRegistry:
    """
    Loads public.tblsampletype once and provides a clean API.

    Call ``registry.load(conn)`` once at app startup (or lazily on first use).
    All methods fall back gracefully to _FALLBACK_COLORS_HEX if the DB has not
    been loaded yet.
    """

    def __init__(self) -> None:
        self._rows: dict[int, dict] = {}
        self._loaded: bool = False

    # ── DB load ───────────────────────────────────────────────────────────

    def load(self, conn) -> None:
        """Query public.tblsampletype and cache all rows."""
        from sqlalchemy import text
        rows = conn.execute(text(
            "SELECT strprefix, intsampletype, mediacode, "
            "strshortdescription, strlongdescription "
            "FROM public.tblsampletype"
        )).fetchall()
        self._rows = {
            int(r[1]): {
                "prefix":    r[0] or "",
                "code":      int(r[1]),
                "mediacode": r[2],
                "short":     r[3] or "",
                "label":     r[4] or "",
            }
            for r in rows
        }
        self._loaded = True

    # ── Descriptions ─────────────────────────────────────────────────────

    def label(self, code: int) -> str:
        """Long description (strlongdescription), or str(code) if unknown."""
        row = self._rows.get(code)
        return row["label"] if row else str(code)

    def short(self, code: int) -> str:
        """Short description (strshortdescription), or str(code) if unknown."""
        row = self._rows.get(code)
        return row["short"] if row else str(code)

    # ── Colours ──────────────────────────────────────────────────────────

    def color_hex(self, code: int) -> str:
        """Hex colour string for a type code, with fallback."""
        return _FALLBACK_COLORS_HEX.get(str(code), _DEFAULT_COLOR_HEX)

    def color(self, code: int) -> QColor:
        """QColor for a type code, with fallback."""
        return _FALLBACK_COLORS_QCOLOR.get(str(code), _DEFAULT_COLOR_QCOLOR)

    # ── Predicates ───────────────────────────────────────────────────────

    def is_sample_slot(self, code: int) -> bool:
        """True when code == STYPE_SAMPLE_SLOT (0) — an empty position to fill."""
        return code == STYPE_SAMPLE_SLOT

    def is_fixed_reference(self, code: int) -> bool:
        """True for any non-zero type (blank, standard, spike, dummy…)."""
        return code != STYPE_SAMPLE_SLOT

    def is_dummy(self, code: int) -> bool:
        """True when code == STYPE_NG_DUMMY (54) — pump-out / dummy run."""
        return code == STYPE_NG_DUMMY

    # ── Filtered lists ────────────────────────────────────────────────────

    def types_for_mediacode(self, mc: int) -> list[dict]:
        """All rows with the given mediacode."""
        return [r for r in self._rows.values() if r["mediacode"] == mc]

    def types_for_prefix(self, prefix: str) -> list[dict]:
        """All rows with the given strprefix."""
        return [r for r in self._rows.values() if r["prefix"] == prefix]


# Module-level singleton ─ call registry.load(conn) once at app startup.
registry = SampleTypeRegistry()
