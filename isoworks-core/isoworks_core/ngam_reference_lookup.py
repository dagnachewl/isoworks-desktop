"""
ngam_reference_lookup.py
========================
Resolves certified per-isotope reference amounts for NGAM processing.

Priority order:
  0. Gas-type direct  (primary, preferred path)
     nvcreferencegas in ('Spike', 'Air', …) → ngam.ngreference → referencecontroldata
     Requires migration 050 to have run.

  1. Loadlist-based   (legacy fallback when gas_type not set in template)
     ngpreparations + ngseqtemplate ourlabid → referencecontroldata

  2. Description-based (last resort — no sequence in DB required)
     reference_gas string → LIKE match on referencecontrol.description

Keys are bare species labels ("4He", "20Ne", …) matching
ngam.ngreference.nvcspecies / referencecontroldata.parameter.

CV date matching
----------------
When run_date is supplied, the lookup first tries an exact match
(availabledatefrom ≤ run_date ≤ availabledateto).  If no exact match
exists the closest available date range is used and a human-readable
warning is returned so callers can surface it to the user.
"""
from __future__ import annotations

import logging
import re
from typing import Dict, List, Optional, Tuple

from datetime import date
from sqlalchemy import text

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Priority 0 — ngreference → referencecontroldata join (migration 055+)
# ---------------------------------------------------------------------------
# Most-recent valid calibration per species for the given gas type.
# Filters by run_date when supplied; falls back to most recent otherwise.
_SQL_BY_GASTYPE = text("""
    SELECT DISTINCT ON (ng.nvcspecies)
           ng.nvcspecies,
           rcd.certifiedvalue,
           rc.referenceid,
           rc.availabledatefrom,
           rc.availabledateto
      FROM ngam.ngreference         ng
      JOIN public.referencecontroldata rcd
             ON rcd.referencedataid = ng.referencedataid
      JOIN public.referencecontrol  rc
             ON rc.referenceid      = rcd.referenceid
     WHERE ng.nvcgasname  = :gas_name
       AND ng.bisratio    = FALSE
       AND rcd.certifiedvalue IS NOT NULL
       AND (:run_date IS NULL
            OR (rc.availabledatefrom IS NULL OR rc.availabledatefrom <= :run_date)
            AND (rc.availabledateto  IS NULL OR rc.availabledateto  >= :run_date))
     ORDER BY ng.nvcspecies, rc.availabledatefrom DESC NULLS LAST
""")

_SQL_BY_GASTYPE_RATIOS = text("""
    SELECT DISTINCT ON (ng.nvcspecies)
           ng.nvcspecies,
           rcd.certifiedvalue
      FROM ngam.ngreference         ng
      JOIN public.referencecontroldata rcd
             ON rcd.referencedataid = ng.referencedataid
      JOIN public.referencecontrol  rc
             ON rc.referenceid      = rcd.referenceid
     WHERE ng.nvcgasname  = :gas_name
       AND ng.bisratio    = TRUE
       AND rcd.certifiedvalue IS NOT NULL
       AND (:run_date IS NULL
            OR (rc.availabledatefrom IS NULL OR rc.availabledatefrom <= :run_date)
            AND (rc.availabledateto  IS NULL OR rc.availabledateto  >= :run_date))
     ORDER BY ng.nvcspecies, rc.availabledatefrom DESC NULLS LAST
""")

_SQL_BY_GASTYPE_ALL_WITH_UNC = text("""
    SELECT DISTINCT ON (ng.nvcspecies)
           ng.nvcspecies,
           rcd.certifiedvalue,
           rcd.certifiedvalueunc,
           ng.bisratio
      FROM ngam.ngreference         ng
      JOIN public.referencecontroldata rcd
             ON rcd.referencedataid = ng.referencedataid
      JOIN public.referencecontrol  rc
             ON rc.referenceid      = rcd.referenceid
     WHERE ng.nvcgasname  = :gas_name
       AND rcd.certifiedvalue IS NOT NULL
       AND (:run_date IS NULL
            OR (rc.availabledatefrom IS NULL OR rc.availabledatefrom <= :run_date)
            AND (rc.availabledateto  IS NULL OR rc.availabledateto  >= :run_date))
     ORDER BY ng.nvcspecies, rc.availabledatefrom DESC NULLS LAST
""")

