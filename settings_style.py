"""
settings_style.py — Shared button/table-header styles for IsoWorks Settings screens.

Extracted from cims_gui.py (the one Settings module that already had a
consistent style system) so the other Settings screens, which previously
had zero custom styling (bare native Qt widgets), can share the same look
instead of each inventing its own.
"""

HDR_SS = (
    "QHeaderView::section {"
    "  background:#37474F; color:white; font-weight:bold;"
    "  padding:4px 6px; border:none;"
    "}"
)
BTN_SS = (
    "QPushButton{background:#546E7A;color:white;font-weight:bold;"
    "border:none;padding:4px 10px;border-radius:3px;}"
    "QPushButton:hover{background:#37474F;}"
    "QPushButton:disabled{background:#B0BEC5;color:#78909C;}"
)
BTN_ADD_SS = (
    "QPushButton{background:#2E7D32;color:white;font-weight:bold;"
    "border:none;padding:4px 10px;border-radius:3px;}"
    "QPushButton:hover{background:#1B5E20;}"
    "QPushButton:disabled{background:#B0BEC5;color:#78909C;}"
)
BTN_DEL_SS = (
    "QPushButton{background:#C62828;color:white;font-weight:bold;"
    "border:none;padding:4px 10px;border-radius:3px;}"
    "QPushButton:hover{background:#B71C1C;}"
    "QPushButton:disabled{background:#B0BEC5;color:#78909C;}"
)
