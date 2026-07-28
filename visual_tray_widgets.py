"""
visual_tray_widgets.py — Visual auto-sampler tray widgets for IsoWorks.
Provides VialWidget (a clickable, colour-coded vial cell) and MultiTrayWidget
(a tabbed multi-tray grid) used in run template editors and load-list builders.
"""
from __future__ import annotations
import sys
from PyQt5.QtWidgets import QWidget, QGridLayout, QLabel, QTabWidget, QVBoxLayout
from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QResizeEvent

from sample_type_registry import _FALLBACK_COLORS_HEX, _DEFAULT_COLOR_HEX

# Build QColor map from the single source of truth in sample_type_registry.
SAMPLE_TYPE_COLORS_HEX = _FALLBACK_COLORS_HEX
SAMPLE_TYPE_COLORS: dict[str, QColor] = {
    k: QColor(v) for k, v in _FALLBACK_COLORS_HEX.items()
}
SAMPLE_TYPE_COLORS["default"] = QColor(_DEFAULT_COLOR_HEX)
ASSIGNED_UNKNOWN_COLOR = QColor("#90CAF9")  # Assigned-unknown — darker blue

class VialWidget(QLabel):
    vialLeftClicked  = pyqtSignal(int, object)
    vialRightClicked = pyqtSignal(int, object, object)
    vialDoubleClicked = pyqtSignal(int, object)

    # Aspect-ratio threshold: if w/h or h/w exceeds this, switch to box mode
    _BOX_RATIO = 1.35

    def __init__(self, position, tray_id, parent=None):
        super().__init__(str(position), parent)
        self.position = str(position)
        self.tray_id  = int(tray_id)
        self.sample_type_id = None
        self.sample_id      = None
        self.is_selected    = False
        self._color_override = None
        self._cell_w = 30   # last known cell width  (updated by setSize)
        self._cell_h = 30   # last known cell height (updated by setSize)
        self.setAlignment(Qt.AlignCenter)
        self.setWordWrap(True)
        self.setToolTip(f"Tray {self.tray_id} | Vial {self.position}\nType: Empty")
        self.setMinimumSize(10, 10)
        self.updateStyle()

    # ------------------------------------------------------------------
    def _is_box_mode(self) -> bool:
        """True when the cell is noticeably non-square → use a rounded rect."""
        w, h = max(self._cell_w, 1), max(self._cell_h, 1)
        return (w / h > self._BOX_RATIO) or (h / w > self._BOX_RATIO)

    def updateStyle(self):
        w = max(int(self._cell_w), 1)
        h = max(int(self._cell_h), 1)
        box_mode = self._is_box_mode()

        if box_mode:
            # Rounded rectangle: font scales with the shorter side
            short = min(w, h)
            font_size = max(6, int(short / 3.2))
            radius    = max(3, short // 6)
        else:
            # Circle: font scales with diameter
            font_size = max(8, int(w / 4.5))
            radius    = w // 2

        # Shrink font a bit more when displaying two lines
        text_content = self.text()
        if "<br>" in text_content or "\n" in text_content:
            font_size = max(6, int(font_size * 0.75))

        type_key = str(self.sample_type_id) if self.sample_type_id is not None else "default"
        if type_key not in SAMPLE_TYPE_COLORS:
            type_key = "0"

        if getattr(self, '_color_override', None) is not None:
            color = self._color_override
        elif type_key == "0" and self.sample_id:
            color = ASSIGNED_UNKNOWN_COLOR
        else:
            color = SAMPLE_TYPE_COLORS.get(type_key, SAMPLE_TYPE_COLORS["default"])

        border_style = "2px solid #D62D20" if self.is_selected else "1px solid #B0B0B0"

        self.setStyleSheet(f"""
            QLabel {{
                background-color: {color.name()};
                border: {border_style};
                border-radius: {radius}px;
                font-size: {font_size}px;
                font-weight: bold;
                color: #333;
                padding: 1px 2px;
            }}
            QLabel:hover {{ border: 2px solid #0078D7; }}
        """)

    # ------------------------------------------------------------------
    def setSize(self, w: float, h: float):
        """Set the vial to explicit (w × h) pixels and repaint."""
        self._cell_w = int(w)
        self._cell_h = int(h)
        self.setFixedSize(self._cell_w, self._cell_h)
        self.updateStyle()

    def setDiameter(self, diameter: float):
        """Legacy square setter — delegates to setSize."""
        self.setSize(diameter, diameter)

    def set_selected(self, selected: bool):
        self.is_selected = selected
        self.updateStyle()

    def set_color(self, hex_color: str):
        color = QColor(hex_color)
        if color.isValid():
            self._color_override = color
            self.updateStyle()

    def setData(self, sample_type_id, sample_id, display_text):
        self._color_override = None
        self.sample_type_id  = str(sample_type_id) if sample_type_id is not None else None
        self.sample_id       = sample_id

        text_str = str(display_text) if display_text else ""
        if "\n" in text_str:
            self.setTextFormat(Qt.RichText)
            self.setText(f"<div align='center'>{text_str.replace(chr(10), '<br>')}</div>")
        else:
            self.setTextFormat(Qt.PlainText)
            self.setText(text_str)

        self.setToolTip(
            f"Tray {self.tray_id} | Vial {self.position}\n"
            f"Type: {self.sample_type_id or 'Empty'}\n"
            f"ID: {self.sample_id or '—'}"
        )
        self.updateStyle()

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.vialLeftClicked.emit(self.tray_id, self.position)
        elif event.button() == Qt.RightButton:
            self.vialRightClicked.emit(self.tray_id, self.position, event.globalPos())

    def mouseDoubleClickEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.vialDoubleClicked.emit(self.tray_id, self.position)

class PicarroTrayWidget(QWidget):
    def __init__(self, tray_id, rows, cols, layout_type="AlphaNumeric", parent=None):
        super().__init__(parent)
        self.tray_id = tray_id; self.rows = rows; self.cols = cols; self.layout_type = layout_type
        self.vials = {} 
        self.layout = QGridLayout(); self.layout.setSpacing(2); self.layout.setContentsMargins(5, 5, 5, 5)
        self.setStyleSheet("QTabWidget::pane { background-color: #F8F8F8; }")        
        for r in range(self.rows):
            for c in range(self.cols):
                if self.layout_type == "AlphaNumeric": pos_name = f"{chr(ord('A') + r)}{c + 1}"
                elif self.layout_type == "Numeric": pos_name = str((r * self.cols) + c + 1)
                else: pos_name = "?"
                vial = VialWidget(pos_name, self.tray_id)
                self.layout.addWidget(vial, r, c, Qt.AlignTop | Qt.AlignLeft)
                self.vials[pos_name] = vial
        self.setLayout(self.layout)
        
    def _recalc_sizes(self):
        margins = self.layout.contentsMargins()
        sp      = self.layout.spacing()
        aw = self.width()  - margins.left() - margins.right()
        ah = self.height() - margins.top()  - margins.bottom()
        cell_w = (aw - sp * (self.cols - 1)) / self.cols
        cell_h = (ah - sp * (self.rows - 1)) / self.rows
        if cell_w >= 10 and cell_h >= 10:
            for vial in self.vials.values():
                vial.setSize(cell_w, cell_h)

    def resizeEvent(self, event: QResizeEvent):
        self._recalc_sizes()
        super().resizeEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self._recalc_sizes)

class MultiTrayWidget(QTabWidget):
    def __init__(self, tray_configs, parent=None):
        super().__init__(parent)
        self.tray_widgets = {}; self.all_vials = {}
        if not tray_configs: self.add_tray(1, 8, 12, "AlphaNumeric")
        else:
            for i, (r, c, l) in enumerate(tray_configs): self.add_tray(i+1, r, c, l)
        self.currentChanged.connect(self._on_tab_changed)

    def _on_tab_changed(self, idx):
        w = self.widget(idx)
        if isinstance(w, PicarroTrayWidget):
            QTimer.singleShot(0, w._recalc_sizes)

    def add_tray(self, tray_id, rows, cols, layout_type):
        w = PicarroTrayWidget(tray_id, rows, cols, layout_type)
        self.addTab(w, f"Tray {tray_id}")
        self.tray_widgets[tray_id] = w
        for pos, vial in w.vials.items(): self.all_vials[(int(tray_id), str(pos))] = vial
            
    def get_vial(self, tray_id, position) -> VialWidget: 
        return self.all_vials.get((int(tray_id), str(position)))
    def get_all_vials(self): return self.all_vials