# Amounts only (bisratio=FALSE) with uncertainty — for IDMS spike CV lookup.
_SQL_BY_GASTYPE_AMOUNTS_WITH_UNC = text("""
    SELECT DISTINCT ON (ng.nvcspecies)
           ng.nvcspecies,
           rcd.certifiedvalue,
           rcd.certifiedvalueunc
      FROM ngam.ngreference         ng
      JOIN public.referencecontroldata rcd
             ON rcd.referencedataid = ng.referencedataid
      JOIN public.referencecontrol  rc
             ON rc.referenceid      = rcd.referenceid
     WHERE ng.nvcgasname  = :gas_name
       AND ng.bisratio    = FALSE
       AND rcd.certifiedvalue IS NOT NULL
       AND (:run_date IS NULL
            OR (rc.availabledatefrom IS NULL OR rc.availabledatefrom <= :run_date)
            AND (rc.availabledateto  IS NULL OR rc.availabledateto  >= :run_date))
     ORDER BY ng.nvcspecies, rc.availabledatefrom DESC NULLS LAST
""")

_SQL_BY_GASTYPE_AMOUNTS_WITH_UNC_CLOSEST = text("""
    SELECT DISTINCT ON (ng.nvcspecies)
           ng.nvcspecies,
           rcd.certifiedvalue,
           rcd.certifiedvalueunc,
           rc.availabledatefrom,
           rc.availabledateto,
           rc.referenceid
      FROM ngam.ngreference         ng
      JOIN public.referencecontroldata rcd
             ON rcd.referencedataid = ng.referencedataid
      JOIN public.referencecontrol  rc
             ON rc.referenceid      = rcd.referenceid
     WHERE ng.nvcgasname  = :gas_name
       AND ng.bisratio    = FALSE
       AND rcd.certifiedvalue IS NOT NULL
     ORDER BY ng.nvcspecies,
              (CASE WHEN rc.availabledatefrom IS NULL
                         OR rc.availabledatefrom <= CAST(:run_date AS date) THEN 0
                    ELSE EXTRACT(EPOCH FROM (
                            rc.availabledatefrom::timestamp
                            - CAST(:run_date AS timestamp)))
               END +
               CASE WHEN rc.availabledateto IS NULL
                         OR rc.availabledateto >= CAST(:run_date AS date) THEN 0
                    ELSE EXTRACT(EPOCH FROM (
                            CAST(:run_date AS timestamp)
                            - rc.availabledateto::timestamp))
               END) ASC,
              rc.availabledatefrom DESC NULLS LAST
""")

# Closest-date fallback: used when run_date falls outside every stored date range.
# Returns one row per species, ordered by temporal distance to run_date.
# Distance = 0 when run_date is within [from, to]; positive otherwise.
_SQL_BY_GASTYPE_CLOSEST = text("""
    SELECT DISTINCT ON (ng.nvcspecies)
           ng.nvcspecies,
           rcd.certifiedvalue,
           rc.availabledatefrom,
           rc.availabledateto,
           ng.bisratio,
           rc.referenceid
      FROM ngam.ngreference         ng
      JOIN public.referencecontroldata rcd
             ON rcd.referencedataid = ng.referencedataid
      JOIN public.referencecontrol  rc
             ON rc.referenceid      = rcd.referenceid
     WHERE ng.nvcgasname  = :gas_name
       AND rcd.certifiedvalue IS NOT NULL
     ORDER BY ng.nvcspecies,
              (CASE WHEN rc.availabledatefrom IS NULL
                         OR rc.availabledatefrom <= CAST(:run_date AS date) THEN 0
                    ELSE EXTRACT(EPOCH FROM (
                            rc.availabledatefrom::timestamp
                            - CAST(:run_date AS timestamp)))
               END
               +
               CASE WHEN rc.availabledateto IS NULL
                         OR rc.availabledateto >= CAST(:run_date AS date) THEN 0
                    ELSE EXTRACT(EPOCH FROM (
                            CAST(:run_date AS timestamp)
                            - rc.availabledateto::timestamp))
               END) ASC,
              rc.availabledatefrom DESC NULLS LAST
""")

