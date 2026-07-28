"""
sample_label_gui.py — QR-code sample label designer and printer for IsoWorks.

Two label types
  Registration  → QR: SUB=...|SID=...|NAME=...
  Analysis Job  → QR: SID=...|AID=...|CTN=...

Usage — manual entry:
    dlg = SampleLabelDialog(parent=self)
    dlg.exec_()

Usage — programmatic batch:
    specs = [analysis_spec(pfx, sid, aid, ctn, "Distillation") for each flask]
    dlg = SampleLabelDialog(specs=specs, parent=self)
    dlg.exec_()

Convenience class-methods:
    SampleLabelDialog.show_registration(sub_id, pfx, sid, name, parent)
    SampleLabelDialog.show_analysis(pfx, sid, aid, ctn, job_type, parent)
    SampleLabelDialog.show_batch(specs, parent)
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass
from typing import List, Optional, Tuple

from PyQt5.QtCore import Qt, QRectF, QSizeF, QPointF
from PyQt5.QtGui import QPainter, QPixmap, QFont, QColor, QPen
from PyQt5.QtPrintSupport import QPrinter, QPrintDialog
from PyQt5.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout, QTabWidget,
    QLabel, QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox,
    QPushButton, QWidget, QGroupBox, QSizePolicy, QFileDialog, QMessageBox,
)

log = logging.getLogger(__name__)

try:
    import qrcode
    import qrcode.constants
    HAS_QR = True
except ImportError:
    HAS_QR = False
    log.warning("qrcode not installed — QR codes disabled. "
                "Install with: pip install 'qrcode[pil]'")


# ── Data model ────────────────────────────────────────────────────────────────

@dataclass
class LabelSpec:
    """All data needed to render one label."""
    label_type: str                  # 'registration' | 'analysis'
    qr_text:    str                  # string encoded into QR symbol
    header:     str                  # small header line, e.g. "REGISTRATION"
    lines:      List[Tuple[str, str]]  # [(field_label, value), ...]
    copies:     int = 1


def registration_spec(
    submission_id: str,
    prefix: str,
    sample_id: str,
    sample_name: str,
    copies: int = 1,
) -> LabelSpec:
    sid = f"{prefix}-{sample_id}".strip("-")
    return LabelSpec(
        label_type="registration",
        qr_text=f"SUB={submission_id}|SID={sid}|NAME={sample_name}",
        header="REGISTRATION",
        lines=[
            ("Sample ID",  sid),
            ("Submission", str(submission_id)),
            ("Name",       sample_name),
        ],
        copies=copies,
    )


def analysis_spec(
    prefix: str,
    sample_id: str,
    analysis_id: str,
    container_num: str,
    job_type: str = "Analysis",
    copies: int = 1,
    sample_name: str = "",
) -> LabelSpec:
    sid = f"{prefix}-{sample_id}".strip("-")
    qr = f"SID={sid}|AID={analysis_id}|CTN={container_num}"
    if sample_name:
        qr += f"|NAME={sample_name}"
    return LabelSpec(
        label_type="analysis",
        qr_text=qr,
        header=job_type.upper(),
        lines=[
            ("Sample ID",   sid),
            ("Analysis ID", str(analysis_id)),
            ("Container",   str(container_num)),
        ],
        copies=copies,
    )


# ── QR pixmap ─────────────────────────────────────────────────────────────────

def _make_qr_pixmap(text: str, size_px: int) -> Optional[QPixmap]:
    if not HAS_QR:
        return None
    try:
        qr = qrcode.QRCode(
            version=None,
            error_correction=qrcode.constants.ERROR_CORRECT_M,
            box_size=10,
            border=1,
        )
        qr.add_data(text)
        qr.make(fit=True)
        buf = io.BytesIO()
        qr.make_image(fill_color="black", back_color="white").save(buf, format="PNG")
        pm = QPixmap()
        pm.loadFromData(buf.getvalue())
        return pm.scaled(size_px, size_px, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    except Exception as exc:
        log.warning("QR generation failed: %s", exc)
        return None


# ── Renderer ──────────────────────────────────────────────────────────────────

_C_HEADER = QColor("#37474F")
_C_LABEL  = QColor("#78909C")
_C_VALUE  = QColor("#212121")
_C_BORDER = QColor("#90A4AE")
_C_SEP    = QColor("#CFD8DC")
_C_BG     = QColor("#FFFFFF")


class LabelRenderer:
    """
    Renders a LabelSpec onto any QPainter in a rectangle (0,0,w_px,h_px).

    Font sizes are derived from h_mm (physical millimetres) so the layout
    looks correct at both screen preview DPI and high-res printer DPI.
    """

    def render(
        self,
        painter: QPainter,
        w_px: float,
        h_px: float,
        w_mm: float,
        h_mm: float,
        spec: LabelSpec,
        show_qr: bool = True,
    ) -> None:
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.TextAntialiasing)

        # ── background + rounded border ──
        painter.fillRect(0, 0, int(w_px), int(h_px), _C_BG)
        painter.setPen(QPen(_C_BORDER, max(1.0, h_px * 0.012)))
        painter.drawRoundedRect(
            QRectF(0.5, 0.5, w_px - 1, h_px - 1),
            h_px * 0.05, h_px * 0.05,
        )

        # ── layout geometry ──
        m = h_px * 0.07
        if show_qr:
            qr_sz = int(h_px * 0.86)
            txt_w = w_px - qr_sz - m * 3.2
        else:
            qr_sz = 0
            txt_w = w_px - m * 2.0

        # ── QR code (right edge, labels only) ──
        if show_qr:
            qr_x = int(w_px - qr_sz - m)
            qr_y = int((h_px - qr_sz) / 2)
            qr_pm = _make_qr_pixmap(spec.qr_text, qr_sz)
            if qr_pm:
                painter.drawPixmap(qr_x, qr_y, qr_pm)
            else:
                painter.setPen(QPen(_C_BORDER, 1, Qt.DashLine))
                painter.drawRect(qr_x, qr_y, qr_sz, qr_sz)
                painter.setFont(QFont("Arial", max(5, int(h_mm * 0.20))))
                painter.setPen(QPen(_C_LABEL))
                painter.drawText(
                    QRectF(qr_x, qr_y, qr_sz, qr_sz), Qt.AlignCenter,
                    "QR\n(install\nqrcode[pil])",
                )

        # ── font sizes from physical mm — cast to int (QFont requires int) ──
        hdr_pt  = max(5, int(h_mm * 0.25))   # ~7pt at 28 mm
        lbl_pt  = max(4, int(h_mm * 0.19))   # ~5pt
        val0_pt = max(7, int(h_mm * 0.40))   # ~11pt — first / main value
        val_pt  = max(5, int(h_mm * 0.30))   # ~8pt  — subsequent values

        # ── header ──
        hdr_h = h_px * 0.23
        hdr_font = QFont("Arial", hdr_pt, QFont.Bold)
        hdr_font.setLetterSpacing(QFont.AbsoluteSpacing, max(0.3, hdr_pt * 0.07))
        painter.setFont(hdr_font)
        painter.setPen(QPen(_C_HEADER))
        painter.drawText(
            QRectF(m, m * 0.35, txt_w, hdr_h),
            Qt.AlignLeft | Qt.AlignVCenter,
            spec.header,
        )

        # ── thin separator ──
        sep_y = hdr_h + m * 0.55
        painter.setPen(QPen(_C_SEP, max(0.5, h_px * 0.008)))
        painter.drawLine(
            QPointF(m, sep_y),
            QPointF(m + txt_w * 0.88, sep_y),
        )

        # ── field rows ──
        n       = len(spec.lines)
        avail_h = h_px - sep_y - m * 0.5
        row_h   = avail_h / max(n, 1)

        for i, (lbl_text, val_text) in enumerate(spec.lines):
            y    = sep_y + m * 0.15 + i * row_h
            fpt  = val0_pt if i == 0 else val_pt

            # field label — small, muted
            painter.setFont(QFont("Arial", lbl_pt))
            painter.setPen(QPen(_C_LABEL))
            painter.drawText(
                QRectF(m, y, txt_w, row_h * 0.40),
                Qt.AlignLeft | Qt.AlignBottom,
                lbl_text + ":",
            )

            # value — elide if overflows
            vf = QFont("Arial", fpt, QFont.Bold if i == 0 else QFont.Normal)
            painter.setFont(vf)
            painter.setPen(QPen(_C_VALUE))
            elided = painter.fontMetrics().elidedText(
                val_text, Qt.ElideRight, int(txt_w)
            )
            painter.drawText(
                QRectF(m, y + row_h * 0.40, txt_w, row_h * 0.56),
                Qt.AlignLeft | Qt.AlignTop,
                elided,
            )


_renderer = LabelRenderer()


# ── Preview widget ────────────────────────────────────────────────────────────

class _LabelPreview(QWidget):
    """Draws a LabelSpec centred and scaled to fit the widget."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._spec: Optional[LabelSpec] = None
        self._w_mm = 89.0
        self._h_mm = 28.0
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumSize(320, 110)
        self.setStyleSheet("background:#ECEFF1;")

    def set_spec(self, spec: LabelSpec, w_mm: float, h_mm: float) -> None:
        self._spec  = spec
        self._w_mm  = w_mm
        self._h_mm  = h_mm
        self.update()

    def paintEvent(self, _event):
        if not self._spec:
            return
        avail_w = self.width()  - 24
        avail_h = self.height() - 24
        aspect  = self._w_mm / self._h_mm
        if avail_w / avail_h > aspect:
            h = avail_h
            w = int(h * aspect)
        else:
            w = avail_w
            h = int(w / aspect)

        x = (self.width()  - w) // 2
        y = (self.height() - h) // 2

        painter = QPainter(self)
        try:
            painter.translate(x, y)
            _renderer.render(painter, w, h, self._w_mm, self._h_mm, self._spec)
        finally:
            painter.end()


