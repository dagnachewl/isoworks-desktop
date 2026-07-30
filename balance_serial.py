"""
balance_serial.py — RS-232/USB analytical balance link for IsoWorks desktop.

Desktop equivalent of the web app's useBalanceSerial.ts (Web Serial API,
browser-only). No direct port is possible -- this uses PyQt5.QtSerialPort
instead, which is already installed and integrates with the Qt event loop
via signals (no manual threading needed).

Balance equipment and their connection settings (baud rate, data bits,
parity, stop bits) are read from the same DB tables the web app's
Settings > Equipment Management writes to (public.equipment WHERE
typeid=11, public.equipment_balance_config) -- nothing new to configure,
existing balance records apply here unchanged.
"""
from __future__ import annotations
import logging
import re
from typing import Optional, Tuple

from PyQt5.QtCore import QObject, QIODevice, QTimer, pyqtSignal
from PyQt5.QtSerialPort import QSerialPort, QSerialPortInfo

from sqlalchemy import text
from db_core import db_manager

log = logging.getLogger(__name__)

# Verified against the KERN PRS/PRJ/ARS/ARJ RS232 output spec (operating
# manual §11.2): each line is "BBB S D7 D6 D5 D4 D3 D2 D1 DP D0 B U...CR LF"
# -- three leading blanks, sign, up to 8 digits with the decimal point at a
# resolution-dependent position, a blank, then the unit. Ported verbatim
# from useBalanceSerial.ts's parseBalanceWeight() -- same two-pattern match.
_FLAG_RE = re.compile(r"\b([SD])\s+([+-]?\s*\d+(?:[ \t.,]\d+)*)\s*(?:g|mg|kg)\b", re.IGNORECASE)
_PLAIN_RE = re.compile(r"([+-]?\s*\d+(?:[ \t.,]\d+)*)\s*(?:g|mg|kg)\b", re.IGNORECASE)

_BAUD_RATES = {
    1200: QSerialPort.Baud1200, 2400: QSerialPort.Baud2400,
    4800: QSerialPort.Baud4800, 9600: QSerialPort.Baud9600,
    19200: QSerialPort.Baud19200, 38400: QSerialPort.Baud38400,
    57600: QSerialPort.Baud57600, 115200: QSerialPort.Baud115200,
}
_DATA_BITS = {5: QSerialPort.Data5, 6: QSerialPort.Data6, 7: QSerialPort.Data7, 8: QSerialPort.Data8}
_PARITY = {
    "none": QSerialPort.NoParity, "even": QSerialPort.EvenParity,
    "odd": QSerialPort.OddParity, "space": QSerialPort.SpaceParity, "mark": QSerialPort.MarkParity,
}
_STOP_BITS = {1: QSerialPort.OneStop, 2: QSerialPort.TwoStop}

# Debounce window before a stable reading is emitted -- matches
# useBalanceSerial.ts exactly. Some balances (Kern "print every value" mode)
# stream a burst of unstable readings while a weight settles; injecting on
# the first line captures a transient value. The real fix is the balance's
# own SET DATA PRINT -> STABLE mode; this is a software-side backup.
_DEBOUNCE_MS = 300


def parse_balance_weight(line: str) -> Optional[Tuple[float, bool]]:
    """Parse one line of balance output. Returns (value, stable) or None if
    the line doesn't contain a recognizable weight."""
    flag_match = _FLAG_RE.search(line)
    if flag_match:
        num_str = flag_match.group(2)
    else:
        plain_match = _PLAIN_RE.search(line)
        if not plain_match:
            return None
        num_str = plain_match.group(1)

    clean = re.sub(r"[ \t]+", "", num_str)
    if "." in clean and "," in clean:
        if clean.rfind(".") > clean.rfind(","):
            clean = clean.replace(",", "")
        else:
            clean = clean.replace(".", "").replace(",", ".")
    elif "," in clean:
        clean = clean.replace(",", ".")

    try:
        value = float(clean)
    except ValueError:
        return None

    stable = flag_match.group(1).upper() == "S" if flag_match else True
    return value, stable


def list_available_ports() -> list:
    """System serial port names (e.g. ['/dev/tty.usbserial-1410', ...])."""
    return [p.portName() for p in QSerialPortInfo.availablePorts()]


def load_balance_equipment() -> list:
    """[(equipmentid, equipmentname), ...] for balance-type equipment
    (typeid=11), same filter as web's GET /equipment?type_id=11."""
    try:
        with db_manager.get_connection() as conn:
            rows = conn.execute(text("""
                SELECT EquipmentID, EquipmentName FROM Equipment
                WHERE TypeID = 11 AND IsObsolete IS NOT TRUE
                ORDER BY EquipmentName
            """)).fetchall()
        return [(r[0], r[1]) for r in rows]
    except Exception as exc:
        log.error("load_balance_equipment: %s", exc)
        return []


