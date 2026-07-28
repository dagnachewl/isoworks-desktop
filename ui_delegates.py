"""
ui_delegates.py — Custom QStyledItemDelegate classes for IsoWorks table views.
Provides SampleTypeDelegate and SampleRefDelegate for rendering colour-coded
sample types and inline combo-box reference selection in run load-list tables.
"""
import logging
from PyQt5.QtWidgets import QStyledItemDelegate, QComboBox
from PyQt5.QtGui import QColor, QBrush, QFont
from PyQt5.QtCore import Qt

from sample_type_registry import _FALLBACK_COLORS_HEX

# Single source of truth is sample_type_registry._FALLBACK_COLORS_HEX.
# Re-exported here so siam_template_editor_gui keeps its existing import.
SAMPLE_TYPE_COLORS_HEX = _FALLBACK_COLORS_HEX
SAMPLE_TYPE_COLORS = dict(_FALLBACK_COLORS_HEX)  # str code → hex string

DEFAULT_BRUSH = QBrush(QColor("#E0F2FE")) 
STANDARD_FONT = QFont()
STANDARD_FONT.setBold(True)

class SampleRefDelegate(QStyledItemDelegate):
    def __init__(self, sample_ref_map, parent=None):
        super().__init__(parent)
        self.sample_ref_map = sample_ref_map
        self.items_list = []
        self.items_list.append(("", "")) 
        for our_lab_id, description in sorted(sample_ref_map.items()):
            if not our_lab_id: continue
            display_text = f"{our_lab_id} ({description})"
            self.items_list.append((display_text, our_lab_id))

    def createEditor(self, parent, option, index):
        editor = QComboBox(parent)
        for display_text, our_lab_id in self.items_list:
            editor.addItem(display_text, our_lab_id)
        editor.setEditable(False)
        return editor

    def setEditorData(self, editor, index):
        value = str(index.model().data(index, Qt.EditRole) or "")
        idx = editor.findData(value)
        editor.setCurrentIndex(idx if idx != -1 else 0)

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentData(), Qt.EditRole)

    def displayText(self, value, locale):
        return str(value or "")

    # REMOVED initStyleOption: We now let the Model (controlled by the Editor) define styles.

class SampleTypeDelegate(QStyledItemDelegate):
    def __init__(self, sample_types_map, parent=None):
        super().__init__(parent)
        self.sample_types_map = sample_types_map

    def createEditor(self, parent, option, index):
        editor = QComboBox(parent)
        # Sort items by description
        for id_str, desc in sorted(self.sample_types_map.items(), key=lambda item: item[1]):
            editor.addItem(f"{id_str}: {desc}", id_str)
        return editor

    def setEditorData(self, editor, index):
        value = str(index.model().data(index, Qt.EditRole) or "0")
        idx = editor.findData(value)
        editor.setCurrentIndex(idx if idx != -1 else 0)

    def setModelData(self, editor, model, index):
        model.setData(index, editor.currentData(), Qt.EditRole)

    def displayText(self, value, locale):
        # Maps ID (e.g., "23") to Description (e.g., "Standard")
        return self.sample_types_map.get(str(value), f"{value}")

    # REMOVED initStyleOption: The Delegate no longer forces colors. 
    # It will respect the Qt.BackgroundRole set by _apply_row_styling in the main GUI.