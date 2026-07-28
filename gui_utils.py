"""
gui_utils.py — Shared GUI utility helpers for IsoWorks.
Provides show_message() and EmbeddedSearchBox — a QLineEdit subclass with
small action and clear buttons rendered inside the field's right margin.
"""
from PyQt5.QtWidgets import QMessageBox, QLineEdit, QToolButton, QSizePolicy, QComboBox
from PyQt5.QtCore import pyqtSignal, Qt
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QCompleter
import logging


class EmbeddedSearchBox(QLineEdit):
    """
    A QLineEdit with two small buttons embedded on the right edge:

    • **action button** — label changes dynamically (e.g. "Open →" / "Search →").
      Emits ``action_clicked``.  Also triggered by Return/Enter key.
    • **clear button** — shows "✕", visible only when the field has text.
      Emits ``clear_clicked`` and clears the field.

    Usage::

        box = EmbeddedSearchBox(action_text="Open →")
        box.action_clicked.connect(self._on_open_or_search)
        box.clear_clicked.connect(self.reset_filters)
        box.set_action_text("Search →")   # change label at any time
    """

    action_clicked = pyqtSignal()
    clear_clicked  = pyqtSignal()

    _BTN_SS = (
        "QToolButton {"
        "  border: none; background: transparent;"
        "  color: #555; font-size: 11px; font-weight: bold; padding: 0 4px;"
        "}"
        "QToolButton:hover { color: #1565C0; }"
        "QToolButton:pressed { color: #0D47A1; }"
    )
    _CLEAR_SS = (
        "QToolButton {"
        "  border: none; background: transparent;"
        "  color: #999; font-size: 12px; font-weight: bold; padding: 0 4px;"
        "}"
        "QToolButton:hover { color: #c0392b; }"
    )

    def __init__(self, parent=None, action_text: str = "Open →"):
        super().__init__(parent)
        self._btn_action = QToolButton(self)
        self._btn_action.setCursor(Qt.ArrowCursor)
        self._btn_action.setStyleSheet(self._BTN_SS)
        self._btn_action.setFocusPolicy(Qt.NoFocus)
        self._btn_action.clicked.connect(self.action_clicked)

        self._btn_clear = QToolButton(self)
        self._btn_clear.setText("✕")
        self._btn_clear.setCursor(Qt.ArrowCursor)
        self._btn_clear.setStyleSheet(self._CLEAR_SS)
        self._btn_clear.setFocusPolicy(Qt.NoFocus)
        self._btn_clear.setVisible(False)
        self._btn_clear.clicked.connect(self._on_clear)

        self.textChanged.connect(lambda t: self._btn_clear.setVisible(bool(t)))
        self.returnPressed.connect(self.action_clicked)

        self.set_action_text(action_text)

    # ------------------------------------------------------------------
    def set_action_text(self, text: str) -> None:
        self._btn_action.setText(text)
        self._btn_action.adjustSize()
        self._reposition()

    def _on_clear(self) -> None:
        self.clear()
        self.clear_clicked.emit()

    # ------------------------------------------------------------------
    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._reposition()

    def _reposition(self) -> None:
        h = self.height()
        pad = 2
        btn_h = h - pad * 2

        # size action button to its text
        self._btn_action.setFixedHeight(btn_h)
        self._btn_action.adjustSize()
        aw = self._btn_action.width()

        # clear button is square
        cw = btn_h
        self._btn_clear.setFixedSize(cw, btn_h)

        gap = 2
        # right-to-left: action | clear
        ax = self.width() - aw - pad
        cx = ax - cw - gap
        self._btn_action.move(ax, pad)
        self._btn_clear.move(cx, pad)

        # push text away from buttons
        self.setTextMargins(2, 0, aw + cw + gap + 4, 0)


def make_searchable_combo(combo: QComboBox) -> None:
    """
    Attach a contains-mode QCompleter to a QComboBox so that typing any
    substring matches — e.g. typing "Dagnachew" finds "Belachew, Dagnachew",
    and "GNIP" finds any station name containing that token.

    Call this after the combo's items are populated (or re-populated).
    The combo must already have setEditable(True).
    """
    texts = [combo.itemText(i) for i in range(combo.count())]
    completer = QCompleter(texts, combo)
    completer.setCaseSensitivity(Qt.CaseInsensitive)
    completer.setFilterMode(Qt.MatchContains)
    completer.setCompletionMode(QCompleter.PopupCompletion)
    combo.setCompleter(completer)


