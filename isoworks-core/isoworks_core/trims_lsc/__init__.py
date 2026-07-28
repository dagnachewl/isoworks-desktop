"""
TRIMS LSC Module - Liquid Scintillation Counting Analysis
Modular architecture for LSC data import and analysis
"""

__version__ = '2.0.0'

# Lazy imports - only import when accessed
# This prevents PyQt5 requirement from blocking everything

def __getattr__(name):
    """Lazy loading of submodules"""
    if name == 'make_lsc_parser':
        from .parsers import make_lsc_parser
        return make_lsc_parser
    elif name == 'LSCImportWorker':
        try:
            from .workers import LSCImportWorker
            return LSCImportWorker
        except ImportError:
            return None
    elif name == 'TrimsLSCImportDialog':
        try:
            from .gui import TrimsLSCImportDialog
            return TrimsLSCImportDialog
        except ImportError:
            return None
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")

__all__ = [
    'make_lsc_parser',
    'LSCImportWorker',
    'TrimsLSCImportDialog',
]
