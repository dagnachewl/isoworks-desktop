from __future__ import annotations

import os
import logging
from typing import Callable, Optional
from PyQt5.QtCore import QObject, pyqtSignal

from ngam_processor.models.ngam_model import NGAMModel
from ngam_processor.database import ngam_repository

from ngam_protocol_parser import parse_protocol, merge_sequences
from ngam_protocol_processor import process_sequence, capture_unc_trace
from ngam_reference_lookup import enrich_sequence_with_reference_amounts
from shared_utils import get_current_user_id, normalize_login_name
from db_core import db_manager

_UNC_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "logs", "ngam_unc")

log = logging.getLogger(__name__)


def _merge_seq_dict(db_base: Optional[dict], session_overlay: Optional[dict]) -> Optional[dict]:
    """Shallow-merge a DB-seeded {seq_num: ...} dict with the current
    session's own overrides -- session entries replace the DB entry for
    that seq_num entirely, any seq_num only present in the DB base passes
    through unchanged. Mirrors the web backend's identically-named helper
    (backend/app/api/ngam.py)."""
    merged = dict(db_base) if db_base else {}
    if session_overlay:
        merged.update(session_overlay)
    return merged or None


class NGAMViewModel(QObject):
    """Coordinates parsing, data reduction, and database operations for Noble Gas sequence files."""

    # View updates
    statusChanged = pyqtSignal(str)
    
    # Process updates
    parsingFinished = pyqtSignal()
    parsingFailed = pyqtSignal(str)
    
    processingFinished = pyqtSignal()
    processingFailed = pyqtSignal(str)
    
    importFinished = pyqtSignal(int)
    importFailed = pyqtSignal(str)
    progressUpdated = pyqtSignal(int, str)

    def __init__(self, model: NGAMModel, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.model = model

    def parse_file(self, filepath: str) -> None:
        """Parse a .protocol sequence file (metadata only, delaying heavy MS read)."""
        if not filepath or not os.path.isfile(filepath):
            self.parsingFailed.emit("Invalid file path.")
            return

        self.statusChanged.emit("Parsing protocol file...")
        try:
            seq = parse_protocol(filepath, load_ms_files=False, read_inlet_state=False)
            seq._ms_loaded = False
            seq._inlet_state_loaded = False
            self.model.reset()
            self.model.sequence = seq
            self.parsingFinished.emit()
        except Exception as e:
            log.exception(f"Parsing failed for: {filepath}")
            self.parsingFailed.emit(str(e))

    def process_data(self) -> None:
        """Run the processing and calibration reduction chain."""
        if self.model.sequence is None:
            self.processingFailed.emit("No sequence parsed.")
            return

        self.statusChanged.emit("Processing data reduction...")

        import dataclasses
        loaded_protocols = {}

        def get_loaded_seq(path: str) -> ProtocolSequence:
            if path not in loaded_protocols:
                self.statusChanged.emit(f"Loading MS data for {os.path.basename(path)}...")
                loaded_seq = parse_protocol(path, load_ms_files=True, read_inlet_state=True)
                loaded_seq._ms_loaded = True
                loaded_seq._inlet_state_loaded = True
                loaded_protocols[path] = loaded_seq
            return loaded_protocols[path]

        try:
            # 1. On-demand load MS files and inlet state for the primary sequence
            if not getattr(self.model.sequence, "_ms_loaded", False):
                self.model.sequence = get_loaded_seq(self.model.sequence.protocol_path)

            # 2. On-demand load MS files for supplemental inlets if present
            if self.model.supplemental_inlets:
                updated_supp = []
                for inlet in self.model.supplemental_inlets:
                    src_path = inlet.source_protocol or self.model.sequence.protocol_path
                    full_seq = get_loaded_seq(src_path)
                    target_seq_num = inlet.source_seq_num if inlet.is_supplemental else inlet.seq_num
                    matching_inlet = next((i for i in full_seq.inlets if i.seq_num == target_seq_num), None)
                    if matching_inlet:
                        inlet = dataclasses.replace(inlet, ms_data=matching_inlet.ms_data)
                    updated_supp.append(inlet)
                self.model.supplemental_inlets = updated_supp

            # Merge supplemental inlets if present
            if self.model.supplemental_inlets:
                seq = merge_sequences(self.model.sequence, self.model.supplemental_inlets)
            else:
                seq = self.model.sequence

            # Query and cache database calibration states if targeted
            db_outlier_overrides: dict = {}
            db_fit_overrides: dict = {}
            if self.model.target_run_id:
                self.model.repro_references, _ = ngam_repository.build_repro_references(self.model.target_run_id)
                self.model.multi_run_linearity = (
                    ngam_repository.build_multi_run_linearity(self.model.target_run_id)
                    if self.model.config.linearity_mode == "multi"
                    else None
                )
                self.model.aliquot_volumes = ngam_repository.build_aliquot_volumes(self.model.target_run_id)
                if self.model.aliquot_volumes:
                    self.model.config.compute_activity = True
                self.model.extraction_info = ngam_repository.build_extraction_info(self.model.target_run_id)
                self.model.config.db_dilution_factors = ngam_repository.build_dilution_info(self.model.target_run_id)

                # Previously-saved outlier/exclusion/fit-override picks as a
                # base, this session's own toggles on top -- so reopening a
                # saved run reproduces the same fits instead of re-flagging
                # everything by hand. Mirrors backend/app/api/ngam.py.
                db_outlier_overrides = ngam_repository.build_db_outlier_overrides(self.model.target_run_id)
                db_fit_overrides = ngam_repository.build_db_fit_overrides(self.model.target_run_id)

            # Merge dilution factors from model into config
            if self.model.dilution_factors:
                self.model.config.dilution_factors = dict(self.model.dilution_factors)
            else:
                self.model.config.dilution_factors = {}

            # Merge IDMS config and resolve spike certified values
            if self.model.config.idms_enabled and self.model.idms_config:
                idms_cfg = dict(self.model.idms_config)
                spike_data = ngam_repository.build_spike_certified_values(idms_cfg)
                if spike_data:
                    for target_iso, entry in idms_cfg.items():
                        spike_per_inlet = entry.get("spike_per_inlet", {})
                        for seq_num, labid in spike_per_inlet.items():
                            vals = spike_data.get(str(labid))
                            if vals:
                                entry["A_spike"] = vals[0]
                                entry["A_spike_unc"] = vals[1]
                                entry["R_spike"] = vals[2]
                                entry["R_spike_unc"] = vals[3]
                                break
                self.model.config.idms_config = idms_cfg

            # Database reference enrichment
            with db_manager.get_connection() as conn:
                enrich_sequence_with_reference_amounts(seq, conn, run_id=self.model.target_run_id)

            # Merge DB-seeded overrides (base) with this session's own edits (overlay)
            merged_flag_overrides = _merge_seq_dict(db_outlier_overrides.get("flag_overrides"), self.model.flag_overrides)
            merged_excluded_standards = _merge_seq_dict(db_outlier_overrides.get("excluded_standards"), self.model.excluded_standards)
            merged_excluded_blanks = _merge_seq_dict(db_outlier_overrides.get("excluded_blanks"), self.model.excluded_blanks)
            merged_force_included_standards = _merge_seq_dict(db_outlier_overrides.get("force_included_standards"), self.model.force_included_standards)
            merged_force_included_blanks = _merge_seq_dict(db_outlier_overrides.get("force_included_blanks"), self.model.force_included_blanks)

            merged_fit_model_overrides = _merge_seq_dict(db_fit_overrides.get("fit_model_overrides"), self.model.fit_model_overrides)
            merged_blank_fit_overrides = _merge_seq_dict(db_fit_overrides.get("blank_fit_overrides"), self.model.blank_fit_overrides)
            merged_drift_fit_overrides = _merge_seq_dict(db_fit_overrides.get("drift_fit_overrides"), self.model.drift_fit_overrides)
            merged_linearity_fit_overrides = _merge_seq_dict(db_fit_overrides.get("linearity_fit_overrides"), self.model.linearity_fit_overrides)

            # Core processing pipeline call. Uncertainty trace is captured
            # here too (matches backend/app/api/ngam.py's pattern) so it's
            # available to review right after Process, not just after
            # Import -- named by run_id when reprocessing an existing run
            # (so repeated Process/Import calls overwrite the same file with
            # the latest version), falling back to the protocol path for a
            # brand-new sequence that has no run_id yet.
            unc_log_name = (
                f"seq_{self.model.target_run_id}.log" if self.model.target_run_id
                else f"parse_{os.path.basename(seq.protocol_path)}.log"
            )
            with capture_unc_trace(os.path.join(_UNC_LOG_DIR, unc_log_name)) as unc_trace:
                result = process_sequence(
                    seq,
                    sequence_id=self.model.target_run_id,
                    config=self.model.config,
                    flag_overrides=merged_flag_overrides,
                    fit_model_overrides=merged_fit_model_overrides,
                    repro_references=self.model.repro_references,
                    multi_run_linearity=self.model.multi_run_linearity,
                    aliquot_volumes=self.model.aliquot_volumes,
                    extraction_info=self.model.extraction_info,
                    excluded_standards=merged_excluded_standards,
                    excluded_blanks=merged_excluded_blanks,
                    force_included_standards=merged_force_included_standards,
                    force_included_blanks=merged_force_included_blanks,
                    blank_fit_overrides=merged_blank_fit_overrides,
                    drift_fit_overrides=merged_drift_fit_overrides,
                    linearity_fit_overrides=merged_linearity_fit_overrides,
                )
            self.model.unc_trace = list(unc_trace)
            self.model.result = result

            # Save linearity snapshots
            if self.model.target_run_id:
                ngam_repository.save_linearity_snapshots(result, self.model.target_run_id)

            self.processingFinished.emit()
        except Exception as e:
            log.exception("Data processing failed.")
            self.processingFailed.emit(str(e))

    def get_existing_id(self) -> Optional[int]:
        """Check if the sequence already exists in the database."""
        if not self.model.sequence:
            return None
        return ngam_repository.get_existing_sequence_id(
            self.model.sequence.protocol_path,
            self.model.sequence.lv_time_start
        )

    def import_data(self, delete_existing: bool = False) -> None:
        """Perform import transaction into the database."""
        if self.model.sequence is None:
            self.importFailed.emit("No sequence to import.")
            return

        self.statusChanged.emit("Importing sequence run to database...")
        
        try:
            uid = get_current_user_id()
            user_stamp = normalize_login_name(uid) if uid else "unknown"
        except Exception:
            user_stamp = "unknown"

        def _progress(step: int, msg: str):
            self.progressUpdated.emit(step, msg)

        try:
            seq_id = ngam_repository.import_sequence(
                seq=self.model.sequence,
                run_id=self.model.target_run_id,
                delete_existing=delete_existing,
                progress_callback=_progress,
                user_stamp=user_stamp,
            )

            # Persist per-point outlier flags and the resolved signal fit so
            # reopening this run restores the same picks instead of starting
            # auto-detection fresh (see process_data()'s DB-seeded read-back).
            # Independently guarded: a failure here shouldn't fail the import
            # itself, since the sequence data is already safely committed.
            if self.model.result is not None:
                try:
                    with db_manager.get_connection() as conn:
                        ngam_repository.save_block_signal_flags(seq_id, self.model.result, conn)
                        conn.commit()
                except Exception as e:
                    log.warning(f"save_block_signal_flags failed for run {seq_id}: {e}")

                try:
                    with db_manager.get_connection() as conn:
                        ngam_repository.save_signal_fit_detail(seq_id, self.model.result, conn)
                        conn.commit()
                except Exception as e:
                    log.warning(f"save_signal_fit_detail failed for run {seq_id}: {e}")

            self.importFinished.emit(seq_id)
        except Exception as e:
            log.exception("Database import failed.")
            self.importFailed.emit(str(e))
