"""
TRIMS LSC Details GUI - Backward Compatibility Wrapper

This file maintains backward compatibility with existing code that imports from
trims_lsc_details_gui.py by re-exporting everything from the refactored trims_lsc package.

Usage (old code still works):
    from trims_lsc_details_gui import TrimsLSCImportDialog
    from trims_lsc_details_gui import TrimsLSCDetailsWindow
    from trims_lsc_details_gui import make_lsc_parser
    
Refactored: 2026-02-08
FIXED: Missing TrimsLSCDetailsWindow class (was wrongly aliased to TrimsLSCImportDialog)
"""

import sys
import os

# Debug mode
_VERBOSE = os.environ.get('TRIMS_DEBUG', '').lower() in ('1', 'true', 'yes')

if _VERBOSE:
    print(f"DEBUG Loading trims_lsc_details_gui.py")
    print(f"DEBUG File location: {__file__}")

# ============================================================================
# PARSERS - Re-export from trims_lsc.parsers
# ============================================================================
try:
    from trims_lsc.parsers import (
        LSCParserStrategy,
        QuantulusRegistryParserModern,
        QuantulusParserStrategy,
        HidexMatrixParserStrategy,
        AlokaCsvParserStrategy,
        PackardLSCParser,
        HidexLSCParser,
        DelimitedParserStrategy,
        make_lsc_parser,
    )
    if _VERBOSE:
        print("DEBUG ✓ Parsers imported")
except ImportError as e:
    import traceback
    print("="*80)
    print("ERROR: Could not import LSC parsers")
    print("="*80)
    print(f"Error: {e}")
    print("\nTraceback:")
    traceback.print_exc()
    print("="*80)
    raise

# ============================================================================
# COMPUTATION - Re-export from trims_lsc.computation
# ============================================================================
try:
    from trims_lsc.computation.statistics import (
        compute_means,
        compute_run_params,
        _compute_net_activity_dpm_row,
    )
    if _VERBOSE:
        print("DEBUG ✓ Computation functions imported")
except ImportError as e:
    if _VERBOSE:
        print(f"WARNING Could not import computation: {e}")
    compute_means = None
    compute_run_params = None
    _compute_net_activity_dpm_row = None

# ============================================================================
# WORKERS - Re-export from trims_lsc.workers
# ============================================================================
try:
    from trims_lsc.workers import LSCImportWorker
    if _VERBOSE:
        print("DEBUG ✓ Workers imported")
except ImportError as e:
    if _VERBOSE:
        print(f"WARNING Could not import workers: {e}")
    LSCImportWorker = None

# ============================================================================
# GUI - Re-export BOTH classes from trims_lsc.gui
# ============================================================================
try:
    from trims_lsc.gui import (
        TrimsLSCImportDialog,
        TrimsLSCDetailsWindow,
    )
    
    if _VERBOSE:
        print("DEBUG ✓ GUI classes imported")
        print(f"DEBUG   - TrimsLSCImportDialog: {TrimsLSCImportDialog}")
        print(f"DEBUG   - TrimsLSCDetailsWindow: {TrimsLSCDetailsWindow}")
        print(f"DEBUG   - Are they different? {TrimsLSCDetailsWindow is not TrimsLSCImportDialog}")

except ImportError as e:
    import traceback
    print("="*80)
    print("ERROR: Could not import GUI classes")
    print("="*80)
    print(f"Error: {e}")
    print("\nTraceback:")
    traceback.print_exc()
    print("="*80)
    raise

# ============================================================================
# HELPER FUNCTIONS - Re-export from trims_lsc.gui.helpers
# ============================================================================
try:
    from trims_lsc.gui.helpers import (
        _canon_quantulus_source,
        save_hidex_me_to_db,
        calculate_and_save_final_activities,
        _get_countid_map,
        _bulk_delete_run_mean_and_raw,
        HidexMatrixMappingDialog,
    )
    if _VERBOSE:
        print("DEBUG ✓ Helper functions imported")
except ImportError as e:
    if _VERBOSE:
        print(f"WARNING Could not import helpers: {e}")
    _canon_quantulus_source = None
    save_hidex_me_to_db = None
    calculate_and_save_final_activities = None
    _get_countid_map = None
    _bulk_delete_run_mean_and_raw = None
    HidexMatrixMappingDialog = None

# ============================================================================
# QUANTULUS HELPERS - Re-export from trims_lsc.parsers.quantulus
# ============================================================================
try:
    from trims_lsc.parsers.quantulus import (
        parse_windows,
        iter_cycles,
        _parse_minutes,
    )
    if _VERBOSE:
        print("DEBUG ✓ Quantulus helpers imported")
except ImportError as e:
    if _VERBOSE:
        print(f"WARNING Could not import quantulus helpers: {e}")
    parse_windows = None
    iter_cycles = None
    _parse_minutes = None

# ============================================================================
# EXPORTS - Define __all__ for explicit exports
# ============================================================================
__all__ = [
    # GUI Classes (BOTH of them!)
    'TrimsLSCImportDialog',      # Data import dialog
    'TrimsLSCDetailsWindow',     # Main run details window
    
    # Parsers
    'make_lsc_parser',
    'LSCParserStrategy',
    'QuantulusRegistryParserModern',
    'QuantulusParserStrategy',
    'HidexMatrixParserStrategy',
    'AlokaCsvParserStrategy',
    'PackardLSCParser',
    'HidexLSCParser',
    'DelimitedParserStrategy',
    
    # Computation
    'compute_means',
    'compute_run_params',
    '_compute_net_activity_dpm_row',
    
    # Workers
    'LSCImportWorker',
    
    # Helpers
    '_canon_quantulus_source',
    'save_hidex_me_to_db',
    'calculate_and_save_final_activities',
    '_get_countid_map',
    '_bulk_delete_run_mean_and_raw',
    'HidexMatrixMappingDialog',
    'parse_windows',
    'iter_cycles',
    '_parse_minutes',
]

# ============================================================================
# VALIDATION - Ensure critical classes are available
# ============================================================================
if _VERBOSE:
    print("\n" + "="*80)
    print("VALIDATION CHECKS")
    print("="*80)
    
    missing = []
    for name in ['TrimsLSCImportDialog', 'TrimsLSCDetailsWindow', 'make_lsc_parser']:
        if globals().get(name) is None:
            missing.append(name)
            print(f"✗ {name}: MISSING")
        else:
            print(f"✓ {name}: Available")
    
    if missing:
        print("\n⚠️  WARNING: Some critical exports are missing!")
        print(f"   Missing: {', '.join(missing)}")
    else:
        print("\n✓ All critical exports available")
    
    print("="*80 + "\n")

# Final check
if TrimsLSCDetailsWindow is None:
    raise ImportError("CRITICAL: TrimsLSCDetailsWindow not available")
if TrimsLSCImportDialog is None:
    raise ImportError("CRITICAL: TrimsLSCImportDialog not available")

if _VERBOSE:
    print("DEBUG ✓ Wrapper loaded successfully\n")