# Known canonical gas-type names in ngreference
_KNOWN_GAS_TYPES: frozenset[str] = frozenset({'Spike', 'Air'})

# Loose normalization: map common protocol gas-name patterns to canonical types.
_GAS_NORM_PATTERNS: list[Tuple[re.Pattern, str]] = [
    (re.compile(r'spike', re.IGNORECASE), 'Spike'),
    (re.compile(r'\bair\b',  re.IGNORECASE), 'Air'),
]


def _normalize_gas_name(gas_name: str) -> Optional[str]:
    """Map a protocol gas name like 'SpikeLarge2' → 'Spike'."""
    for pattern, canonical in _GAS_NORM_PATTERNS:
        if pattern.search(gas_name):
            return canonical
    return None


def load_amounts_by_gastype(
    gas_name: str,
    conn,
    run_date=None,
) -> Tuple[Dict[str, float], Optional[str]]:
    """
    Return (amounts, warning) for canonical gas type 'Spike' or 'Air'.

    amounts : {species: certifiedvalue} — isotope amounts + certified ratios.
    warning : human-readable string when the exact date match failed and the
              closest available date range was used instead; None on exact hit.
    """
    if not gas_name:
        return {}, None
    try:
        params = {"gas_name": gas_name, "run_date": run_date}
        rows = conn.execute(_SQL_BY_GASTYPE, params).fetchall()
        amounts = {row[0]: float(row[1]) for row in rows if row[0]}
        # rows: (species, value, referenceid, datefrom, dateto)
        ref_ids = list({row[2] for row in rows if row[0]})
        ratio_rows = conn.execute(_SQL_BY_GASTYPE_RATIOS, params).fetchall()
        for row in ratio_rows:
            if row[0]:
                amounts[row[0]] = float(row[1])

        if amounts:
            log.debug("CV lookup OK: gas=%r run_date=%s refids=%s n=%d", gas_name, run_date, ref_ids, len(amounts))
            return amounts, None

        log.info("CV lookup miss (exact): gas=%r run_date=%s", gas_name, run_date)

        # No exact match — try closest date range when run_date is known
        if run_date is None:
            log.debug("No ngreference rows for gas_name=%r", gas_name)
            return {}, None

        fallback_rows = conn.execute(
            _SQL_BY_GASTYPE_CLOSEST, {"gas_name": gas_name, "run_date": run_date}
        ).fetchall()
        if not fallback_rows:
            log.warning("No ngreference rows at all for gas_name=%r", gas_name)
            return {}, None

        # Build amounts from fallback rows (bisratio split by column 4)
        fb_amounts: Dict[str, float] = {}
        for row in fallback_rows:
            if row[0]:
                fb_amounts[row[0]] = float(row[1])

        # Format warning using date range from first row
        row0 = fallback_rows[0]
        dt_from  = str(row0[2])[:10] if row0[2] is not None else "—"
        dt_to    = str(row0[3])[:10] if row0[3] is not None else "—"
        ref_id   = row0[5]
        rd_str   = str(run_date)[:10]
        warning = (
            f"No certified values for '{gas_name}' valid on {rd_str}. "
            f"Using closest available (referenceid={ref_id}) [{dt_from} – {dt_to}]."
        )
        log.warning(warning)
        return fb_amounts, warning

    except Exception:
        log.exception("load_amounts_by_gastype failed for %r", gas_name)
        return {}, None


