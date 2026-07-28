"""
ngam_import_from_protocol_gui.py
=================================
PyQt5 widget wrapper for importing NobleControl .protocol sequences.
Forwarding wrapper to ngam_processor view package.
"""
from __future__ import annotations

from ngam_processor.views.import_protocol_widget import ImportFromProtocolWidget

__all__ = ["ImportFromProtocolWidget"]
