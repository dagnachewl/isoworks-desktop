from __future__ import annotations

from typing import Dict, List, Optional, Set
from ngam_protocol_parser import ProtocolSequence, InletPrep
from ngam_protocol_processor import (
    ProcessingConfig, SequenceProcessingResult,
    ReproReferenceInfo, MultiRunLinearityData
)


class NGAMModel:
    """Holds the in-memory state of the Noble Gas Analysis sequence processing."""

    def __init__(self, target_run_id: Optional[int] = None) -> None:
        self.sequence: Optional[ProtocolSequence] = None
        self.target_run_id: Optional[int] = target_run_id
        self.supplemental_inlets: List[InletPrep] = []

        self.config = ProcessingConfig()
        self.result: Optional[SequenceProcessingResult] = None

        # Loaded calibration states
        self.repro_references: Optional[List[ReproReferenceInfo]] = None
        self.multi_run_linearity: Optional[List[MultiRunLinearityData]] = None
        self.aliquot_volumes: Optional[Dict[int, float]] = None

        # Extraction info for water samples: {seq_num: ExtractionInfo}
        self.extraction_info = None

        # Per-inlet dilution factors: {seq_num: float}
        self.dilution_factors: Dict[int, float] = {}
        # IDMS configuration: {target_isotope: {spike_isotope, sample_ratio, spike_per_inlet, ...}}
        self.idms_config: Dict[str, Dict] = {}

        # User manual outlier overrides: {seq_num: {device: {action: [bool, ...]}}}
        self.flag_overrides: Dict[int, Dict[str, Dict[str, List[bool]]]] = {}
        # User manual fit model overrides: {seq_num: {isotope_key: model_name}}
        self.fit_model_overrides: Dict[int, Dict[str, str]] = {}
        # Standard inlet exclusions: {seq_num: None (all iso) | set of iso_keys}
        self.excluded_standards: Dict[int, Optional[Set[str]]] = {}

    def reset(self) -> None:
        """Reset state, keeping target_run_id."""
        self.sequence = None
        self.supplemental_inlets = []
        self.config = ProcessingConfig()
        self.result = None
        self.repro_references = None
        self.multi_run_linearity = None
        self.aliquot_volumes = None
        self.extraction_info = None
        self.dilution_factors = {}
        self.idms_config = {}
        self.flag_overrides = {}
        self.fit_model_overrides = {}
        self.excluded_standards = {}