def load_amounts_with_unc_by_gastype(
    gas_name: str,
    conn,
    run_date=None,
) -> Tuple[Dict[str, Tuple[float, float]], Optional[str]]:
    """
    Return ({species: (value, unc)}, warning) for non-ratio amounts of gas_name.

    Exact date match first; falls back to closest date range with a warning string.
    Used by build_spike_certified_values for IDMS where uncertainty is needed.
    """
    if not gas_name:
        return {}, None
    try:
        params = {"gas_name": gas_name, "run_date": run_date}
        rows = conn.execute(_SQL_BY_GASTYPE_AMOUNTS_WITH_UNC, params).fetchall()
        amounts = {row[0]: (float(row[1]), float(row[2] or 0.0)) for row in rows if row[0]}
        if amounts:
            return amounts, None

        if run_date is None:
            return {}, None

        fb_rows = conn.execute(
            _SQL_BY_GASTYPE_AMOUNTS_WITH_UNC_CLOSEST,
            {"gas_name": gas_name, "run_date": run_date},
        ).fetchall()
        if not fb_rows:
            return {}, None

        fb_amounts = {row[0]: (float(row[1]), float(row[2] or 0.0)) for row in fb_rows if row[0]}
        row0 = fb_rows[0]
        dt_from = str(row0[3])[:10] if row0[3] is not None else "—"
        dt_to   = str(row0[4])[:10] if row0[4] is not None else "—"
        ref_id  = row0[5]
        warning = (
            f"No certified values for '{gas_name}' valid on {str(run_date)[:10]}. "
            f"Using closest available (referenceid={ref_id}) [{dt_from} – {dt_to}]."
        )
        log.warning(warning)
        return fb_amounts, warning
    except Exception:
        log.exception("load_amounts_with_unc_by_gastype failed for %r", gas_name)
        return {}, None


def load_certified_values_for_display(
    conn,
    run_date=None,
) -> Dict[str, Dict[str, Dict]]:
    """
    Return certified values + uncertainties for Spike and Air gas types.

    Shape: {
        "Spike": { species: {"value": float, "unc": float|None, "is_ratio": bool} },
        "Air":   { species: {"value": float, "unc": float|None, "is_ratio": bool} },
    }
    Used by the Certified Reference Values modal in the frontend.
    """
    result: Dict[str, Dict[str, Dict]] = {}
    for gas_name in ("Spike", "Air"):
        try:
            rows = conn.execute(
                _SQL_BY_GASTYPE_ALL_WITH_UNC,
                {"gas_name": gas_name, "run_date": run_date},
            ).fetchall()
            result[gas_name] = {
                row[0]: {
                    "value": float(row[1]),
                    "unc": float(row[2]) if row[2] is not None else None,
                    "is_ratio": bool(row[3]),
                }
                for row in rows
                if row[0]
            }
        except Exception:
            log.exception("load_certified_values_for_display failed for %r", gas_name)
            result[gas_name] = {}
    return result


# ---------------------------------------------------------------------------
# Priority 1 — loadlist-based (legacy, ourlabid → referencecontroldata)
# ---------------------------------------------------------------------------

_SQL_BY_LABID = text("""
    SELECT rcd.parameter,
           rcd.certifiedvalue
      FROM public.referencecontrol  rc
      JOIN public.referencecontroldata rcd
        ON rcd.referenceid  = rc.referenceid
     WHERE rc.prefix         = :pfx
       AND rc.sampleid       = :sid
       AND rcd.parameter     IS NOT NULL
       AND rcd.certifiedvalue > 0
""")

# Returns both ourlabid and nvcreferencegas per inlet position.
_SQL_TEMPLATE_MAP = text("""
    SELECT p.positioninrun,
           t.ourlabid,
           t.nvcreferencegas
      FROM ngam.ngpreparations   p
      JOIN ngam.ngsequencerun    r  ON r.runid = p.runid
      LEFT JOIN public.ngseqtemplate t
        ON t.procedureid = r.procedureid
       AND t.iinletid    = p.positioninrun
     WHERE p.runid = :run_id
       AND (t.ourlabid IS NOT NULL OR t.nvcreferencegas IS NOT NULL)
""")


