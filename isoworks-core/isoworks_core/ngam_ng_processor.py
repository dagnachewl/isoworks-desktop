"""
ngam_ng_processor.py
====================
Match parsed NG sequence data to the database load list.

No database or GUI dependencies.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ngam_ng_parser import NGSequenceParsed, NGInletEntry, NGIsotopeSignals, NGFinalResult

log = logging.getLogger(__name__)


# ─────────────────────────────── input dataclasses ────────────────────────────

@dataclass
class NGPreparationRecord:
    """One row from ngam.ngpreparations."""
    inoblepreparationid: int
    runid: int
    positioninrun: int           # inlet number
    analysisid: Optional[int]
    iportnumber: Optional[int]
    bisblank: bool
    bisreproreference: bool
    bislinreference: bool
    nvcreferences: Optional[str]
    freferenceamount: Optional[float]


# ─────────────────────────────── result dataclasses ───────────────────────────

@dataclass
class NGInletResult:
    """Processed result for one inlet position."""
    inlet_num: int
    port_num: Optional[int]
    name: str
    position_type: str          # blank/standard/air_ref/sample/invalid
    preparation_id: Optional[int]
    analysis_id: Optional[int]
    # Signals per isotope (dict: isotope_name → NGIsotopeSignals)
    isotope_signals: Dict[str, Any]
    # Final result if this inlet has one (matched from NGFinalResult)
    final_result: Optional[Any]  # NGFinalResult


@dataclass
class NGRunResult:
    run_id: Optional[int]
    n_blanks: int
    n_standards: int
    n_air_refs: int
    n_samples: int
    inlet_results: List[NGInletResult]
    unmatched_final_results: List[Any]   # NGFinalResult items not matched to an inlet


# ─────────────────────────────── matching helpers ─────────────────────────────

def _build_isotope_map(
    isotope_signals: Dict[str, List[NGIsotopeSignals]],
) -> Dict[int, Dict[str, NGIsotopeSignals]]:
    """
    Re-index isotope signals as: inlet_num → {isotope_name: NGIsotopeSignals}.
    """
    by_inlet: Dict[int, Dict[str, NGIsotopeSignals]] = {}
    for iso_name, signals in isotope_signals.items():
        for sig in signals:
            by_inlet.setdefault(sig.inlet_num, {})[iso_name] = sig
    return by_inlet


def _srg_matches_inlet(srg: str, inlet: NGInletEntry) -> bool:
    """
    Heuristic: does a Final Result SRG match a sample inlet entry?

    Checks whether the SRG contains the subsample_id or sample_id.
    """
    if not srg:
        return False
    srg_lower = srg.lower()

    if inlet.subsample_id and inlet.subsample_id.lower() in srg_lower:
        return True
    if inlet.sample_id and str(inlet.sample_id) in srg:
        return True
    # Direct name fragment check
    name_clean = re.sub(r'\s+', '', inlet.name.lower())
    srg_clean  = re.sub(r'\s+', '', srg_lower)
    if name_clean and name_clean in srg_clean:
        return True
    return False


def _match_final_results(
    pipeline: List[NGInletEntry],
    final_results: List[NGFinalResult],
) -> Dict[int, NGFinalResult]:
    """
    Match NGFinalResult entries to inlet entries by SRG heuristic.

    Returns dict: inlet_num → NGFinalResult.
    """
    matched: Dict[int, NGFinalResult] = {}
    used: set = set()

    sample_inlets = [e for e in pipeline if e.position_type == "sample"]

    for inlet in sample_inlets:
        for i, fr in enumerate(final_results):
            if i in used:
                continue
            if _srg_matches_inlet(fr.srg, inlet):
                matched[inlet.inlet_num] = fr
                used.add(i)
                break

    # Positional fallback: assign remaining final results to unmatched sample inlets
    unmatched_inlets = [
        e for e in sample_inlets if e.inlet_num not in matched
    ]
    unmatched_frs = [
        (i, fr) for i, fr in enumerate(final_results) if i not in used
    ]

    for inlet, (i, fr) in zip(unmatched_inlets, unmatched_frs):
        matched[inlet.inlet_num] = fr
        used.add(i)

    return matched


# ─────────────────────────────── main function ────────────────────────────────

def process_ng_run(
    parsed: NGSequenceParsed,
    preparations: List[NGPreparationRecord],
    run_id: Optional[int] = None,
) -> NGRunResult:
    """
    Match parsed NG sequence data to the load list preparations.

    Parameters
    ----------
    parsed : NGSequenceParsed
        Output of parse_ng_sequence().
    preparations : list of NGPreparationRecord
        Load list rows from the database.
    run_id : int, optional
        Database run ID.

    Returns
    -------
    NGRunResult
    """
    # ── 1. Index preparations by positioninrun ───────────────────────────────
    prep_by_inlet: Dict[int, NGPreparationRecord] = {
        p.positioninrun: p for p in preparations
    }

    # ── 2. Build per-inlet isotope signal map ─────────────────────────────────
    iso_by_inlet = _build_isotope_map(parsed.isotope_signals)

    # ── 3. Match final results to sample inlets ───────────────────────────────
    final_by_inlet = _match_final_results(parsed.pipeline, parsed.final_results)

    # Track which final results were matched
    matched_frs: set = set()
    for inet, fr in final_by_inlet.items():
        try:
            matched_frs.add(id(fr))
        except Exception as e:

            logging.warning(f"Exception caught: {e}")

    # ── 4. Build NGInletResult list ───────────────────────────────────────────
    inlet_results: List[NGInletResult] = []

    for entry in parsed.pipeline:
        prep = prep_by_inlet.get(entry.inlet_num)

        preparation_id: Optional[int] = None
        analysis_id: Optional[int] = None

        if prep is not None:
            preparation_id = prep.inoblepreparationid
            analysis_id    = prep.analysisid

            # For samples: verify port number matches if available
            if (
                entry.position_type == "sample"
                and entry.port_num is not None
                and prep.iportnumber is not None
                and entry.port_num != prep.iportnumber
            ):
                log.warning(
                    "Inlet %d: parsed port %d != DB port %d — keeping prep match",
                    entry.inlet_num, entry.port_num, prep.iportnumber,
                )

        # Isotope signals for this inlet
        iso_signals = iso_by_inlet.get(entry.inlet_num, {})

        # Final result
        final_result: Optional[NGFinalResult] = None
        if entry.position_type == "sample":
            final_result = final_by_inlet.get(entry.inlet_num)
        elif entry.position_type in ("blank", "standard", "air_ref"):
            # Try matching by name heuristic for non-sample types
            name_lower = entry.name.lower()
            for fr in parsed.final_results:
                if fr.srg.lower() in name_lower or name_lower in fr.srg.lower():
                    final_result = fr
                    break

        inlet_results.append(NGInletResult(
            inlet_num=entry.inlet_num,
            port_num=entry.port_num,
            name=entry.name,
            position_type=entry.position_type,
            preparation_id=preparation_id,
            analysis_id=analysis_id,
            isotope_signals=iso_signals,
            final_result=final_result,
        ))

    # ── 5. Collect unmatched final results ────────────────────────────────────
    matched_fr_ids = {id(r.final_result) for r in inlet_results if r.final_result is not None}
    unmatched = [fr for fr in parsed.final_results if id(fr) not in matched_fr_ids]

    # ── 6. Summary counts ─────────────────────────────────────────────────────
    n_blanks    = sum(1 for r in inlet_results if r.position_type == "blank")
    n_standards = sum(1 for r in inlet_results if r.position_type == "standard")
    n_air_refs  = sum(1 for r in inlet_results if r.position_type == "air_ref")
    n_samples   = sum(1 for r in inlet_results if r.position_type == "sample")

    return NGRunResult(
        run_id=run_id,
        n_blanks=n_blanks,
        n_standards=n_standards,
        n_air_refs=n_air_refs,
        n_samples=n_samples,
        inlet_results=inlet_results,
        unmatched_final_results=unmatched,
    )
