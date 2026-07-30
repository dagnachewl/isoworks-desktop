"""
trims_distillation_details_gui.py — TRIMS distillation run details view for IsoWorks.
Provides TrimsDistillationDetailsWindow for viewing and editing the load list,
sample results, and metadata of an existing distillation run, with DYMO label support.
"""
from __future__ import annotations
import os
import sys
import logging
import getpass
from datetime import datetime
from typing import Optional, List, Tuple, Dict, Set

from PyQt5.QtCore import Qt, QEvent, QPersistentModelIndex, QTimer, QSize
from PyQt5.QtGui import (
    QStandardItemModel, QStandardItem, QColor, QBrush, QDoubleValidator, 
    QTextDocument, QFont
)
from PyQt5.QtPrintSupport import QPrinter, QPrintDialog, QPrinterInfo
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QStyle,
    QGroupBox, QLineEdit, QTextEdit, QDateTimeEdit, QComboBox,
    QPushButton, QTableView, QMessageBox, QHeaderView, QStyledItemDelegate, QLabel,
    QAbstractItemView, QSizePolicy, QFormLayout, QDialog, QSpinBox, QFileDialog
)
from sqlalchemy import text

from db_core import db_manager
from shared_utils import check_employee_privilege, set_status, get_current_user_id
from gui_utils import show_message
from sample_label_gui import SampleLabelDialog, analysis_spec

try:
    from dymo_utils import DymoLabelPrinter
    HAS_DYMO_SUPPORT = True
except ImportError:
    HAS_DYMO_SUPPORT = False
    
# ------------------ Delegates ------------------

class ColumnHighlightDelegate(QStyledItemDelegate):
    def __init__(self, parent=None, target_columns=None, bg_color="#e3f2fd"):
        super().__init__(parent)
        self.target_columns = set(target_columns or [])
        self.bg_color = QColor(bg_color)
        self.focus_border_color = QColor("#0078d7")

    def set_target_columns(self, cols: List[int]):
        self.target_columns = set(cols)

    def paint(self, painter, option, index):
        if index.column() in self.target_columns:
            painter.save()
            painter.fillRect(option.rect, self.bg_color)
            painter.restore()
        super().paint(painter, option, index)
        if (option.state & QStyle.State_HasFocus) and (index.flags() & Qt.ItemIsEditable):
            painter.save()
            pen = painter.pen(); pen.setColor(self.focus_border_color); pen.setWidth(2)
            painter.setPen(pen); rect = option.rect.adjusted(1, 1, -1, -1)
            painter.drawRect(rect); painter.restore()

class ECDelegate(ColumnHighlightDelegate):
    def __init__(self, parent=None, post_mode: bool = False, max_ec_provider=None, status_setter=None):
        super().__init__(parent)
        self.post_mode = post_mode
        self.max_ec_provider = max_ec_provider
        self.status_setter = status_setter
        self._open_editor = None
        self._editor_index = None

    def createEditor(self, parent, option, index):
        editor = QLineEdit(parent)
        editor.setValidator(QDoubleValidator(bottom=-1e12, top=1e12, decimals=1))
        editor.installEventFilter(self)
        self._open_editor = editor
        self._editor_index = QPersistentModelIndex(index)
        return editor

    def setEditorData(self, editor, index):
        text = index.data(Qt.EditRole)
        editor.setText(text or ""); editor.selectAll()

    def setModelData(self, editor, model, index):
        model.setData(index, editor.text().strip(), Qt.EditRole)

    def force_close_active_editor(self):
        if self._open_editor:
            self.commitData.emit(self._open_editor)
            self.closeEditor.emit(self._open_editor, QStyledItemDelegate.NoHint)
            self._open_editor = None

    def eventFilter(self, editor, event):
        if event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Return, Qt.Key_Enter):
            if self.post_mode and self.max_ec_provider and self.status_setter:
                try: val = float(editor.text().strip() or 0)
                except: val = 0.0
                max_ec = float(self.max_ec_provider()) or 0.0
                if val > 0:
                    st = 3 if val <= max_ec else 99
                    if self._editor_index: self.status_setter(self._editor_index.row(), st)
            self.commitData.emit(editor)
            self.closeEditor.emit(editor, QStyledItemDelegate.NoHint)
            self._advance_focus()
            return True
        return super().eventFilter(editor, event)

    def _advance_focus(self):
        view = self.parent()
        idx = self._editor_index if self._editor_index else (view.currentIndex() if view else None)
        self._open_editor = None; self._editor_index = None
        if isinstance(view, QTableView) and idx and idx.isValid():
            editable = getattr(view, "_editable_columns", set())
            if not editable or idx.column() in editable:
                if view.model().rowCount() > 0:
                    next_row = (idx.row() + 1) % view.model().rowCount()
                    next_idx = view.model().index(next_row, idx.column())
                    view.setCurrentIndex(next_idx); view.edit(next_idx)
                    view.scrollTo(next_idx, QAbstractItemView.PositionAtCenter)

class RemarksDelegate(ColumnHighlightDelegate):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._open_editor = None
        self._editor_index = None
    def createEditor(self, parent, option, index):
        editor = QLineEdit(parent); editor.installEventFilter(self)
        self._open_editor = editor; self._editor_index = QPersistentModelIndex(index)
        return editor
    def setEditorData(self, editor, index):
        editor.setText(index.data(Qt.EditRole) or ""); editor.selectAll()
    def setModelData(self, editor, model, index):
        model.setData(index, editor.text(), Qt.EditRole)
    def force_close_active_editor(self):
        if self._open_editor:
            self.commitData.emit(self._open_editor)
            self.closeEditor.emit(self._open_editor, QStyledItemDelegate.NoHint)
            self._open_editor = None
    def eventFilter(self, editor, event):
        if event.type() == QEvent.KeyPress and event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.commitData.emit(editor); self.closeEditor.emit(editor, QStyledItemDelegate.NoHint)
            view = self.parent()
            idx = self._editor_index if self._editor_index else (view.currentIndex() if view else None)
            self._open_editor = None; self._editor_index = None
            if isinstance(view, QTableView) and idx and idx.isValid():
                editable = getattr(view, "_editable_columns", set())
                if not editable or idx.column() in editable:
                    if view.model().rowCount() > 0:
                        next_row = (idx.row() + 1) % view.model().rowCount()
                        next_idx = view.model().index(next_row, idx.column())
                        view.setCurrentIndex(next_idx); view.edit(next_idx)
                        view.scrollTo(next_idx, QAbstractItemView.PositionAtCenter)
            return True
        return super().eventFilter(editor, event)