# ── Main dialog ───────────────────────────────────────────────────────────────

_JOB_TYPES = [
    "Distillation", "Electrolysis", "Chemical Enrichment",
    "LSC", "SIAM", "NGAM", "Analysis",
]

_DEFAULT_W_MM = 89.0
_DEFAULT_H_MM = 28.0


class SampleLabelDialog(QDialog):
    """
    Label designer / printer.

    Operates in two modes:
      • form mode  — user fills fields, instant preview, prints one label
      • batch mode — called with pre-built LabelSpec list; shows prev/next
                     navigation and prints all labels in one job
    """

    def __init__(
        self,
        specs: Optional[List[LabelSpec]] = None,
        parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Sample Label Printer — IsoWorks")
        self.resize(740, 430)
        self._batch_specs = list(specs) if specs else []
        self._batch_idx   = 0
        self._batch_mode  = bool(self._batch_specs)
        self._build_ui()
        if self._batch_mode:
            self._show_batch_label()
        else:
            self._update_preview()

    # ── UI ────────────────────────────────────────────────────────────────────

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(8)

        body = QHBoxLayout()
        body.setSpacing(12)
        body.addWidget(
            self._build_batch_nav() if self._batch_mode else self._build_form(),
            0,
        )

        right = QVBoxLayout()
        self._preview = _LabelPreview()
        right.addWidget(self._preview, 1)
        self._dim_lbl = QLabel()
        self._dim_lbl.setAlignment(Qt.AlignCenter)
        self._dim_lbl.setStyleSheet("color:#78909C;font-size:9pt;margin-top:2px;")
        right.addWidget(self._dim_lbl)
        body.addLayout(right, 1)

        root.addLayout(body, 1)
        root.addWidget(self._build_bottom_bar())
        self._refresh_dim_label()

    # ── form (manual entry) ───────────────────────────────────────────────────

    def _build_form(self) -> QWidget:
        box = QGroupBox("Label Content")
        lay = QVBoxLayout(box)
        self._tabs = QTabWidget()
        self._tabs.addTab(self._build_registration_tab(), "Registration")
        self._tabs.addTab(self._build_analysis_tab(),     "Analysis Job")
        self._tabs.currentChanged.connect(self._update_preview)
        lay.addWidget(self._tabs)
        return box

    def _build_registration_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        f.setSpacing(7)
        self._reg_sub  = QLineEdit(); self._reg_sub.setPlaceholderText("e.g. SUB-2024-001")
        self._reg_pfx  = QLineEdit(); self._reg_pfx.setPlaceholderText("e.g. ETH")
        self._reg_sid  = QLineEdit(); self._reg_sid.setPlaceholderText("e.g. 001234")
        self._reg_name = QLineEdit(); self._reg_name.setPlaceholderText("Descriptive sample name")
        for le in (self._reg_sub, self._reg_pfx, self._reg_sid, self._reg_name):
            le.textChanged.connect(self._update_preview)
        f.addRow("Submission ID:", self._reg_sub)
        f.addRow("Prefix:",        self._reg_pfx)
        f.addRow("Sample ID:",     self._reg_sid)
        f.addRow("Sample Name:",   self._reg_name)
        return w

    def _build_analysis_tab(self) -> QWidget:
        w = QWidget()
        f = QFormLayout(w)
        f.setSpacing(7)
        self._ana_pfx = QLineEdit(); self._ana_pfx.setPlaceholderText("e.g. ETH")
        self._ana_sid = QLineEdit(); self._ana_sid.setPlaceholderText("e.g. 001234")
        self._ana_aid = QLineEdit(); self._ana_aid.setPlaceholderText("e.g. A-5678")
        self._ana_ctn = QLineEdit(); self._ana_ctn.setPlaceholderText("e.g. Flask 3, Vial A")
        self._ana_job = QComboBox(); self._ana_job.addItems(_JOB_TYPES)
        for le in (self._ana_pfx, self._ana_sid, self._ana_aid, self._ana_ctn):
            le.textChanged.connect(self._update_preview)
        self._ana_job.currentTextChanged.connect(self._update_preview)
        f.addRow("Prefix:",      self._ana_pfx)
        f.addRow("Sample ID:",   self._ana_sid)
        f.addRow("Analysis ID:", self._ana_aid)
        f.addRow("Container:",   self._ana_ctn)
        f.addRow("Job Type:",    self._ana_job)
        return w

    # ── batch navigation panel ────────────────────────────────────────────────

    def _build_batch_nav(self) -> QWidget:
        n = len(self._batch_specs)
        box = QGroupBox(f"Labels — {n} total")
        lay = QVBoxLayout(box)

        nav = QHBoxLayout()
        self._btn_prev = QPushButton("◀ Prev")
        self._btn_next = QPushButton("Next ▶")
        self._nav_lbl  = QLabel()
        self._nav_lbl.setAlignment(Qt.AlignCenter)
        self._btn_prev.clicked.connect(self._batch_prev)
        self._btn_next.clicked.connect(self._batch_next)
        nav.addWidget(self._btn_prev)
        nav.addWidget(self._nav_lbl, 1)
        nav.addWidget(self._btn_next)
        lay.addLayout(nav)

        lay.addWidget(QLabel("QR content:"))
        self._qr_preview = QLabel()
        self._qr_preview.setWordWrap(True)
        self._qr_preview.setStyleSheet(
            "background:#F5F5F5;border:1px solid #DADADA;padding:6px;"
            "font-family:monospace;font-size:8pt;color:#546E7A;"
        )
        lay.addWidget(self._qr_preview)
        lay.addStretch()
        return box

    # ── bottom bar ────────────────────────────────────────────────────────────

    def _build_bottom_bar(self) -> QWidget:
        bar = QWidget()
        bar.setStyleSheet("background:#ECEFF1;border-top:1px solid #B0BEC5;")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(6)

        # Label size (always shown)
        lay.addWidget(QLabel("W:"))
        self._spin_w = QDoubleSpinBox()
        self._spin_w.setRange(20, 250); self._spin_w.setValue(_DEFAULT_W_MM)
        self._spin_w.setSuffix(" mm"); self._spin_w.setDecimals(1)
        self._spin_w.valueChanged.connect(self._update_preview)
        lay.addWidget(self._spin_w)

        lay.addWidget(QLabel("H:"))
        self._spin_h = QDoubleSpinBox()
        self._spin_h.setRange(10, 120); self._spin_h.setValue(_DEFAULT_H_MM)
        self._spin_h.setSuffix(" mm"); self._spin_h.setDecimals(1)
        self._spin_h.valueChanged.connect(self._update_preview)
        lay.addWidget(self._spin_h)

        lay.addSpacing(12)
        lay.addWidget(QLabel("Copies:"))
        self._spin_copies = QSpinBox()
        self._spin_copies.setRange(1, 99); self._spin_copies.setValue(1)
        lay.addWidget(self._spin_copies)

        lay.addStretch()

        btn_pdf   = QPushButton("Export PDF")
        btn_pdf.clicked.connect(self._export_pdf)

        btn_print = QPushButton("  Print  ")
        btn_print.setDefault(True)
        btn_print.clicked.connect(self._do_print)
        btn_print.setStyleSheet(
            "QPushButton{background:#37474F;color:white;border-radius:3px;"
            "padding:4px 18px;font-weight:bold;}"
            "QPushButton:hover{background:#546E7A;}"
        )

        btn_close = QPushButton("Close")
        btn_close.clicked.connect(self.reject)

        for b in (btn_pdf, btn_print, btn_close):
            lay.addWidget(b)

        return bar

    # ── helpers ───────────────────────────────────────────────────────────────

    def _w_mm(self) -> float: return self._spin_w.value()
    def _h_mm(self) -> float: return self._spin_h.value()

    def _current_spec(self) -> Optional[LabelSpec]:
        if self._batch_mode:
            return self._batch_specs[self._batch_idx] if self._batch_specs else None
        if self._tabs.currentIndex() == 0:
            return registration_spec(
                self._reg_sub.text().strip() or "—",
                self._reg_pfx.text().strip(),
                self._reg_sid.text().strip() or "—",
                self._reg_name.text().strip() or "—",
            )
        return analysis_spec(
            self._ana_pfx.text().strip(),
            self._ana_sid.text().strip() or "—",
            self._ana_aid.text().strip() or "—",
            self._ana_ctn.text().strip() or "—",
            self._ana_job.currentText(),
        )

    def _update_preview(self):
        spec = self._current_spec()
        if spec:
            self._preview.set_spec(spec, self._w_mm(), self._h_mm())
        self._refresh_dim_label()

    def _refresh_dim_label(self):
        self._dim_lbl.setText(f"{self._w_mm():.1f} × {self._h_mm():.1f} mm")

    def _show_batch_label(self):
        n    = len(self._batch_specs)
        spec = self._batch_specs[self._batch_idx]
        self._preview.set_spec(spec, self._w_mm(), self._h_mm())
        self._nav_lbl.setText(f"{self._batch_idx + 1} / {n}")
        self._qr_preview.setText(spec.qr_text)
        self._btn_prev.setEnabled(self._batch_idx > 0)
        self._btn_next.setEnabled(self._batch_idx < n - 1)
        self._refresh_dim_label()

    def _batch_prev(self):
        if self._batch_idx > 0:
            self._batch_idx -= 1
            self._show_batch_label()

    def _batch_next(self):
        if self._batch_idx < len(self._batch_specs) - 1:
            self._batch_idx += 1
            self._show_batch_label()

    # ── print / export ────────────────────────────────────────────────────────

    def _specs_to_print(self) -> List[LabelSpec]:
        if self._batch_mode:
            specs = list(self._batch_specs)
        else:
            spec = self._current_spec()
            specs = [spec] if spec else []
        copies = self._spin_copies.value()
        for s in specs:
            s = LabelSpec(s.label_type, s.qr_text, s.header, s.lines, copies)
        return specs

    def _apply_page(self, printer: QPrinter) -> None:
        printer.setPaperSize(QSizeF(self._w_mm(), self._h_mm()), QPrinter.Millimeter)
        printer.setPageMargins(0, 0, 0, 0, QPrinter.Millimeter)

    def _paint_all(self, printer: QPrinter, specs: List[LabelSpec]) -> None:
        dpi      = printer.resolution()
        px_per_mm = dpi / 25.4
        w_mm, h_mm = self._w_mm(), self._h_mm()
        w_px     = w_mm * px_per_mm
        h_px     = h_mm * px_per_mm

        painter = QPainter(printer)
        try:
            first = True
            for spec in specs:
                copies = self._spin_copies.value()
                for _ in range(copies):
                    if not first:
                        printer.newPage()
                    first = False
                    _renderer.render(painter, w_px, h_px, w_mm, h_mm, spec)
        finally:
            painter.end()

    def _do_print(self):
        specs = self._specs_to_print()
        if not specs:
            QMessageBox.warning(self, "Nothing to print", "Fill in at least one field.")
            return
        printer = QPrinter(QPrinter.HighResolution)
        dlg = QPrintDialog(printer, self)
        if dlg.exec_() != QPrintDialog.Accepted:
            return
        self._apply_page(printer)
        self._paint_all(printer, specs)

    def _export_pdf(self):
        specs = self._specs_to_print()
        if not specs:
            QMessageBox.warning(self, "Nothing to export", "Fill in at least one field.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export Labels as PDF", "labels.pdf",
            "PDF Files (*.pdf);;All Files (*)",
        )
        if not path:
            return
        printer = QPrinter(QPrinter.HighResolution)
        printer.setOutputFormat(QPrinter.PdfFormat)
        printer.setOutputFileName(path)
        self._apply_page(printer)
        self._paint_all(printer, specs)
        QMessageBox.information(self, "Export Complete", f"Labels saved to:\n{path}")

    # ── class-method constructors ─────────────────────────────────────────────

    @classmethod
    def show_registration(
        cls,
        submission_id: str,
        prefix: str,
        sample_id: str,
        sample_name: str,
        parent=None,
    ) -> "SampleLabelDialog":
        return cls(
            specs=[registration_spec(submission_id, prefix, sample_id, sample_name)],
            parent=parent,
        )

    @classmethod
    def show_analysis(
        cls,
        prefix: str,
        sample_id: str,
        analysis_id: str,
        container_num: str,
        job_type: str = "Analysis",
        parent=None,
    ) -> "SampleLabelDialog":
        return cls(
            specs=[analysis_spec(prefix, sample_id, analysis_id, container_num, job_type)],
            parent=parent,
        )

    @classmethod
    def show_batch(
        cls,
        specs: List[LabelSpec],
        parent=None,
    ) -> "SampleLabelDialog":
        return cls(specs=specs, parent=parent)
