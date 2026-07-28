"""
ngam_processing_bridge.py
==========================
Adapter that converts a Helix SFT parsed run (ngam_ms_parser.HelixSFTRun)
into a ProtocolSequence so it can be processed by the unified
ngam_protocol_processor.process_sequence() pipeline.

Usage
-----
    from ngam_ms_parser import parse_helix_sft
    from ngam_ms_processor import process_run, LoadListRecord
    from ngam_processing_bridge import helix_to_protocol_sequence
    from ngam_protocol_processor import process_sequence, ProcessingConfig

    helix_run = parse_helix_sft(path)
    seq       = helix_to_protocol_sequence(helix_run, load_list, run_id=42)
    result    = process_sequence(seq, sequence_id=42)
"""
from __future__ import annotations

import math
from typing import List, Optional

from ngam_ms_parser import HelixSFTRun, PositionData
from ngam_ms_processor import LoadListRecord
from ngam_protocol_parser import (
    MSSignalRow, MSMeasurementData, InletPrep, ProtocolSequence,
)


def helix_to_protocol_sequence(
    helix_run: HelixSFTRun,
    load_list: List[LoadListRecord],
    run_description: str = "",
    run_id: Optional[int] = None,
) -> ProtocolSequence:
    """
    Convert a HelixSFTRun to a ProtocolSequence.

    Each position becomes an InletPrep with one MSMeasurementData (device
    "SMS") whose signals are the 3He and 4He cycles.  The protocol processor
    can then apply linear regression, blank correction and sensitivity
    calibration exactly as it does for NobleControl data.

    Classification rules (matching ngam_ms_processor._classify_position):
      - loadrec.bisblank                              → "blank"
      - loadrec.bisreproreference/bislinreference     → "standard" (is_reference = True)
      - else                                          → "sample"

    Parameters
    ----------
    helix_run : HelixSFTRun
        Output of parse_helix_sft().
    load_list : list of LoadListRecord
        One record per position; used for blank/standard/sample classification
        and reference amounts.
    run_description : str
        Optional label stored in ProtocolSequence.description.
    run_id : int, optional
        Stored in the ProtocolSequence for tracing; not used in processing.
    """
    ll_by_pos = {rec.positioninrun: rec for rec in load_list}

    inlets: List[InletPrep] = []

    for pos in helix_run.positions:
        if not pos.is_valid:
            continue

        loadrec = ll_by_pos.get(pos.position_num)

        # ── classification ──────────────────────────────────────────
        is_reference = False
        per_iso_amounts: dict = {}
        label = pos.description

        if loadrec:
            if loadrec.bisblank:
                if "blank" not in label.lower():
                    label = f"[Blank] {label}"
            elif loadrec.bisreproreference or loadrec.bislinreference:
                is_reference = True
                # Calibration amount: knownstdactivity (TU) if set, else sampleamount (ccSTP)
                cal = (
                    loadrec.knownstdactivity
                    if (loadrec.knownstdactivity and loadrec.knownstdactivity > 0)
                    else loadrec.sampleamount
                )
                if cal:
                    # Store only for 3He — prevents 4He from being calibrated against
                    # a 3H/3He calibration value (different isotope, meaningless sensitivity).
                    per_iso_amounts = {"SMS:3He": cal}

        # ── build SMS signals from per-cycle arrays ──────────────────
        # Interleave 4He then 3He for each cycle so action blocks are
        # contiguous within the single MSMeasurementData.
        he4_signals: List[MSSignalRow] = []
        he3_signals: List[MSSignalRow] = []

        for i in range(helix_run.n_cycles):
            t4 = pos.he4_times[i] if i < len(pos.he4_times) else float("nan")
            v4 = pos.he4_y[i]     if i < len(pos.he4_y)     else float("nan")
            t3 = pos.he3_times[i] if i < len(pos.he3_times) else float("nan")
            v3 = pos.he3_y[i]     if i < len(pos.he3_y)     else float("nan")

            # Fall back to cycle index if timestamp is missing
            if math.isnan(t4):
                t4 = float(i)
            if math.isnan(t3):
                t3 = float(i)

            if not math.isnan(v4):
                he4_signals.append(
                    MSSignalRow(lv_time=t4, action="4He",
                                signal=v4, on_faraday=True)
                )
            if not math.isnan(v3):
                he3_signals.append(
                    MSSignalRow(lv_time=t3, action="3He",
                                signal=v3, on_faraday=False)
                )

        # Keep 4He block before 3He block (matches measurement order)
        all_signals = he4_signals + he3_signals

        ms = MSMeasurementData(
            device="SMS",
            original_path="(Helix SFT)",
            resolved_path="(Helix SFT XLSM)",   # non-None → data is present
            inlet_num=pos.position_num,
            nuclides=["4He", "3He"],
            signals=all_signals,
        )

        # ── time bounds ───────────────────────────────────────────────
        valid_t4 = [t for t in pos.he4_times if not math.isnan(t)]
        t_start = valid_t4[0]  if valid_t4 else 0.0
        t_end   = valid_t4[-1] if valid_t4 else float(helix_run.n_cycles)

        inlet = InletPrep(
            seq_num=pos.position_num,
            lv_time_start=t_start,
            lv_time_end=t_end,
            inlet_string=label,
            is_reference=is_reference,
            reference_gas="3He" if is_reference else "",
            reference_amount=0.0,          # scalar unused; per_isotope_amounts carries it
            per_isotope_amounts=per_iso_amounts,
            ms_data=[ms],
        )
        inlets.append(inlet)

    t0 = inlets[0].lv_time_start  if inlets else 0.0
    te = inlets[-1].lv_time_end   if inlets else None

    desc = run_description or f"Helix SFT run #{run_id}" if run_id else "Helix SFT run"

    return ProtocolSequence(
        protocol_path=f"helix_sft:{run_id or 0}",
        inlet_state_path=None,
        lv_time_start=t0,
        lv_time_end=te,
        description=desc,
        inlets=inlets,
        inlet_state=None,
    )
