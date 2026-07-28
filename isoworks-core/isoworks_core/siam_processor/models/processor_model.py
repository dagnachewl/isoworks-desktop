import pandas as pd
from typing import Dict, Tuple, Optional

class ProcessorModel:
    """
    Holds the application state for the SIAM processor.
    Decouples raw data, calibrated results, and fits from the UI.
    """
    def __init__(self):
        # File paths
        self.data_file: Optional[str] = None
        self.standards_file: Optional[str] = None
        
        # Primary data frames
        self.data: Optional[pd.DataFrame] = None
        self.standards_data: Optional[pd.DataFrame] = None
        self.injection_data: Optional[pd.DataFrame] = None
        self.analysis_data: Optional[pd.DataFrame] = None
        self.raw_df_ea: Optional[pd.DataFrame] = None
        
        # State
        self.current_isotope: Optional[str] = None
        self.current_protocol = None
        self.current_run_id: Optional[int] = None
        
        # Post-processing summaries
        self.post_results: Optional[pd.DataFrame] = None
        self.qc_summary: Optional[pd.DataFrame] = None
        self.batch_summary: Optional[pd.DataFrame] = None
        
        # Fits
        self.memory_fits: dict = {}
        self.drift_fits: dict = {}
        self.calibration_fits: dict = {}
        
        # Multi-isotope caches
        self.multi_iso_raw: Dict[str, pd.DataFrame] = {}
        self.multi_iso_inj: Dict[str, pd.DataFrame] = {}
        self.multi_iso_analysis: Dict[str, pd.DataFrame] = {}
        self.multi_iso_fits: Dict[str, dict] = {}
        self.multi_iso_post: Dict[str, Tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]] = {}

    def clear_data(self):
        """Resets the data state, typically called when loading a new file."""
        self.data = None
        self.standards_data = None
        self.injection_data = None
        self.analysis_data = None
        self.raw_df_ea = None
        self.post_results = None
        self.qc_summary = None
        self.batch_summary = None
        self.memory_fits.clear()
        self.drift_fits.clear()
        self.calibration_fits.clear()
        self.multi_iso_raw.clear()
        self.multi_iso_inj.clear()
        self.multi_iso_analysis.clear()
        self.multi_iso_fits.clear()
        self.multi_iso_post.clear()