class StatusDelegate(QStyledItemDelegate):
    def __init__(self, parent=None, options=None, on_change=None):
        super().__init__(parent)
        self.options = options or []; self.on_change = on_change
        self.bg_color = QColor("#e3f2fd"); self.target_columns = set()
    def set_target_columns(self, cols): self.target_columns = set(cols)
    def paint(self, painter, option, index):
        if index.column() in self.target_columns:
            painter.save(); painter.fillRect(option.rect, self.bg_color); painter.restore()
        super().paint(painter, option, index)
    def createEditor(self, parent, option, index):
        combo = QComboBox(parent)
        for c, d in self.options: combo.addItem(d, c)
        return combo
    def setEditorData(self, editor, index):
        val = index.data(Qt.EditRole)
        try: val = int(val)
        except: val = index.data(Qt.UserRole)
        if val is not None:
            idx = editor.findData(val)
            if idx >= 0: editor.setCurrentIndex(idx)
    def setModelData(self, editor, model, index):
        code = editor.currentData(); display = editor.currentText()
        model.setData(index, code, Qt.EditRole); model.setData(index, display, Qt.DisplayRole)
        model.setData(index, code, Qt.UserRole)
        if self.on_change: self.on_change(index.row(), int(code))

# ------------------ Table ------------------
class FastEntryTable(QTableView):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._editable_columns = set()
    def set_editable_columns(self, columns): self._editable_columns = set(columns)
    def edit(self, index, trigger=None, event=None):
        if not index.isValid() or (self._editable_columns and index.column() not in self._editable_columns): return False
        return super().edit(index, trigger or QAbstractItemView.AllEditTriggers, event)
    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and self.state() != QAbstractItemView.EditingState:
            idx = self.currentIndex()
            if self.edit(idx): self.scrollTo(idx, QAbstractItemView.PositionAtCenter); return
        super().keyPressEvent(event)

# ------------------ Print Dialog ------------------
class GenericPrintDialog(QDialog):
    def __init__(self, run_id, mode="Label", parent=None):
        super().__init__(parent)
        self.run_id = run_id
        self.mode = mode # "Label" or "Report"
        self.setWindowTitle(f"Print {mode}s - Batch {run_id}")
        self.resize(450, 350)
        self.setAttribute(Qt.WA_DeleteOnClose) # Cleanup on close
        self._init_ui()
        
    def _init_ui(self):
        layout = QVBoxLayout(self)
        
        form = QFormLayout()
        
        self.txtRunID = QLineEdit(str(self.run_id)); self.txtRunID.setReadOnly(True)
        form.addRow("Distillation Run ID:", self.txtRunID)
        
        self.spinCopies = QSpinBox(); self.spinCopies.setRange(1, 100); self.spinCopies.setValue(1)
        form.addRow("Number of copies:", self.spinCopies)
        
        # Device Selection
        self.cmbDevice = QComboBox()
        
        if self.mode == "Label":
            # For labels, we prioritize Dymo logic, but user might want standard printer too?
            # For this implementation, "Dymo LabelWriter" implies using the OLE method
            self.cmbDevice.addItem("DYMO LabelWriter (Direct OLE)")
            self.cmbDevice.insertSeparator(1)
            
        printers = QPrinterInfo.availablePrinters()
        default_p = QPrinterInfo.defaultPrinter().printerName()
        for p in printers:
            self.cmbDevice.addItem(p.printerName())
            if p.printerName() == default_p and self.mode != "Label":
                self.cmbDevice.setCurrentText(default_p)
                
        form.addRow("Device:", self.cmbDevice)
        
        # Template/Format Selection
        self.cmbFormat = QComboBox()
        self.btnBrowseLabel = QPushButton("Browse...")
        self.btnBrowseLabel.setVisible(False)
        self.btnBrowseLabel.clicked.connect(self._browse_label_file)
        
        if self.mode == "Label":
            # These map to your .label files. 
            # You should configure the actual paths in a config file or constants
            self.cmbFormat.addItem("Standard Flask Label", "flask_std.label") 
            self.cmbFormat.addItem("Small Flask Label", "flask_small.label")
            self.cmbFormat.addItem("QR Code Label", "flask_qr.label")
            self.cmbFormat.addItem("Custom File...", "custom")
            
            # Show browse button if custom
            self.cmbFormat.currentIndexChanged.connect(self._on_format_changed)
        else:
            self.cmbFormat.addItems(["Standard Report", "Summary Report"])
            
        format_layout = QHBoxLayout()
        format_layout.addWidget(self.cmbFormat)
        format_layout.addWidget(self.btnBrowseLabel)
        form.addRow("Format:", format_layout)
        
        layout.addLayout(form)
        layout.addStretch(1)
        
        # Buttons
        btns = QHBoxLayout()
        self.btnPrint = QPushButton("Print")
        self.btnPrint.clicked.connect(self._do_print)
        self.btnCancel = QPushButton("Cancel")
        self.btnCancel.clicked.connect(self.reject)
        
        btns.addStretch()
        btns.addWidget(self.btnPrint)
        btns.addWidget(self.btnCancel)
        layout.addLayout(btns)
        
        self.custom_label_path = ""

    def _on_format_changed(self):
        is_custom = self.cmbFormat.currentData() == "custom"
        self.btnBrowseLabel.setVisible(is_custom)

    def _browse_label_file(self):
        fname, _ = QFileDialog.getOpenFileName(self, "Select Dymo Label", "", "Dymo Labels (*.label)")
        if fname:
            self.custom_label_path = fname

    def _get_label_path(self):
        data = self.cmbFormat.currentData()
        if data == "custom":
            return self.custom_label_path
        
        # Assuming a 'labels' subfolder in current app directory
        # Adjust base_path as needed
        base_path = os.path.join(os.getcwd(), "labels")
        return os.path.join(base_path, data)

    def _do_print(self):
        device = self.cmbDevice.currentText()
        copies = self.spinCopies.value()
        
        if self.mode == "Label" and "DYMO" in device:
            self._print_dymo_ole(copies)
        else:
            self._print_standard_qt(device, copies)

    def _print_dymo_ole(self, copies):
        """Replicates PrintLabel from mDymoLabelWriter.bas using Python OLE"""
        if not HAS_DYMO_SUPPORT:
            QMessageBox.critical(self, "Error", "Dymo Support libraries (pywin32) not installed.")
            return

        label_path = self._get_label_path()
        if not os.path.exists(label_path):
             QMessageBox.warning(self, "Error", f"Label file not found:\n{label_path}\n\nPlease place .label files in a 'labels' folder.")
             return

        printer = DymoLabelPrinter()
        if not printer.connect():
            QMessageBox.critical(self, "Error", "Could not connect to DYMO Label Software. Is it installed?")
            return

        # Fetch Data
        try:
            with db_manager.get_connection() as conn:
                # Query matches your VBA context - getting FlaskID and SampleName
                sql = text("""
                    SELECT pd.FlaskID, s.sName, pd.ECbeforer 
                    FROM TRIMS.PrimaryDistillation pd 
                    JOIN Analysis a ON a.AnalysisID=pd.AnalysisID 
                    JOIN Sample s ON s.SampleID=a.SampleID AND s.Prefix=a.Prefix 
                    WHERE pd.RunID=:r 
                    ORDER BY pd.FlaskID
                """)
                rows = conn.execute(sql, {"r": self.run_id}).fetchall()
                
            success_count = 0
            
            for row in rows:
                # Map DB columns to Label Object Names (from your .label file)
                # You need to open your .label file in Dymo Designer and check Object names
                field_data = {
                    "FlaskID": str(row.FlaskID),      # Assuming Object Name "FlaskID"
                    "SampleName": str(row.sName),     # Assuming Object Name "SampleName"
                    "EC": str(row.ECbeforer or "")    # Assuming Object Name "EC"
                }
                
                if printer.print_label(label_path, field_data, copies):
                    success_count += 1
            
            printer.close()
            QMessageBox.information(self, "Success", f"Sent {success_count} labels to Dymo LabelWriter.")
            self.accept()

        except Exception as e:
            QMessageBox.critical(self, "Error", f"Printing failed: {e}")

    def _print_standard_qt(self, printer_name, copies):
        """Fallback or Report Printing"""
        printer = QPrinter(QPrinter.HighResolution)
        printer.setPrinterName(printer_name)
        
        doc = QTextDocument()
        html = f"<h1>Distillation Batch {self.run_id} - {self.mode}</h1>"
        html += f"<p><b>Date:</b> {datetime.now().strftime('%Y-%m-%d %H:%M')}</p><hr>"
        
        # Build HTML Table
        html += "<table border='1' cellspacing='0' cellpadding='4' width='100%'>"
        html += "<thead><tr bgcolor='#eeeeee'><th>Flask</th><th>Sample</th><th>EC-Pre</th><th>EC-Post</th><th>Status</th></tr></thead><tbody>"
        
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("SELECT pd.FlaskID, s.sName, pd.ECbeforer, pd.ECafter, pd.Status FROM TRIMS.PrimaryDistillation pd JOIN Analysis a ON a.AnalysisID=pd.AnalysisID JOIN Sample s ON s.SampleID=a.SampleID AND s.Prefix=a.Prefix WHERE pd.RunID=:r ORDER BY pd.FlaskID"), {"r": self.run_id})
                for row in rows:
                    html += f"<tr><td>{row.FlaskID}</td><td>{row.sName}</td><td>{row.ECbeforer or ''}</td><td>{row.ECafter or ''}</td><td>{row.Status}</td></tr>"
            html += "</tbody></table>"
            
            doc.setHtml(html)
            for _ in range(copies):
                doc.print_(printer)
                
            QMessageBox.information(self, "Success", f"Sent to {printer_name}")
            self.accept()
        except Exception as e:
            QMessageBox.critical(self, "Error", str(e))

