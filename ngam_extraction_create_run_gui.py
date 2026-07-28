"""
ngam_extraction_create_run_gui.py
==================================
Redirects to the unified NGAMCreateRunDialog.
Use:  NGAMCreateRunDialog(parent, mode="extraction")
"""
from ngam_ingrowth_create_run_gui import NGAMCreateRunDialog

# Legacy alias kept for any stale imports
NGAMExtractionCreateRunDialog = NGAMCreateRunDialog

__all__ = ["NGAMCreateRunDialog", "NGAMExtractionCreateRunDialog"]
