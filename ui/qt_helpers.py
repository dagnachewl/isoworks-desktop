"""
ui/qt_helpers.py — PyQt5 utility helpers for the IsoWorks processor UI.
Provides get_or_create_output_tabs(), set_instrument_combo_silent(), and
sync_post_tab_visibility() for managing tab widgets and combo-box state.
"""

# ui/qt_helpers.py
from __future__ import annotations
def get_or_create_output_tabs(window):
    from PyQt5.QtWidgets import QTabWidget, QWidget, QVBoxLayout
    # 1) Common attribute names
    for attr in ("output_tabs", "tabs", "tab_widget", "central_tabs"):
        tabw = getattr(window, attr, None)
        if isinstance(tabw, QTabWidget):
            return tabw
    # 2) Search tree
    found = window.findChildren(QTabWidget)
    if found:
        window.output_tabs = found0
        return found0
    # 3) Create
    cw = window.centralWidget()
    if cw is None:
        cw = QWidget(window); window.setCentralWidget(cw)
    lay = cw.layout()
    if lay is None:
        lay = QVBoxLayout(cw); cw.setLayout(lay)
    tabw = QTabWidget(cw); lay.addWidget(tabw); window.output_tabs = tabw
    return tabw

def set_instrument_combo_silent(window, label: str):
    if not label: return
    combo = getattr(window, "instrument_combo", None)
    if combo is None: return
    cur = combo.currentText()
    if cur == label: return
    from PyQt5.QtCore import Qt
    combo.blockSignals(True)
    try:
        idx = combo.findText(label, Qt.MatchFixedString)
        if idx >= 0: combo.setCurrentIndex(idx)
    finally:
        combo.blockSignals(False)

def tab_host(window):
    host = getattr(window, "output_tabs", None) or getattr(window, "tabs", None)
    if host is None:
        host = get_or_create_output_tabs(window)
    return host

def sync_post_tab_visibility(window):
    host = tab_host(window)
    post_tab = getattr(window, "post_tab", None)
    if host is None or post_tab is None: return
    try:
        label = window.instrument_combo.currentText()
    except Exception:
        label = ""
    is_irms = label in ("IRMS (EA)", "IRMS (Thermo DI)")
    idx = host.indexOf(post_tab)
    if idx != -1:
        host.setTabEnabled(idx, bool(is_irms))
    post_tab.setVisible(bool(is_irms))

def ensure_post_ui(window, builder=None):
    if getattr(window, "post_results_table", None) is not None:
        return True
    if builder is None:
        builder = getattr(window, "setup_postprocess_tab", None)
    if builder is None:
        return False
    try:
        builder()
        try:
            window.reset_postprocess(hide_tab=True)
            sync_post_tab_visibility(window)
        except Exception as e:

            logging.warning(f"Exception caught: {e}")
        return getattr(window, "post_results_table", None) is not None
    except Exception as e:

        logging.warning(f"Exception caught: {e}"); return False