# ------------------ Main Window (Dialog) ------------------
class TrimsDistillationDetailsWindow(QDialog):
    # Modes
    MODE_PRE = "pre"  
    MODE_POST = "post" 
    
    COLOR_HEADER_ACTIVE = QColor("#fff176")

    def __init__(self, run_id: int, link_criteria: str = "", parent: Optional[QWidget] = None, compact_ui: bool = True) -> None:
        super().__init__(parent)
        self.run_id = run_id
        self.link_criteria = link_criteria
        self.compact_ui = compact_ui
        
        self.setAttribute(Qt.WA_DeleteOnClose, True)  
        
        self.setWindowTitle(f"DistillationBatch::{run_id}")              
        self.resize(1200, 800)
        
        self.has_write_priv = False
        self._is_locked = False

        # UI components
        self.btnEditMeta = QPushButton("Edit")
        self.btnSaveMeta = QPushButton("Save")
        self.btnCancelMeta = QPushButton("Cancel")
        self.btnSaveMeta.hide()
        self.btnCancelMeta.hide()
        self.btnFinalize = QPushButton("Finalize Batch")
        self.btnPrintLabels = QPushButton("Print Labels")
        self.btnPrintReport = QPushButton("Print Report")
        
        self.btnEditPost = QPushButton("Edit EC After Distillation")
        self.btnEditPost.setCheckable(True)
        self.btnEditPost.setEnabled(False)

        self.btnClose = QPushButton("Close")
        
        self.txtRunID = QLineEdit()
        self.dtStart = QDateTimeEdit()
        self.dtEnd = QDateTimeEdit()
        self.cmbTechnician = QComboBox()
        self.txtRemarks = QTextEdit()
        self.lblMaxEC = QLabel("")
        
        self.table = FastEntryTable()
        self.model = QStandardItemModel()
        self.table.setModel(self.model)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setEditTriggers(QTableView.NoEditTriggers)

        # Delegates
        self.status_options: List[Tuple[int, str]] = []
        self.status_map: Dict[int, str] = {}
        
        self.ec_pre_delegate = ECDelegate(self.table, post_mode=False)
        self.ec_post_delegate = ECDelegate(self.table, post_mode=True, max_ec_provider=lambda: self.max_ec_small, status_setter=self._set_status_for_row)
        self.remarks_delegate = RemarksDelegate(self.table)
        self.status_delegate = StatusDelegate(self.table, options=self.status_options, on_change=self._set_status_for_row)

        self.status_label = QLabel("")
        set_status(self.status_label, "Ready", "neutral")

        self.current_mode = self.MODE_PRE
        self.max_ec_small: float = 0.0

        self._init_ui()
        
        try:
            self._check_privileges()
            self._load_global_parameters()
            self._load_status_options()
            self._load_technicians()
            self._load_header()
            self._load_mode(self.MODE_PRE)
            self._apply_read_only_state()
            
        except Exception as e:
            logging.exception("Initialization error")
            set_status(self.status_label, f"Error: {e}", "error")

    def _init_ui(self) -> None:
        main = QVBoxLayout(self)
        
        top_actions = QHBoxLayout()
        top_actions.addStretch(1)
        top_actions.addWidget(self.btnEditMeta)
        top_actions.addWidget(self.btnSaveMeta)
        top_actions.addWidget(self.btnCancelMeta)
        top_actions.addSpacing(12)
        top_actions.addWidget(self.btnFinalize)
        top_actions.addWidget(self.btnPrintLabels)
        top_actions.addWidget(self.btnPrintReport)
        top_actions.addSpacing(12)
        top_actions.addWidget(self.btnClose)
        main.addLayout(top_actions)
        
        self._build_header_form()
        main.addWidget(self.header_group)

        mode_actions = QHBoxLayout()
        mode_actions.addWidget(QLabel("<b>Pre- and Post-Distillation EC Data Entry:</b>"))
        mode_actions.addSpacing(100)
        mode_actions.addWidget(self.btnEditPost)
        mode_actions.addStretch(1)
        main.addLayout(mode_actions)
        self._apply_table_styles()
        main.addWidget(self.table, 1)
        main.addWidget(self.status_label)

        # Connections
        self.btnEditMeta.clicked.connect(self._start_metadata_edit)
        self.btnSaveMeta.clicked.connect(self._save_metadata_edit)
        self.btnCancelMeta.clicked.connect(self._cancel_metadata_edit)
        self.btnEditPost.toggled.connect(self._on_edit_post_toggled)
        self.btnFinalize.clicked.connect(self._finalize_batch)
        self.btnPrintLabels.clicked.connect(lambda: self._open_print_dialog("Label"))
        self.btnPrintReport.clicked.connect(lambda: self._open_print_dialog("Report"))
        self.btnClose.clicked.connect(self.reject)

    def _open_print_dialog(self, mode):
        if mode == "Label":
            self._print_distillation_labels()
        else:
            dlg = GenericPrintDialog(self.run_id, mode, self)
            dlg.exec_()

    def _print_distillation_labels(self):
        """Fetch flasks for this run and open the QR label printer."""
        try:
            with db_manager.get_connection() as conn:
                rows = conn.execute(text("""
                    SELECT pd.FlaskID,
                           s.Prefix, s.SampleID, s.sName,
                           a.AnalysisID
                    FROM   TRIMS.PrimaryDistillation pd
                    JOIN   Analysis a ON a.AnalysisID = pd.AnalysisID
                    JOIN   Sample   s ON s.SampleID   = a.SampleID
                                     AND s.Prefix     = a.Prefix
                    WHERE  pd.RunID = :r
                    ORDER  BY pd.FlaskID
                """), {"r": self.run_id}).fetchall()
        except Exception as exc:
            logging.error("Distillation label fetch: %s", exc)
            show_message(self, "Error", str(exc))
            return
        if not rows:
            show_message(self, "No Data", "No flasks found for this run.")
            return
        specs = [
            analysis_spec(
                prefix=r.Prefix or "",
                sample_id=str(r.SampleID),
                analysis_id=str(r.AnalysisID),
                container_num=f"Flask {r.FlaskID}",
                job_type="Distillation",
            )
            for r in rows
        ]
        SampleLabelDialog.show_batch(specs, parent=self).exec_()

    def _build_header_form(self):
        self.header_group = QGroupBox("Batch Header")
        self.header_group.setStyleSheet("""
        QGroupBox { background-color: #FAFAFC; border: 1px solid #D9D9E3; border-radius: 8px; margin-top: 8px; font-weight: 600; padding-top: 10px; }
        QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 6px; color: #2D2D33; font-size: 13px; }
        QLabel.header-label { color: #3A3A44; font-size: 12.5px; font-weight: 500; }
        QLineEdit, QComboBox, QTextEdit, QDateTimeEdit { background: #FFFFFF; border: 1px solid #D0D0DD; border-radius: 6px; padding: 4px 6px; font-size: 12.5px; }
        QLineEdit:focus, QComboBox:focus, QTextEdit:focus, QDateTimeEdit:focus { border: 1px solid #4C82FF; outline: none; }
        QDateTimeEdit::drop-down { background: #C5CAE9; border: none; width: 22px; border-radius: 0 6px 6px 0; }
        QDateTimeEdit::drop-down:hover { background: #7986CB; }
        QDateTimeEdit::drop-down:pressed { background: #3949AB; }
        QDateTimeEdit::down-arrow { image: none; width: 0; height: 0; border-left: 5px solid transparent; border-right: 5px solid transparent; border-top: 6px solid #3949AB; }
        """)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        margin = 10 if self.compact_ui else 14
        form.setContentsMargins(margin, margin, margin, margin)
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)

        def header_label(text: str) -> QLabel:
            lbl = QLabel(text); lbl.setProperty("class", "header-label"); lbl.setMinimumWidth(110)
            return lbl

        self.txtRunID.setReadOnly(True); self.txtRunID.setFixedWidth(110); self.txtRunID.setStyleSheet("background-color: #F2F3F7; font-weight: 600;")
        form.addRow(header_label("Run No.:"), self.txtRunID)

        self.cmbTechnician.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        form.addRow(header_label("Technician:"), self.cmbTechnician)

        # Fix 5: Dates Wider
        for dt in (self.dtStart, self.dtEnd):
            dt.setCalendarPopup(True); dt.setDisplayFormat("yyyy-MM-dd HH:mm")
            dt.setFixedWidth(180 if self.compact_ui else 200)
            dt.setEnabled(False)

        date_row = QHBoxLayout(); date_row.setContentsMargins(0,0,0,0); date_row.setSpacing(16)
        date_row.addWidget(QLabel("Start:")); date_row.addWidget(self.dtStart)
        date_row.addSpacing(16)
        date_row.addWidget(QLabel("End:")); date_row.addWidget(self.dtEnd); date_row.addStretch(1)
        form.addRow(header_label("Dates:"), date_row)

        self.txtRemarks.setEnabled(False); self.txtRemarks.setPlaceholderText("Batch remarks…")
        self.txtRemarks.setMinimumHeight(48 if self.compact_ui else 60); self.txtRemarks.setMaximumHeight(84 if self.compact_ui else 96)
        form.addRow(header_label("Remarks:"), self.txtRemarks)
        
        self.lblMaxEC.setStyleSheet("color: #666; font-style: italic; margin-left: 2px;")
        form.addRow(QLabel(""), self.lblMaxEC)
        self.header_group.setLayout(form)

    def _apply_table_styles(self):
        self.table.setStyleSheet("""
        QTableView { border: 1px solid #d0d0d0; background-color: white; gridline-color: #cccccc; selection-background-color: #DDEEFF; selection-color: #000000; }
        QTableView::item { padding: 4px; border: none; }
        QHeaderView::section { background-color: #f0f0f0; font-weight: bold; border: 1px solid #d0d0d0; padding: 4px; }
        """)
        self.table.setShowGrid(True); self.table.setAlternatingRowColors(False)
        
    def _load_header(self) -> None:
        sql = text("SELECT RunID, StartDate, EndDate, TechnicianID, Remarks, IsLocked FROM TRIMS.PrimaryDistillationBatch WHERE RunID = :rid")
        with db_manager.get_connection() as conn:
            r = conn.execute(sql, {"rid": self.run_id}).fetchone()
        if not r: raise RuntimeError(f"Batch {self.run_id} not found")
        self.txtRunID.setText(str(r.RunID))
        if r.StartDate: self.dtStart.setDateTime(r.StartDate)
        else: self.dtStart.clear()
        if r.EndDate: self.dtEnd.setDateTime(r.EndDate)
        else: self.dtEnd.clear()
        
        self._is_locked = bool(r.IsLocked)
        
        self.txtRemarks.setText(r.Remarks or "")        
        self._set_combo_value(self.cmbTechnician, int(r.TechnicianID) if r.TechnicianID is not None else None)
        
    def _load_technicians(self) -> None:
        self.cmbTechnician.clear(); self.cmbTechnician.addItem("", None)
        with db_manager.get_connection() as conn:
            for r in conn.execute(text("SELECT EmployeeID, LastName, FirstMiddleName FROM Employee ORDER BY UPPER(LastName)")):
                name = f"{(r.LastName or '').upper()}, {r.FirstMiddleName or ''}".strip().strip(',')
                self.cmbTechnician.addItem(name, int(r.EmployeeID))
                
    def _set_combo_value(self, combo, value):
        if value is None: combo.setCurrentIndex(0); return
        idx = combo.findData(value)
        if idx >= 0: combo.setCurrentIndex(idx)
        
    def _load_global_parameters(self):
        try:
            with db_manager.get_connection() as conn:
                r = conn.execute(text("SELECT TokenValue FROM GlobalValue WHERE Token = 'MAXIMUM_EC_SMALL'")).fetchone()
            self.max_ec_small = float(r.TokenValue) if r else 0.0
            self.lblMaxEC.setText(f"Maximum Acceptable EC: {self.max_ec_small} µS/cm")
        except:
            self.max_ec_small = 0.0; self.lblMaxEC.setText("Maximum Acceptable EC: Not Set")
            
    def _load_status_options(self):
        self.status_options.clear(); self.status_map.clear()
        with db_manager.get_connection() as conn:
            sql = text("SELECT Status, Description FROM StatusLookup WHERE Status IN (2,3,-9,99) ORDER BY Status")
            for row in conn.execute(sql):
                code = int(row.Status); display = f"{code}-{row.Description}"
                self.status_options.append((code, display)); self.status_map[code] = display
        self.status_delegate = StatusDelegate(self.table, options=self.status_options, on_change=self._set_status_for_row)
        
    def _load_mode(self, mode: str) -> None:
        self.current_mode = mode
        if mode == self.MODE_PRE: self._load_ec_pre_table()
        else: self._load_ec_post_table()
        self._reset_header_colors()
        
    def _load_ec_pre_table(self) -> None:
        sql = text("""
        SELECT pd.FlaskID, pd.AnalysisID, a.SampleID, pd.ECbeforer, pd.ECafter, pd.Repeat, s.sName, pd.Remarks, pd.Status
        FROM TRIMS.PrimaryDistillation pd
        JOIN Analysis a ON a.AnalysisID = pd.AnalysisID
        JOIN Sample s ON s.SampleID = a.SampleID AND s.Prefix = a.Prefix
        WHERE pd.RunID = :rid ORDER BY pd.FlaskID
        """)
        with db_manager.get_connection() as conn:
            rows = conn.execute(sql, {"rid": self.run_id}).fetchall()
        headers = ["FlaskID", "AnalysisID", "SampleID", "EC-pre", "EC-post (Avg)", "Repeat", "Sample Name", "Remarks", "Status"]
        self.model.clear(); self.model.setHorizontalHeaderLabels(headers)
        for r in rows:
            items = [
                QStandardItem(str(r.FlaskID)), QStandardItem(str(r.AnalysisID)), QStandardItem(str(r.SampleID)),
                QStandardItem(self._fmt_float(r.ECbeforer)), QStandardItem(self._fmt_float(r.ECafter)),
                QStandardItem(str(r.Repeat)), QStandardItem(r.sName or ""), QStandardItem(r.Remarks or ""), QStandardItem(str(r.Status))
            ]
            for c, it in enumerate(items):
                flags = it.flags()
                if c in (3,7,8): it.setFlags(flags | Qt.ItemIsEditable)
                else: it.setFlags(flags & ~Qt.ItemIsEditable)
            try:
                code = int(items[8].text())
                items[8].setData(code, Qt.EditRole); items[8].setData(code, Qt.UserRole); items[8].setData(self.status_map.get(code, items[8].text()), Qt.DisplayRole)
            except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
            self.model.appendRow(items)
        self._resize_table()
        
    def _load_ec_post_table(self) -> None:
        sql = text("""
        SELECT d.FlaskID, d.SubAnalysisID, pd.AnalysisID, a.SampleID, s.sName, d.Repeat, d.ECafter, d.Remarks, d.Status, pd.ECbeforer
        FROM TRIMS.PrimaryDistillationData d
        JOIN TRIMS.PrimaryDistillation pd ON pd.ID = d.ID
        JOIN Analysis a ON a.AnalysisID = pd.AnalysisID
        JOIN Sample s ON s.SampleID = a.SampleID AND s.Prefix = a.Prefix
        WHERE pd.RunID = :rid ORDER BY d.FlaskID, d.SubAnalysisID, d.Repeat
        """)
        with db_manager.get_connection() as conn:
            rows = conn.execute(sql, {"rid": self.run_id}).fetchall()
        headers = ["FlaskID", "AnalysisID", "SampleID", "EC-pre", "EC-post", "Repeat", "Sample Name", "Remarks", "Status"]
        self.model.clear(); self.model.setHorizontalHeaderLabels(headers)
        for r in rows:
            s_item = QStandardItem(str(r.SampleID)); s_item.setData(r.SubAnalysisID, Qt.UserRole)
            items = [
                QStandardItem(str(r.FlaskID)), QStandardItem(str(r.AnalysisID)), s_item,
                QStandardItem(self._fmt_float(r.ECbeforer)), QStandardItem(self._fmt_float(r.ECafter)),
                QStandardItem(str(r.Repeat)), QStandardItem(r.sName or ""), QStandardItem(r.Remarks or ""), QStandardItem(str(r.Status))
            ]
            for c, it in enumerate(items):
                flags = it.flags()
                if c in (4,7,8): it.setFlags(flags | Qt.ItemIsEditable)
                else: it.setFlags(flags & ~Qt.ItemIsEditable)
            try:
                code = int(items[8].text())
                items[8].setData(code, Qt.EditRole); items[8].setData(code, Qt.UserRole); items[8].setData(self.status_map.get(code, items[8].text()), Qt.DisplayRole)
            except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
            self.model.appendRow(items)
        self._resize_table()
        
    def _resize_table(self):
        hdr = self.table.horizontalHeader()
        hdr.setMinimumSectionSize(75) 
        # Fix 5: Wider fixed column for Sample ID (Index 2)
        hdr.setSectionResizeMode(2, QHeaderView.Interactive)
        hdr.resizeSection(2, 120)
        for i in range(self.model.columnCount()):
            if i in (6, 7): hdr.setSectionResizeMode(i, QHeaderView.Stretch)
            elif i != 2: hdr.setSectionResizeMode(i, QHeaderView.ResizeToContents)
            
    def _set_status_for_row(self, row: int, code: int):
        if 0 <= row < self.model.rowCount():
            idx = self.model.index(row, 8)
            display = self.status_map.get(code, str(code))
            self.model.setData(idx, code, Qt.EditRole); self.model.setData(idx, display, Qt.DisplayRole); self.model.setData(idx, code, Qt.UserRole)

    def _check_privileges(self):
        try: self.has_write_priv = check_employee_privilege(get_current_user_id(), "AccessDistillation")
        except: self.has_write_priv = False
        self.setWindowTitle(f"DistillationBatch::{self.run_id} ({'RW' if self.has_write_priv else 'RO'})")
        
    def _apply_edit_state(self, editing):
        on = editing and self.has_write_priv and not self._is_locked
        self.header_group.setEnabled(True)
        for w in (self.dtStart, self.dtEnd, self.cmbTechnician, self.txtRemarks):
            w.setEnabled(on)
        self.btnEditPost.setEnabled(on)
        self.btnFinalize.setEnabled(self.has_write_priv and not self._is_locked and not editing)

    def _apply_read_only_state(self):
        self.header_group.setEnabled(True)
        for w in (self.dtStart, self.dtEnd, self.cmbTechnician, self.txtRemarks):
            w.setEnabled(False)
        self.btnEditPost.setEnabled(False)
        self.btnEditMeta.setEnabled(self.has_write_priv and not self._is_locked)
        self.btnFinalize.setEnabled(self.has_write_priv and not self._is_locked)
        self.btnEditMeta.show(); self.btnSaveMeta.hide(); self.btnCancelMeta.hide()
        self.table.setEditTriggers(QTableView.NoEditTriggers); self.table.setItemDelegate(QStyledItemDelegate()); self.table.set_editable_columns([])

    def _start_metadata_edit(self):
        if not self.has_write_priv or self._is_locked:
            return
        self.btnEditMeta.hide()
        self.btnSaveMeta.show()
        self.btnCancelMeta.show()
        self._apply_edit_state(True)
        set_status(self.status_label, "Editing distillation batch metadata...", "processing")
        self._activate_ec_pre_editing()

    def _save_metadata_edit(self):
        self._stop_edit(save=True)
        self.btnSaveMeta.hide()
        self.btnCancelMeta.hide()
        self.btnEditMeta.show()
        self._apply_edit_state(False)
        set_status(self.status_label, "Changes saved successfully.", "success")
        self.btnEditPost.setEnabled(False)
        self.btnEditPost.setChecked(False)
        self.btnEditPost.setText("Edit EC After Distillation")
        self._load_mode(self.MODE_PRE)

    def _cancel_metadata_edit(self):
        self._stop_edit(save=False)
        self._load_header()
        self._load_global_parameters()
        self._load_mode(self.MODE_PRE)
        
        self.btnSaveMeta.hide()
        self.btnCancelMeta.hide()
        self.btnEditMeta.show()
        self._apply_edit_state(False)
        set_status(self.status_label, "Edits cancelled. Reloaded from database.", "neutral")
        self.btnEditPost.setEnabled(False)
        self.btnEditPost.setChecked(False)
        self.btnEditPost.setText("Edit EC After Distillation")
    
    def _activate_ec_pre_editing(self):
        if self.current_mode != self.MODE_PRE: self._load_mode(self.MODE_PRE)
        self.table.setSelectionBehavior(QAbstractItemView.SelectItems); self.table.setEditTriggers(QTableView.AllEditTriggers)
        editable = [3, 7]; target_col = 3
        self.ec_pre_delegate.set_target_columns(editable); self.remarks_delegate.set_target_columns(editable)
        self.table.setItemDelegateForColumn(3, self.ec_pre_delegate); self.table.setItemDelegateForColumn(7, self.remarks_delegate)
        self.table.set_editable_columns(editable); self._highlight_column(target_col)

    def _on_edit_post_toggled(self, checked: bool):
        self._stop_edit(save=True)
        if checked:
            self._load_mode(self.MODE_POST); self.btnEditPost.setText("Stop EC-post Edit")
            self.table.setSelectionBehavior(QAbstractItemView.SelectItems); self.table.setEditTriggers(QTableView.AllEditTriggers)
            editable = [4, 7, 8]; target_col = 4
            self.ec_post_delegate.set_target_columns(editable); self.remarks_delegate.set_target_columns(editable); self.status_delegate.set_target_columns(editable)
            self.table.setItemDelegateForColumn(4, self.ec_post_delegate); self.table.setItemDelegateForColumn(7, self.remarks_delegate); self.table.setItemDelegateForColumn(8, self.status_delegate)
            self.table.set_editable_columns(editable); self._highlight_column(target_col)
            set_status(self.status_label, "Editing EC-post", "processing")
            if self.model.rowCount() > 0:
                start_idx = self.model.index(0, target_col); self.table.setCurrentIndex(start_idx); self.table.edit(start_idx)
        else:
            self.btnEditPost.setText("Edit EC After Distillation"); self._activate_ec_pre_editing()
            set_status(self.status_label, "Reverted to EC-pre editing", "neutral")
            
    def _stop_edit(self, save: bool = True, triggered_by_master: bool = False) -> None:
        try: self.ec_pre_delegate.force_close_active_editor()
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
        try: self.ec_post_delegate.force_close_active_editor()
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)
        try: self.remarks_delegate.force_close_active_editor()
        except Exception as _e: logging.error(f"[{__class__.__name__ if hasattr(__class__, '__name__') else ''}] {_e}", exc_info=False)

        if save and self.has_write_priv:
            try:
                self._save_header()
                if self.current_mode == self.MODE_PRE: self._save_ec_pre_changes()
                else: self._save_ec_post_changes_and_average()
                self._clear_enddate_if_incomplete()
                set_status(self.status_label, "Changes saved.", "success")
            except Exception as e:
                logging.exception("Save failed"); set_status(self.status_label, f"Save failed: {e}", "error")

        # FIX: Removed the Auto-Finalize Logic here. 
        # The user must click "Finalize Batch" explicitly. This prevents accidental locking.

        self.table.setEditTriggers(QTableView.NoEditTriggers); self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        for col in (3,4,7,8): self.table.setItemDelegateForColumn(col, QStyledItemDelegate(self.table))
        self._reset_header_colors(); self._load_mode(self.current_mode)
        
    def _finalize_batch(self) -> None:
        if not self.has_write_priv: return
        if not self.cmbTechnician.currentData():
            show_message(self, "Error", "Select Technician.", QMessageBox.Warning); return
            
        is_ok = self._check_all_distillation_ok()
        
        # Fix 1: Override Logic
        if not is_ok:
            reply = QMessageBox.question(
                self, "Incomplete Batch",
                "Some samples have Status < 3 (Incomplete/Fail).\n\n"
                "Do you want to FORCE Finalize?\n"
                "This will set all incomplete samples to '3-Success' and close the batch.",
                QMessageBox.Yes | QMessageBox.No
            )
            if reply != QMessageBox.Yes:
                return
            
            # Perform Force Update
            try:
                with db_manager.get_connection() as conn:
                    # Update incomplete PrimaryDistillation to 3
                    conn.execute(text("UPDATE TRIMS.PrimaryDistillation SET Status=3 WHERE RunID=:r AND Status < 3 AND Status <> -9"), {"r": self.run_id})
                    # Sync Analysis status using the recompute logic logic or direct update
                    # For safety, let's just trigger the recompute logic which handles Analysis sync
                    with conn.begin():
                        self._recompute_average_status_and_update(conn)
                    conn.commit()
            except Exception as e:
                show_message(self, "Error", f"Force update failed: {e}", QMessageBox.Critical)
                return

        self._do_db_finalize()
        set_status(self.status_label, "Finalized.", "success")
        self.accept()

    def _do_db_finalize(self):
        with db_manager.get_connection() as conn:
            conn.execute(text(f"UPDATE TRIMS.PrimaryDistillationBatch SET EndDate=:n, IsLocked={db_manager.sql_bool(True)} WHERE RunID=:r"),
                         {"n": datetime.now(), "r": self.run_id})
            conn.commit()
        self._is_locked = True

    def _save_header(self):
        if not self.has_write_priv: return
        
        # FIX: Check for Null End Date in UI
        end_date = self.dtEnd.dateTime().toPyDateTime()
        # If UI has a "Null" date (often represented as min date in Qt or unchecked), handle it.
        # But QDateTimeEdit usually has a valid date. 
        # Check if enabled? If disabled, we rely on the loaded value or just save what is there.
        # Ideally, if Status is ongoing, EndDate should be Null.
        
        # Simpler: Just save what is on screen. The finalization handles the "Official" EndDate.
        
        with db_manager.get_connection() as conn:
            conn.execute(text("UPDATE TRIMS.PrimaryDistillationBatch SET StartDate=:s, EndDate=:e, TechnicianID=:t, Remarks=:r WHERE RunID=:id"),
                         {"s": self.dtStart.dateTime().toPyDateTime(), "e": end_date,
                          "t": self.cmbTechnician.currentData(), "r": self.txtRemarks.toPlainText(), "id": self.run_id}); conn.commit()

    def _save_ec_pre_changes(self):
        current_db = {}
        with db_manager.get_connection() as conn:
            rows = conn.execute(text("SELECT AnalysisID, ECbeforer, Remarks, Status FROM TRIMS.PrimaryDistillation WHERE RunID=:rid"), {"rid": self.run_id}).fetchall()
            for r in rows: current_db[r.AnalysisID] = {"ec": r.ECbeforer, "rm": r.Remarks or "", "st": r.Status}
        updates = []
        for r in range(self.model.rowCount()):
            aid = int(self.model.item(r, 1).text()); ec_txt = self.model.item(r, 3).text().strip(); rm = self.model.item(r, 7).text().strip()
            st_item = self.model.item(r, 8); status_code = None
            if st_item:
                try: status_code = int(st_item.data(Qt.UserRole) or st_item.text())
                except: status_code = None
            ec = float(ec_txt) if ec_txt else None
            old = current_db.get(aid)
            if old:
                diff = False
                if ec != old["ec"]: diff = True
                elif rm != old["rm"]: diff = True
                elif status_code is not None and status_code != old["st"]: diff = True
                if diff: updates.append({"aid": aid, "ec": ec, "rm": rm, "status": status_code})
        if not updates: return
        with db_manager.get_connection() as conn:
            with conn.begin():
                for u in updates:
                    conn.execute(text("UPDATE TRIMS.PrimaryDistillation SET ECbeforer=:ec, Remarks=:rm, Status=:st WHERE AnalysisID=:aid AND RunID=:rid"), {"ec": u["ec"], "rm": u["rm"], "st": u["status"], "aid": u["aid"], "rid": self.run_id})

    def _save_ec_post_changes_and_average(self):
        current_db = {}
        with db_manager.get_connection() as conn:
            rows = conn.execute(text("SELECT d.SubAnalysisID, d.ECafter, d.Remarks, d.Status FROM TRIMS.PrimaryDistillationData d JOIN TRIMS.PrimaryDistillation pd ON pd.ID = d.ID WHERE pd.RunID = :rid"), {"rid": self.run_id}).fetchall()
            for r in rows: current_db[r.SubAnalysisID] = {"ec": r.ECafter, "rm": r.Remarks or "", "st": r.Status}
        updates = []
        for r in range(self.model.rowCount()):
            subid = self.model.item(r, 2).data(Qt.UserRole)
            if subid is None: continue
            aid = int(self.model.item(r, 1).text()); rep = int(self.model.item(r, 5).text())
            ec_txt = self.model.item(r, 4).text().strip(); rm = self.model.item(r, 7).text().strip()
            st_item = self.model.item(r, 8); status_code = None
            if st_item:
                try: status_code = int(st_item.data(Qt.UserRole) or st_item.text())
                except: status_code = None
            ec = float(ec_txt) if ec_txt else None
            old = current_db.get(subid)
            if old:
                diff = False
                if ec != old["ec"]: diff = True
                elif rm != old["rm"]: diff = True
                elif status_code is not None and status_code != old["st"]: diff = True
                if diff: updates.append({"sid": subid, "aid": aid, "rep": rep, "ec": ec, "rm": rm, "status": status_code})
        if not updates: return
        with db_manager.get_connection() as conn:
            mds = datetime.now().strftime('%Y-%m-%d %H:%M:%S'); mus = getpass.getuser()
            with conn.begin():
                for u in updates:
                    conn.execute(text("UPDATE TRIMS.PrimaryDistillationData SET ECafter=:ec, Remarks=:rm, Status=:st, ModifDateStamp=:mds, ModifUserStamp=:mus WHERE SubAnalysisID=:sid AND Repeat=:rep AND ID IN (SELECT ID FROM TRIMS.PrimaryDistillation WHERE RunID=:rid AND AnalysisID=:aid)"), {"ec": u["ec"], "rm": u["rm"], "st": u["status"], "mds": mds, "mus": mus, "sid": u["sid"], "rep": u["rep"], "rid": self.run_id, "aid": u["aid"]})
            with conn.begin(): self._recompute_average_status_and_update(conn)

    def _recompute_average_status_and_update(self, conn):
        sql_calc = text("""
            WITH LatestData AS (
                SELECT d.SubAnalysisID, MAX(d.Repeat) as MaxRep
                FROM TRIMS.PrimaryDistillationData d
                JOIN TRIMS.PrimaryDistillation pd ON pd.ID = d.ID
                WHERE pd.RunID = :rid
                GROUP BY d.SubAnalysisID
            )
            SELECT pd.AnalysisID, AVG(d.Status) as AvgStatus, AVG(d.ECafter) as AvgEC
            FROM TRIMS.PrimaryDistillationData d
            JOIN LatestData ld ON d.SubAnalysisID = ld.SubAnalysisID AND d.Repeat = ld.MaxRep
            JOIN TRIMS.PrimaryDistillation pd ON pd.ID = d.ID
            WHERE pd.RunID = :rid
            GROUP BY pd.AnalysisID
        """)
        rows = conn.execute(sql_calc, {"rid": self.run_id}).fetchall()
        for aid, avg_status, avg_ec in rows:
            if avg_status is not None:
                if avg_ec is not None and avg_ec <= self.max_ec_small:
                    final_status = 3
                elif avg_status > 9:
                    final_status = 99
                elif avg_status < 0:
                    final_status = -9
                else:
                    final_status = 99
            else:
                final_status = 2
            conn.execute(text("UPDATE TRIMS.PrimaryDistillation SET ECafter=:v, Status=:s WHERE AnalysisID=:a AND RunID=:r"), {"v": avg_ec, "s": final_status, "a": aid, "r": self.run_id})
            if final_status is not None:
                 conn.execute(text("UPDATE Analysis SET Status=:s, ModifDateStamp=:d, ModifUserStamp=:u WHERE AnalysisID=:a"), {"s": final_status, "d": datetime.now(), "u": getpass.getuser(), "a": aid})

    def _clear_enddate_if_incomplete(self):
        with db_manager.get_connection() as conn:
            cnt = conn.execute(text("SELECT COUNT(*) FROM TRIMS.PrimaryDistillationData d JOIN TRIMS.PrimaryDistillation pd ON pd.ID = d.ID WHERE pd.RunID = :r AND d.ECafter IS NULL"), {"r": self.run_id}).fetchone()[0]
            if cnt > 0: conn.execute(text("UPDATE TRIMS.PrimaryDistillationBatch SET EndDate=NULL WHERE RunID=:r"), {"r": self.run_id}); conn.commit()

    def _check_all_distillation_ok(self):
        # FIX: Query TRIMS.PrimaryDistillation (Flasks) directly to handle NULL status correctly
        with db_manager.get_connection() as conn:
            return conn.execute(text("SELECT COUNT(*) FROM TRIMS.PrimaryDistillation WHERE RunID=:r AND (Status IS NULL OR (Status < 3 AND Status <> -9))"), {"r": self.run_id}).fetchone()[0] == 0
    
    def _fmt_float(self, v):
        try: return f"{float(v):.3f}" 
        except: return ""
    def _reset_header_colors(self):
        for i in range(self.model.columnCount()): self.model.setHeaderData(i, Qt.Horizontal, QBrush(QColor("#f0f0f0")), Qt.BackgroundRole)
    def _highlight_column(self, col_idx):
        self._reset_header_colors(); self.model.setHeaderData(col_idx, Qt.Horizontal, QBrush(self.COLOR_HEADER_ACTIVE), Qt.BackgroundRole)

if __name__ == "__main__":
    from PyQt5.QtWidgets import QApplication
    app = QApplication(sys.argv)
    w = TrimsDistillationDetailsWindow(10027, compact_ui=True)
    w.show()
    sys.exit(app.exec())