def show_message(parent, title, text, icon=QMessageBox.Information):
    """
    Displays a modal message box.
    
    Args:
        parent (QWidget): The parent widget.
        title (str): Window title.
        text (str): Message text.
        icon (QMessageBox.Icon): The icon style (default: Information).
    """
    msg_box = QMessageBox(parent)
    msg_box.setIcon(icon)
    msg_box.setText(text)
    msg_box.setWindowTitle(title)
    msg_box.exec_()

# =============================================================================
# SVG Icon Rendering & Cache System (Consolidated from shared_utils)
# =============================================================================
from PyQt5.QtCore import QSize, QRect, QRectF
from PyQt5.QtGui import QIcon, QFont, QPixmap, QPainter, QIconEngine
from PyQt5.QtSvg import QSvgRenderer
from PyQt5.QtWidgets import QWidget, QVBoxLayout, QLabel
from typing import Optional, Callable

SVG_ICONS = {
    # =========================================================================
    # MAIN NAVIGATION ICONS
    # =========================================================================
    "menu": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M3 4H21V6H3V4ZM3 11H21V13H3V11ZM3 18H21V20H3V18Z"></path></svg>""",
    "settings": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="-2 -2 28 28" fill="currentColor" width="24" height="24" style="overflow: visible;"><path d="M12 8a4 4 0 1 1 0 8 4 4 0 0 1 0-8zm8.94 4.34l1.06-1.84-2.12-3.67-2.12 1.22a7.96 7.96 0 0 0-1.84-1.06l-.34-2.42h-4l-.34 2.42a7.96 7.96 0 0 0-1.84 1.06l-2.12-1.22-2.12 3.67 1.06 1.84a7.96 7.96 0 0 0 0 2.12l-1.06 1.84 2.12 3.67 2.12-1.22a7.96 7.96 0 0 0 1.84 1.06l.34 2.42h4l.34-2.42a7.96 7.96 0 0 0 1.84-1.06l2.12 1.22 2.12-3.67-1.06-1.84a7.96 7.96 0 0 0 0-2.12z"/></svg>""",
    "go-home": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M10 20V14H14V20H19V12H22L12 3L2 12H5V20H10Z"></path></svg>""",
    "folder-open": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M4 6H2V20C2 21.1046 2.89543 22 4 22H18V20H4V6ZM20 4H12L10 2H4C2.89543 2 2 2.89543 2 4V16C2 17.1046 2.89543 18 4 18H20C21.1046 18 22 17.1046 22 16V6C22 4.89543 21.1046 4 20 4Z"></path></svg>""",
    "document-new": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M14 2H6C4.89543 2 4 2.89543 4 4V20C4 21.1046 4.89543 22 6 22H18C19.1046 22 20 21.1046 20 20V8L14 2ZM13 9V3.5L18.5 9H13Z"></path></svg>""",
    "view-list-tree": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M3 5H11V7H3V5ZM3 11H11V13H3V11ZM3 17H11V19H3V17ZM15.8 11H21V13H15.8C15.4 14.17 14.3 15 13 15C11.7 15 10.6 14.17 10.2 13H5V11H10.2C10.6 9.83 11.7 9 13 9C14.3 9 15.4 9.83 15.8 11ZM13 11C12.4477 11 12 11.4477 12 12C12 12.5523 12.4477 13 13 13C13.5523 13 14 12.5523 14 12C14 11.4477 13.5523 11 13 11Z"></path></svg>""",
    "applications-science": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M18 3.5C18.8284 3.5 19.5 4.17157 19.5 5V19C19.5 19.8284 18.8284 20.5 18 20.5C17.1716 20.5 16.5 19.8284 16.5 19V5C16.5 4.17157 17.1716 3.5 18 3.5ZM12.0494 6.02678C12.4206 5.56702 13.0538 5.50339 13.4882 5.86431L14.7303 6.84333C15.0118 7.05943 15.068 7.4526 14.8519 7.73413C14.6358 8.01566 14.2426 8.07185 13.9611 7.85575L12.913 7.02534C13.5243 7.6433 13.8343 8.44181 13.8054 9.25585L13.748 11.2323C13.7191 12.0463 13.386 12.822 12.8054 13.4025L13.9611 15.1442C14.2426 14.9281 14.6358 14.9843 14.8519 15.2659C15.068 15.5474 15.0118 15.9406 14.7303 16.1567L13.4882 17.1357C13.0538 17.4966 12.4206 17.433 12.0494 16.9732L9.65344 14.1357C9.28224 13.676 9.28224 13.0645 9.65344 12.6047L10.354 11.7743L8.27111 9.35123C7.9006 8.8923 7.9006 8.28092 8.27111 7.822L10.354 5.39896L12.0494 6.02678Z"></path></svg>""",
    "applications-education": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M22 10.4593C22 10.8265 21.8494 11.1783 21.5833 11.4194L12.6583 19.3828C12.2824 19.7196 11.7176 19.7196 11.3417 19.3828L2.41667 11.4194C2.15061 11.1783 2 10.8265 2 10.4593C2 9.66414 2.92341 9.15067 3.58333 9.61942L11.5 16.4882V2H12.5V16.4882L20.4167 9.61942C21.0766 9.15067 22 9.66414 22 10.4593Z"></path></svg>""",
    "dialog-information": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M13 9H11V7H13V9ZM13 17H11V11H13V17ZM12 2C6.47715 2 2 6.47715 2 12C2 17.5228 6.47715 22 12 22C17.5228 22 22 17.5228 22 12C22 6.47715 17.5228 2 12 2Z"></path></svg>""",
    "system-users": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M15 14C17.7614 14 20 16.2386 20 19V21H4V19C4 16.2386 6.23858 14 9 14H15ZM12 13C9.23858 13 7 10.7614 7 8C7 5.23858 9.23858 3 12 3C14.7614 3 17 5.23858 17 8C17 10.7614 14.7614 13 12 13Z"></path></svg>""",
    "x-office-address-book": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M8 2H16C17.1046 2 18 2.89543 18 4V20C18 21.1046 17.1046 22 16 22H8C6.89543 22 6 21.1046 6 20V4C6 2.89543 6.89543 2 8 2ZM11 6C10.4477 6 10 6.44772 10 7C10 7.55228 10.4477 8 11 8C11.5523 8 12 7.55228 12 7C12 6.44772 11.5523 6 11 6ZM14 10H8V11H14V10ZM14 12H8V13H14V12ZM14 14H8V15H14V14Z"></path></svg>""",
    "computer": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M22 18V3C22 2.44772 21.5523 2 21 2H3C2.44772 2 2 2.44772 2 3V18H10V20H7V22H17V20H14V18H22ZM20 4V16H4V4H20Z"></path></svg>""",
    "preferences-desktop-tasks": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M8 8H11V11H8V8ZM13 8H16V11H13V8ZM8 13H11V16H8V13ZM13 13H16V16H13V13ZM4 2H20C21.1046 2 22 2.89543 22 4V20C22 21.1046 21.1046 22 20 22H4C2.89543 22 2 21.1046 2 20V4C2 2.89543 2.89543 2 4 2Z"></path></svg>""",
    "preferences-system-windows": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M3 3H11V11H3V3ZM13 3H21V11H13V3ZM3 13H11V21H3V13ZM13 13H21V21H13V13Z"></path></svg>""",
    "format-list-ordered": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M7 15H21V13H7V15ZM7 19H21V17H7V19ZM7 11H21V9H7V11ZM7 7H21V5H7V7ZM4 17H2V18H3V19H4V20H2V21H4V17ZM3 6H2V2H3V3H4V4H2V5H4V6H3ZM3.25 10H2V14H4V13H3V12H4V11H3V10H3.25Z"></path></svg>""",
    "configure": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M17.6569 5.65685L19.0711 4.24264L20.4853 5.65685C21.0711 6.24264 21.0711 7.20711 20.4853 7.79289L19.0711 9.20711L17.6569 7.79289C17.0711 7.20711 17.0711 6.24264 17.6569 5.65685ZM2 11.5C2 7.35786 5.35786 4 9.5 4C11.5215 4 13.3644 4.80562 14.7061 6.17591L17.1302 3.7518C17.5186 3.36337 18.1517 3.36337 18.5401 3.7518L20.2482 5.4599C20.6366 5.84833 20.6366 6.48149 20.2482 6.86992L17.8241 9.29399C18.563 10.1982 19 11.3142 19 12.5C19 16.6421 15.6421 20 11.5 20C7.35786 20 4 16.6421 4 12.5C4 10.4785 4.80562 8.63558 6.17591 7.2939L3.7518 4.86982C3.36337 4.48139 3.79702 4.48139 3.40859 4.86982L1.70049 6.15396C1.31206 6.54238 1.31206 7.17555 1.70049 7.56398L4.12455 9.98804C3.08508 10.7583 2.37625 11.8385 2.10228 13L2 11.5Z"></path></svg>""",
    "network-workgroup": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M12 1C9.23858 1 7 3.23858 7 6C7 8.76142 9.23858 11 12 11C14.7614 11 17 8.76142 17 6C17 3.23858 14.7614 1 12 1ZM11.5 13C8.46243 13 6 15.4624 6 18.5V21H18V18.5C18 15.4624 15.5376 13 12.5 13H11.5ZM6 6C6 3.79086 7.79086 2 10 2C11.691 2 13.1364 3.06412 13.7121 4.5026C13.2995 4.5009 12.8729 4.5 12.5 4.5C9.46243 4.5 7 6.96243 7 10C7 10.1706 7.00947 10.3381 7.02766 10.5026C6.4172 9.56943 6 8.35617 6 7.14286V6ZM14 2C16.2091 2 18 3.79086 18 6V7.14286C18 8.35617 17.5828 9.56943 16.9723 10.5026C16.9905 10.3381 17 10.1706 17 10C17 6.96243 14.5376 4.5 11.5 4.5C11.1271 4.5 10.7005 4.5009 10.2879 4.5026C10.8636 3.06412 12.309 2 14 2Z"></path></svg>""",
    "go-next": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M8.59 16.59L13.17 12L8.59 7.41L10 6L16 12L10 18L8.59 16.59Z"></path></svg>""",
    "view-list-details": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M3 4H21V6H3V4ZM3 11H15V13H3V11ZM3 18H21V20H3V18ZM17 11H21V13H17V11Z"></path></svg>""",
    "stock_convert": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M12 4V1L8 5L12 9V6C15.31 6 18 8.69 18 12C18 13.01 17.75 13.97 17.3 14.8L18.76 16.26C19.54 15.03 20 13.57 20 12C20 7.58 16.42 4 12 4ZM12 18C8.69 18 6 15.31 6 12C6 10.99 6.25 10.03 6.7 9.2L5.24 7.74C4.46 8.97 4 10.43 4 12C4 16.42 7.58 20 12 20V23L16 19L12 15V18Z"></path></svg>""",
    "process-stop": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M6 6H18V18H6V6Z"></path></svg>""",
    "dashboard": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M10 20V14H14V20H19V12H22L12 3L2 12H5V20H10Z"/></svg>""",
    "projects": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M20 6H12L10 4H4C2.89 4 2 4.89 2 6V18C2 19.11 2.89 20 4 20H20C21.11 20 22 19.11 22 18V8C22 6.89 21.11 6 20 6Z"/><path d="M7 14 L9 16 L13 12" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" opacity="0.8"/></svg>""",
    "workflow": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M12 6V9L16 5L12 1V4C7.58 4 4 7.58 4 12C4 13.57 4.46 15.03 5.24 16.26L6.7 14.8C6.25 13.97 6 13 6 12C6 8.69 8.69 6 12 6Z"/><path d="M18.76 7.74L17.3 9.2C17.74 10.04 18 11 18 12C18 15.31 15.31 18 12 18V15L8 19L12 23V20C16.42 20 20 16.42 20 12C20 10.43 19.54 8.97 18.76 7.74Z"/></svg>""",
    "management": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="13" y="3" width="8" height="7" rx="1"/><rect x="3" y="13" width="8" height="8" rx="1"/><rect x="14" y="13" width="7" height="8" rx="1"/></svg>""",

    # LABORATORY - Flask/beaker
    "laboratory": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M9 2 L9 9 L4 18 C3 19.5 4 22 6 22 L18 22 C20 22 21 19.5 20 18 L15 9 L15 2 Z"/><rect x="8" y="2" width="8" height="1.5" fill="currentColor"/><path d="M9 12 L15 12" stroke="white" stroke-width="1" opacity="0.5"/><circle cx="11" cy="16" r="1" fill="white" opacity="0.4"/><circle cx="13.5" cy="17" r="0.8" fill="white" opacity="0.4"/></svg>""",

    # =========================================================================
    # ISOTOPES & RADIOACTIVITY
    # =========================================================================
    "tritium": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" stroke-width="1.5"/><circle cx="12" cy="12" r="5" fill="none" stroke="currentColor" stroke-width="1"/><text x="12" y="15" font-size="8" font-weight="bold" text-anchor="middle" fill="currentColor">H</text><text x="16" y="10" font-size="4" font-weight="bold" fill="currentColor">3</text><path d="M12 2 L12 4 M12 20 L12 22 M2 12 L4 12 M20 12 L22 12" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>""",
    "carbon-14": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="1.5"/><text x="12" y="16" font-size="10" font-weight="bold" text-anchor="middle" fill="currentColor">C</text><text x="17" y="10" font-size="5" font-weight="bold" fill="currentColor">14</text><circle cx="12" cy="12" r="3" fill="currentColor" opacity="0.3"/></svg>""",
    "stable-isotopes": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="8" r="3"/><circle cx="7" cy="16" r="2.5"/><circle cx="17" cy="16" r="2.5"/><line x1="10.5" y1="9.5" x2="8" y2="14" stroke="currentColor" stroke-width="1.5"/><line x1="13.5" y1="9.5" x2="16" y2="14" stroke="currentColor" stroke-width="1.5"/><text x="12" y="10" font-size="6" text-anchor="middle" fill="white">O</text><text x="7" y="17.5" font-size="5" text-anchor="middle" fill="white">H</text><text x="17" y="17.5" font-size="5" text-anchor="middle" fill="white">H</text></svg>""",
    "radioactive": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="2.5" fill="currentColor"/><path d="M12 4 L12 8 M12 16 L12 20 M4.93 7.5 L8 10 M16 14 L19.07 16.5 M4.93 16.5 L8 14 M16 10 L19.07 7.5" stroke="currentColor" stroke-width="2" fill="none"/><circle cx="12" cy="5.5" r="2.5"/><circle cx="17.5" cy="16.5" r="2.5"/><circle cx="6.5" cy="16.5" r="2.5"/></svg>""",

    # =========================================================================
    # PROCESSES
    # =========================================================================
    "distillation": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M10 2 L10 8 L6 16 C5 17.5 6 20 8 20 L16 20 C18 20 19 17.5 18 16 L14 8 L14 2 Z" fill="currentColor"/><rect x="9" y="2" width="6" height="1.5" fill="currentColor"/><rect x="11" y="14" width="2" height="6" fill="currentColor" opacity="0.4"/><path d="M12 2 L12 0.5" stroke="currentColor" stroke-width="2" stroke-linecap="round"/><circle cx="9" cy="14" r="0.8" fill="white" opacity="0.3"/><circle cx="15" cy="15" r="0.6" fill="white" opacity="0.3"/></svg>""",
    "enrichment": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2 L8 8 L10 8 L10 12 L8 12 L12 18 L16 12 L14 12 L14 8 L16 8 Z" fill="currentColor"/><circle cx="12" cy="20" r="1.5" fill="currentColor"/><circle cx="9" cy="21" r="1" fill="currentColor" opacity="0.6"/><circle cx="15" cy="21" r="1" fill="currentColor" opacity="0.6"/><path d="M6 10 L6 14 M18 10 L18 14" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" opacity="0.5"/></svg>""",
    "lsc-counter": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><rect x="8" y="6" width="8" height="14" rx="1" fill="currentColor"/><rect x="9" y="14" width="6" height="6" fill="currentColor" opacity="0.4"/><circle cx="12" cy="4" r="1.5" fill="currentColor"/><path d="M10 17 L10 16 L12 14 L14 16 L14 17" fill="white" opacity="0.3"/><line x1="6" y1="10" x2="8" y2="10" stroke="currentColor" stroke-width="1.5"/><line x1="16" y1="10" x2="18" y2="10" stroke="currentColor" stroke-width="1.5"/><line x1="6" y1="14" x2="8" y2="14" stroke="currentColor" stroke-width="1.5"/><line x1="16" y1="14" x2="18" y2="14" stroke="currentColor" stroke-width="1.5"/></svg>""",

    # CALIBRATION - Calibration curve
    "calibration": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M4 20 L4 4 L20 4" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M6 18 L8 16 L10 14 L12 11 L14 9 L16 7 L18 6" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><circle cx="6" cy="18" r="1.5" fill="currentColor"/><circle cx="10" cy="14" r="1.5" fill="currentColor"/><circle cx="14" cy="9" r="1.5" fill="currentColor"/><circle cx="18" cy="6" r="1.5" fill="currentColor"/></svg>""",

    # MEASUREMENT - Ruler/scale
    "measurement": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><rect x="3" y="9" width="18" height="6" rx="1" fill="currentColor"/><line x1="5" y1="9" x2="5" y2="12" stroke="white" stroke-width="1"/><line x1="7" y1="9" x2="7" y2="11" stroke="white" stroke-width="1"/><line x1="9" y1="9" x2="9" y2="12" stroke="white" stroke-width="1"/><line x1="11" y1="9" x2="11" y2="11" stroke="white" stroke-width="1"/><line x1="13" y1="9" x2="13" y2="12" stroke="white" stroke-width="1"/><line x1="15" y1="9" x2="15" y2="11" stroke="white" stroke-width="1"/><line x1="17" y1="9" x2="17" y2="12" stroke="white" stroke-width="1"/><line x1="19" y1="9" x2="19" y2="11" stroke="white" stroke-width="1"/></svg>""",

    # DATA PROCESSING - Chip/processor
    "data-processing": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><rect x="7" y="7" width="10" height="10" rx="1" fill="currentColor"/><rect x="9" y="9" width="6" height="6" rx="0.5" fill="none" stroke="white" stroke-width="1"/><line x1="3" y1="9" x2="7" y2="9" stroke="currentColor" stroke-width="1.5"/><line x1="3" y1="12" x2="7" y2="12" stroke="currentColor" stroke-width="1.5"/><line x1="3" y1="15" x2="7" y2="15" stroke="currentColor" stroke-width="1.5"/><line x1="17" y1="9" x2="21" y2="9" stroke="currentColor" stroke-width="1.5"/><line x1="17" y1="12" x2="21" y2="12" stroke="currentColor" stroke-width="1.5"/><line x1="17" y1="15" x2="21" y2="15" stroke="currentColor" stroke-width="1.5"/></svg>""",

    # ANALYSIS - Magnifying glass with graph
    "analysis": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><circle cx="10" cy="10" r="7" fill="none" stroke="currentColor" stroke-width="2"/><line x1="15" y1="15" x2="21" y2="21" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/><path d="M7 11 L8 9 L10 10 L12 7 L13 9" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>""",

    # =========================================================================
    # SAMPLE MANAGEMENT
    # =========================================================================
    "sample-management": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><rect x="5" y="3" width="14" height="19" rx="1" fill="currentColor"/><rect x="8" y="1" width="8" height="3" rx="1" fill="currentColor"/><rect x="7" y="6" width="10" height="13" rx="0.5" fill="white"/><rect x="9" y="8" width="2" height="6" rx="0.5" fill="currentColor" opacity="0.7"/><rect x="12" y="8" width="2" height="6" rx="0.5" fill="currentColor" opacity="0.7"/><rect x="15" y="8" width="2" height="6" rx="0.5" fill="currentColor" opacity="0.7"/><rect x="9" y="11" width="2" height="3" fill="currentColor" opacity="0.3"/><rect x="12" y="11" width="2" height="3" fill="currentColor" opacity="0.3"/><rect x="15" y="11" width="2" height="3" fill="currentColor" opacity="0.3"/></svg>""",
    "samples": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><rect x="2" y="10" width="20" height="10" rx="1" fill="currentColor" opacity="0.3"/><rect x="4" y="6" width="3" height="12" rx="0.5" fill="currentColor"/><rect x="8.5" y="4" width="3" height="14" rx="0.5" fill="currentColor"/><rect x="13" y="5" width="3" height="13" rx="0.5" fill="currentColor"/><rect x="17.5" y="7" width="3" height="11" rx="0.5" fill="currentColor"/><rect x="4" y="13" width="3" height="5" fill="currentColor" opacity="0.4"/><rect x="8.5" y="13" width="3" height="5" fill="currentColor" opacity="0.4"/><rect x="13" y="13" width="3" height="5" fill="currentColor" opacity="0.4"/><rect x="17.5" y="13" width="3" height="5" fill="currentColor" opacity="0.4"/></svg>""",
    "batch": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><rect x="4" y="8" width="4" height="12" rx="0.5" fill="currentColor" opacity="0.7"/><rect x="10" y="6" width="4" height="14" rx="0.5" fill="currentColor"/><rect x="16" y="9" width="4" height="11" rx="0.5" fill="currentColor" opacity="0.7"/><rect x="4" y="14" width="4" height="6" fill="currentColor" opacity="0.4"/><rect x="10" y="14" width="4" height="6" fill="currentColor" opacity="0.4"/><rect x="16" y="14" width="4" height="6" fill="currentColor" opacity="0.4"/></svg>""",

    # =========================================================================
    # QUALITY CONTROL
    # =========================================================================
    "qa-qc": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2 L4 6 L4 12 C4 16.5 7 20.5 12 22 C17 20.5 20 16.5 20 12 L20 6 Z"/><path d="M8 12 L11 15 L16 9" fill="none" stroke="white" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>""",

    # =========================================================================
    # UTILITY ICONS (for buttons, actions, etc.)
    # =========================================================================
    "refresh": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M17.65 6.35C16.2 4.9 14.21 4 12 4C7.58 4 4.01 7.58 4.01 12C4.01 16.42 7.58 20 12 20C15.73 20 18.84 17.45 19.73 14H17.65C16.83 16.33 14.61 18 12 18C8.69 18 6 15.31 6 12C6 8.69 8.69 6 12 6C13.66 6 15.14 6.69 16.22 7.78L13 11H20V4L17.65 6.35Z"/></svg>""",
    "search": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M15.5 14H14.71L14.43 13.73C15.41 12.59 16 11.11 16 9.5C16 5.91 13.09 3 9.5 3C5.91 3 3 5.91 3 9.5C3 13.09 5.91 16 9.5 16C11.11 16 12.59 15.41 13.73 14.43L14 14.71V15.5L19 20.49L20.49 19L15.5 14ZM9.5 14C7.01 14 5 11.99 5 9.5C5 7.01 7.01 5 9.5 5C11.99 5 14 7.01 14 9.5C14 11.99 11.99 14 9.5 14Z"/></svg>""",
    "add": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M19 13H13V19H11V13H5V11H11V5H13V11H19V13Z"/></svg>""",
    "delete": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M6 19C6 20.1 6.9 21 8 21H16C17.1 21 18 20.1 18 19V7H6V19ZM19 4H15.5L14.5 3H9.5L8.5 4H5V6H19V4Z"/></svg>""",
    "edit": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M3 17.25V21H6.75L17.81 9.94L14.06 6.19L3 17.25ZM20.71 7.04C21.1 6.65 21.1 6.02 20.71 5.63L18.37 3.29C17.98 2.9 17.35 2.9 16.96 3.29L15.13 5.12L18.88 8.87L20.71 7.04Z"/></svg>""",
    "save": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M17 3H5C3.89 3 3 3.9 3 5V19C3 20.1 3.89 21 5 21H19C20.1 21 21 20.1 21 19V7L17 3ZM12 19C10.34 19 9 17.66 9 16C9 14.34 10.34 13 12 13C13.66 13 15 14.34 15 16C15 17.66 13.66 19 12 19ZM15 9H5V5H15V9Z"/></svg>""",
    "close": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M19 6.41L17.59 5L12 10.59L6.41 5L5 6.41L10.59 12L5 17.59L6.41 19L12 13.41L17.59 19L19 17.59L13.41 12L19 6.41Z"/></svg>""",
    "exit": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M13 3h-2v10h2V3zm4.83 2.17l-1.42 1.42C17.99 7.86 19 9.81 19 12c0 3.87-3.13 7-7 7s-7-3.13-7-7c0-2.19 1.01-4.14 2.58-5.42L6.17 5.17C4.23 6.82 3 9.26 3 12c0 4.97 4.03 9 9 9s9-4.03 9-9c0-2.74-1.23-5.18-3.17-6.83z"/></svg>""",
    "info": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M13 9H11V7H13V9ZM13 17H11V11H13V17ZM12 2C6.48 2 2 6.48 2 12C2 17.52 6.48 22 12 22C17.52 22 22 17.52 22 12C22 6.48 17.52 2 12 2ZM12 20C7.59 20 4 16.41 4 12C4 7.59 7.59 4 12 4C16.41 4 20 7.59 20 12C20 16.41 16.41 20 12 20Z"/></svg>""",
    "warning": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M1 21H23L12 2L1 21ZM13 18H11V16H13V18ZM13 14H11V10H13V14Z"/></svg>""",
    "success": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M12 2C6.48 2 2 6.48 2 12C2 17.52 6.48 22 12 22C17.52 22 22 17.52 22 12C22 6.48 17.52 2 12 2ZM10 17L5 12L6.41 10.59L10 14.17L17.59 6.58L19 8L10 17Z"/></svg>""",

    # =========================================================================
    # NEW: additional offline icons for contextual use
    # =========================================================================
    "database": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
      <ellipse cx="12" cy="5" rx="8" ry="3"/>
      <path d="M4 5v6c0 1.66 3.58 3 8 3s8-1.34 8-3V5" fill="currentColor"/>
      <path d="M4 11v6c0 1.66 3.58 3 8 3s8-1.34 8-3v-6" fill="currentColor" opacity="0.6"/>
    </svg>""",
    "log": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor">
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <rect x="6" y="7" width="12" height="2" fill="white" opacity="0.9"/>
      <rect x="6" y="11" width="10" height="2" fill="white" opacity="0.7"/>
      <rect x="6" y="15" width="8" height="2" fill="white" opacity="0.5"/>
    </svg>""",

    # HISTORY - Clock with counter-clockwise arrow
    "history": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M13 3C8.03 3 4 7.03 4 12H1L4.89 15.89L4.96 16.03L9 12H6C6 8.13 9.13 5 13 5C16.87 5 20 8.13 20 12C20 15.87 16.87 19 13 19C11.07 19 9.32 18.21 8.06 16.94L6.64 18.36C8.27 19.99 10.52 21 13 21C17.97 21 22 16.97 22 12C22 7.03 17.97 3 13 3ZM12 8V13L16.28 15.54L17 14.33L13.5 12.25V8H12Z"/></svg>""",

    # CHEMISTRY - Atom with orbiting electrons
    "chemistry": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><circle cx="12" cy="12" r="2"/><ellipse cx="12" cy="12" rx="10" ry="4" fill="none" stroke="currentColor" stroke-width="1.5"/><ellipse cx="12" cy="12" rx="10" ry="4" fill="none" stroke="currentColor" stroke-width="1.5" transform="rotate(60 12 12)"/><ellipse cx="12" cy="12" rx="10" ry="4" fill="none" stroke="currentColor" stroke-width="1.5" transform="rotate(120 12 12)"/></svg>""",

    # FLASK - Erlenmeyer flask
    "flask": """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="currentColor"><path d="M9 3V10.5L4.5 18C3.83 19.17 4.67 20.5 6 20.5H18C19.33 20.5 20.17 19.17 19.5 18L15 10.5V3H9ZM7 3H17V4H7V3ZM6 18L9.5 12H14.5L18 18H6Z"/><circle cx="9" cy="16" r="1" fill="white" opacity="0.5"/><circle cx="13" cy="17" r="0.8" fill="white" opacity="0.4"/></svg>""",
}


class SvgIconEngine(QIconEngine):
    """
    Custom QIconEngine that renders SVG at any size on-demand.
    This keeps icons as vectors and lets Qt scale them perfectly for any DPI.
    """
    def __init__(self, svg_data: str, color: QColor):
        super().__init__()
        self.svg_data = svg_data
        self.color = color
        
    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State):
        """Render the SVG at the requested size on-demand"""
        # Replace currentColor with actual color
        svg_colored = self.svg_data.replace('fill="currentColor"', f'fill="{self.color.name()}"')
        svg_colored = svg_colored.replace('stroke="currentColor"', f'stroke="{self.color.name()}"')
        
        # Render SVG directly to painter
        renderer = QSvgRenderer(svg_colored.encode('utf-8'))
        if renderer.isValid():
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            renderer.render(painter, QRectF(rect))
    
    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        """Generate a pixmap at the requested size"""
        pixmap = QPixmap(size)
        pixmap.fill(Qt.transparent)
        
        painter = QPainter(pixmap)
        self.paint(painter, QRect(0, 0, size.width(), size.height()), mode, state)
        painter.end()
        
        return pixmap


class IconCache:
    """
    Centralized icon management system using vector SVG icons.
    Icons scale perfectly on any display (including 4K/Retina).
    """
    _cache = {}
    
    @staticmethod
    def get_icon(icon_name: str, color: QColor = QColor("#E0E0E0")) -> QIcon:
        """
        Get a QIcon by name. Icons are cached for performance.
        
        Args:
            icon_name: Name of the icon from SVG_ICONS dictionary
            color: Color to render the icon (default: light gray)
        
        Returns:
            QIcon object that scales perfectly at any size
        """
        if icon_name == "calibrate":
            icon_name = "calibration"

        cache_key = f"{icon_name}_{color.name()}"
        if cache_key in IconCache._cache:
            return IconCache._cache[cache_key]

        svg_data = SVG_ICONS.get(icon_name)
        if not svg_data:
            logging.warning(f"Icon '{icon_name}' not found in SVG_ICONS")
            return QIcon()  # Return empty icon
        
        # Create icon with custom engine
        icon = QIcon(SvgIconEngine(svg_data, color))
        IconCache._cache[cache_key] = icon
        
        return icon

# =============================================================================
# Shared UI Components
# =============================================================================

class ModuleSpec:
    """Specification for a module in the launcher"""
    def __init__(
        self,
        key: str,
        title: str,
        icon: QIcon,
        description: str = "",
        make_embedded_widget: Optional[Callable] = None,
        open_external: Optional[Callable] = None,
        children: Optional[list] = None
    ):
        self.key = key
        self.title = title
        self.icon = icon
        self.description = description
        self.make_embedded_widget = make_embedded_widget
        self.open_external = open_external
        self.children = children or []


class ComingSoonWidget(QWidget):
    """Placeholder widget for modules under development"""
    def __init__(self, module_name: str = "Module", message: str = "Coming soon"):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setAlignment(Qt.AlignCenter)
        
        title = QLabel(f"🚧 {module_name}")
        title.setFont(QFont("Arial", 18, QFont.Bold))
        title.setAlignment(Qt.AlignCenter)
        
        msg = QLabel(message)
        msg.setFont(QFont("Arial", 12))
        msg.setAlignment(Qt.AlignCenter)
        msg.setStyleSheet("color: #666;")
        
        layout.addWidget(title)
        layout.addWidget(msg)
        layout.addStretch()


