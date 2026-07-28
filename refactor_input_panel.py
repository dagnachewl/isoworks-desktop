import re

with open("siam_processor/views/components/input_panel.py", "r") as f:
    content = f.read()

# Fix indentation to fit inside a class if needed.
# Actually, the methods are already indented at 4 spaces.
# So we just prepend the class definition and imports.

header = """from PyQt5.QtWidgets import (
    QWidget, QLabel, QFormLayout, QVBoxLayout, QHBoxLayout, QGroupBox, QGridLayout,
    QLineEdit, QPushButton, QComboBox, QAction, QCheckBox, QSpacerItem, QSizePolicy,
    QScrollArea, QFrame, QMessageBox, QFileDialog, QToolButton
)
from PyQt5.QtCore import Qt, QSize
from PyQt5.QtGui import QIcon, QFont
from siam_processor.database import db_manager
from siam_processor.models.processor_model import ResolverInstrument
from isotope_processor import Config, Protocol, InstrumentType
import os
import logging
from icons import get_icon

class InputPanelBuilder:
    def __init__(self, parent):
        self.parent = parent

"""

# Replace `def some_method(self` with `def some_method(self` (no change needed)
# Replace internal `self.` with `self.parent.` EXCEPT when calling methods that are moved to the builder!
# The methods moved to the builder are:
# setup_input_panel
# _populate_file_formats
# _on_format_changed
# _load_default_protocol_for_format
# _apply_protocol_settings
# _build_protocol_from_current_settings

methods_in_builder = [
    "setup_input_panel",
    "_populate_file_formats",
    "_on_format_changed",
    "_load_default_protocol_for_format",
    "_apply_protocol_settings",
    "_build_protocol_from_current_settings"
]

# We want `self.parent.dsn_edit` instead of `self.dsn_edit`
# It's easiest to replace ALL `self.` with `self.parent.`
content = re.sub(r'\bself\.', 'self.parent.', content)

# Then revert `self.parent.method_name` back to `self.method_name` for the builder methods
for method in methods_in_builder:
    content = re.sub(rf'\bself\.parent\.{method}\b', f'self.{method}', content)

with open("siam_processor/views/components/input_panel.py", "w") as f:
    f.write(header + content)