def _parse_labid(labid: str) -> Optional[Tuple[str, int]]:
    labid = labid.strip()
    if "-" not in labid:
        return None
    pfx, sid_str = labid.split("-", 1)
    try:
        return pfx, int(sid_str)
    except ValueError:
        return None


def _query_amounts_by_labid(conn, pfx: str, sid: int) -> Dict[str, float]:
    rows = conn.execute(_SQL_BY_LABID, {"pfx": pfx, "sid": sid}).fetchall()
    return {row[0]: float(row[1]) for row in rows if row[0]}


# ---------------------------------------------------------------------------
# Priority 2 — description-based fallback
# ---------------------------------------------------------------------------

_SQL_BY_DESC = text("""
    SELECT rcd.parameter,
           rcd.certifiedvalue
      FROM public.referencecontrol  rc
      JOIN public.referencecontroldata rcd
        ON rcd.referenceid  = rc.referenceid
     WHERE REPLACE(LOWER(rc.description), '_', '')
               LIKE '%' || LOWER(:gas_name) || '%'
       AND rcd.parameter     IS NOT NULL
       AND rcd.certifiedvalue > 0
""")


def load_amounts_by_gas_name(gas_name: str, conn) -> Dict[str, float]:
    """Description-based match — kept as last-resort fallback."""
    if not gas_name:
        return {}
    try:
        rows = conn.execute(_SQL_BY_DESC, {"gas_name": gas_name}).fetchall()
        amounts = {row[0]: float(row[1]) for row in rows if row[0]}
        if amounts:
            log.debug("Description match for %r: %d species", gas_name, len(amounts))
        return amounts
    except Exception:
        log.exception("load_amounts_by_gas_name failed for %r", gas_name)
        return {}


def load_amounts_by_labid(labid: str, conn) -> Dict[str, float]:
    parsed = _parse_labid(labid)
    if parsed is None:
        log.debug("Malformed labid: %r", labid)
        return {}
    pfx, sid = parsed
    try:
        amounts = _query_amounts_by_labid(conn, pfx, sid)
        if amounts:
            log.debug("Labid %r: %d certified values loaded", labid, len(amounts))
        return amounts
    except Exception:
        log.exception("load_amounts_by_labid failed for %r", labid)
        return {}


# ---------------------------------------------------------------------------
# Template map (replaces old load_labid_map)
# ---------------------------------------------------------------------------

def load_template_map(run_id: int, conn) -> Dict[int, dict]:
    """
    Return {positioninrun: {'ourlabid': ..., 'gas_type': ...}} for all
    inlets in *run_id* that have template data (either ourlabid or gas_type).
    """
    try:
        rows = conn.execute(_SQL_TEMPLATE_MAP, {"run_id": run_id}).fetchall()
        return {
            int(row[0]): {"ourlabid": row[1] or "", "gas_type": row[2] or ""}
            for row in rows
        }
    except Exception:
        log.exception("load_template_map failed for run_id=%s", run_id)
        return {}


# Backward-compat alias (some callers may import the old name)
def load_labid_map(run_id: int, conn) -> Dict[int, str]:
    tmap = load_template_map(run_id, conn)
    return {pos: info["ourlabid"] for pos, info in tmap.items() if info["ourlabid"]}


# ---------------------------------------------------------------------------
# Top-level enrichment
# ---------------------------------------------------------------------------