def load_balance_config(equipment_id: int) -> dict:
    """Same shape/defaults as web's GET /equipment/{id}/balanceconfig."""
    defaults = {"baud_rate": 9600, "data_bits": 7, "parity": "even", "stop_bits": 1}
    try:
        with db_manager.get_connection() as conn:
            row = conn.execute(text("""
                SELECT baud_rate, data_bits, parity, stop_bits
                FROM public.equipment_balance_config
                WHERE equipmentid = :eid
            """), {"eid": equipment_id}).fetchone()
        if row:
            return {"baud_rate": row[0], "data_bits": row[1], "parity": (row[2] or "even").lower(), "stop_bits": row[3]}
        return defaults
    except Exception as exc:
        log.error("load_balance_config(%s): %s", equipment_id, exc)
        return defaults


class BalanceSerialReader(QObject):
    """
    Connects to an analytical balance over a serial port and emits a signal
    once per settled weight reading. Mirrors useBalanceSerial.ts: buffers
    incoming bytes, splits on line endings, parses each line, debounces a
    stable reading before emitting (so a settling burst can't fire early).

    Usage:
        reader = BalanceSerialReader(self)
        reader.stableReading.connect(self._on_balance_reading)
        reader.connect_to(equipment_id=14007, port_name="/dev/tty.usbserial-1410")
        ...
        reader.disconnect_port()
    """

    stableReading = pyqtSignal(float)
    rawLineReceived = pyqtSignal(str)
    statusChanged = pyqtSignal(str)
    connectionChanged = pyqtSignal(bool)

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._port: Optional[QSerialPort] = None
        self._buf = ""
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.timeout.connect(self._emit_pending)
        self._pending_value: Optional[float] = None
        self._last_stable_value: Optional[float] = None

    @property
    def connected(self) -> bool:
        return self._port is not None and self._port.isOpen()

    def connect_to(self, equipment_id: int, port_name: str) -> bool:
        self.disconnect_port()
        cfg = load_balance_config(equipment_id)

        port = QSerialPort(self)
        port.setPortName(port_name)
        port.setBaudRate(_BAUD_RATES.get(cfg["baud_rate"], QSerialPort.Baud9600))
        port.setDataBits(_DATA_BITS.get(cfg["data_bits"], QSerialPort.Data7))
        port.setParity(_PARITY.get(cfg["parity"], QSerialPort.EvenParity))
        port.setStopBits(_STOP_BITS.get(cfg["stop_bits"], QSerialPort.OneStop))
        port.setFlowControl(QSerialPort.NoFlowControl)

        if not port.open(QIODevice.ReadOnly):
            self.statusChanged.emit(f"Failed to open {port_name}: {port.errorString()}")
            return False

        self._port = port
        self._buf = ""
        port.readyRead.connect(self._on_ready_read)
        self.statusChanged.emit(f"Connected to {port_name}")
        self.connectionChanged.emit(True)
        return True

    def disconnect_port(self) -> None:
        if self._debounce_timer.isActive():
            self._debounce_timer.stop()
        self._pending_value = None
        self._last_stable_value = None
        if self._port is not None:
            try:
                self._port.readyRead.disconnect(self._on_ready_read)
            except Exception:
                pass
            if self._port.isOpen():
                self._port.close()
            self._port = None
            self.statusChanged.emit("Disconnected")
            self.connectionChanged.emit(False)

    def consume_last_stable(self) -> Optional[float]:
        """Returns and invalidates the last stable value, so it can't be
        silently reused for a different cell (mirrors consumeStableValue())."""
        v = self._last_stable_value
        self._last_stable_value = None
        return v

    def _on_ready_read(self) -> None:
        if self._port is None:
            return
        data = bytes(self._port.readAll()).decode("utf-8", errors="replace")
        self._buf += data
        *lines, self._buf = re.split(r"\r\n|\r|\n", self._buf)
        for line in lines:
            if not line.strip():
                continue
            self.rawLineReceived.emit(line)
            reading = parse_balance_weight(line)
            if reading is None:
                continue
            value, stable = reading
            if stable:
                self._pending_value = value
                self._debounce_timer.start(_DEBOUNCE_MS)
                self.statusChanged.emit(f"{value:.4f} g")
            else:
                if self._debounce_timer.isActive():
                    self._debounce_timer.stop()
                self._pending_value = None
                self.statusChanged.emit(f"{value:.4f} g (settling…)")

    def _emit_pending(self) -> None:
        if self._pending_value is None:
            return
        self._last_stable_value = self._pending_value
        self.stableReading.emit(self._pending_value)