def enrich_sequence_with_reference_amounts(
    seq,
    conn,
    run_id: Optional[int] = None,
) -> List[str]:
    """
    Populate InletPrep.per_isotope_amounts for every reference inlet in *seq*.

    Priority:
      0. gas_type from template (canonical 'Spike'/'Air') → ngreference → referencecontroldata
      1. ourlabid from template → referencecontroldata
      2. reference_gas from protocol → description match → referencecontroldata
         (also tries normalizing protocol gas name to canonical type first)

    Mutates seq.inlets in-place; never overwrites already-populated
    per_isotope_amounts.

    Returns a list of human-readable warning strings for cases where CV
    lookup fell back to the closest available date range rather than an
    exact match.  Empty list means all CVs matched exactly (or no
    run_date was available for date-based filtering).
    """
    template_map: Dict[int, dict] = {}
    if run_id is not None:
        template_map = load_template_map(run_id, conn)

    # Cache keyed by gas_name → (amounts, warning) to avoid repeated DB hits
    # and to deduplicate warnings for the same gas across multiple inlets.
    gastype_cache: Dict[str, Tuple[Dict[str, float], Optional[str]]] = {}
    desc_cache: Dict[str, Dict[str, float]] = {}
    warnings: List[str] = []

    # Derive measurement date from the sequence's LVT start timestamp.
    run_date: Optional[date] = None
    lv_start = getattr(seq, "lv_time_start", None)
    if lv_start:
        try:
            from ngam_protocol_parser import lv_to_datetime as _lv_to_dt
            run_date = _lv_to_dt(float(lv_start)).date()
        except Exception:
            log.exception("enrich_sequence: failed to convert lv_start=%s to date", lv_start)

    for prep in seq.inlets:
        if not prep.is_reference:
            continue
        if prep.per_isotope_amounts:
            continue

        amounts: Dict[str, float] = {}
        tinfo = template_map.get(prep.seq_num, {})

        # ── Priority 0: canonical gas type from template ──────────────────
        gas_type = tinfo.get("gas_type", "")
        if not gas_type:
            rgas = getattr(prep, "reference_gas", "") or ""
            if rgas in _KNOWN_GAS_TYPES:
                gas_type = rgas
        if gas_type:
            if gas_type not in gastype_cache:
                gastype_cache[gas_type] = load_amounts_by_gastype(gas_type, conn, run_date)
            amounts, warn = gastype_cache[gas_type]
            if warn and warn not in warnings:
                warnings.append(warn)
            if amounts:
                log.debug(
                    "Inlet %d: %d amounts from gas_type %r (ngreference)",
                    prep.seq_num, len(amounts), gas_type,
                )

        # ── Priority 1: ourlabid → referencecontroldata ───────────────────
        if not amounts:
            labid = tinfo.get("ourlabid", "")
            if labid:
                amounts = load_amounts_by_labid(labid, conn)
                if amounts:
                    log.debug(
                        "Inlet %d: %d amounts from labid %r",
                        prep.seq_num, len(amounts), labid,
                    )

        # ── Priority 2: description fallback (and gas-name normalization) ──
        if not amounts and getattr(prep, "reference_gas", None):
            gas = prep.reference_gas

            # Try normalizing protocol gas name → canonical type first
            canonical = _normalize_gas_name(gas)
            if canonical and canonical not in (gas_type, ""):
                if canonical not in gastype_cache:
                    gastype_cache[canonical] = load_amounts_by_gastype(canonical, conn, run_date)
                amounts, warn = gastype_cache[canonical]
                if warn and warn not in warnings:
                    warnings.append(warn)
                if amounts:
                    log.debug(
                        "Inlet %d: %d amounts from normalized gas_type %r (via %r)",
                        prep.seq_num, len(amounts), canonical, gas,
                    )

            # Full description match as last resort
            if not amounts:
                if gas not in desc_cache:
                    desc_cache[gas] = load_amounts_by_gas_name(gas, conn)
                amounts = desc_cache[gas]
                if amounts:
                    log.debug(
                        "Inlet %d: %d amounts from description match on %r",
                        prep.seq_num, len(amounts), gas,
                    )

        if not amounts:
            log.warning(
                "Inlet %d (%r): no certified reference amounts found",
                prep.seq_num, getattr(prep, "reference_gas", "?"),
            )

        if amounts:
            ref_vol = getattr(prep, "reference_amount", 0.0) or 0.0
            if ref_vol > 0:
                prep.per_isotope_amounts = {k: ref_vol * v for k, v in amounts.items()}
            else:
                prep.per_isotope_amounts = dict(amounts)

    return warnings
