"""
auto_sampler_tray_layout.py — Interactive auto-sampler tray layout widget.
Provides VialWidget and PicarroTrayWidget for visually arranging and labelling
sample vials in a configurable tray grid, with right-click context menus.
"""
import sys
import argparse
from PyQt5.QtWidgets import (
    QApplication, 
    QWidget, 
    QGridLayout, 
    QLabel,
    QMenu,
    QAction,
    QActionGroup,
    QTabWidget,
    QVBoxLayout,
    QMainWindow  # Used in the demo
)
from PyQt5.QtCore import Qt

# --- SAMPLE TYPE DEFINITIONS ---
SAMPLE_TYPES = {
    "Empty": "#E0E0E0",
    "Standard": "#AEC6CF",     # Pastel blue
    "Sample": "#C7EAC5",       # Pastel green
    "Reference": "#FFF9AE",    # Pastel yellow
    "Blank": "#FFFFFF"         # White
}

# =============================================================================
# == EMBEDDABLE COMPONENT CODE (VialWidget and PicarroTrayWidget are unchanged)
# =============================================================================

class VialWidget(QLabel):
    """
    An interactive, clickable vial icon.
    Its size and style are now set dynamically.
    """
    def __init__(self, position, tray_id, parent=None):
        super().__init__(str(position), parent)
        
        self.position = position
        self.tray_id = tray_id
        self.sample_type = "Empty"
        
        self.initUI()
        self.setDiameter(32) # Set a default starting size

    def initUI(self):
        self.setAlignment(Qt.AlignCenter)
        self.setToolTip(f"Tray {self.tray_id} | Vial {self.position}\nType: {self.sample_type}")
        
    def setDiameter(self, diameter):
        """Updates the vial's size and style based on a new diameter."""
        
        diameter = int(diameter)
        radius = int(diameter / 2)
        font_size = max(8, int(diameter / 4.5)) 
        
        self.setFixedSize(diameter, diameter)
        
        color = SAMPLE_TYPES.get(self.sample_type, "#E0E0E0")
        
        style = f"""
        QLabel {{
            background-color: {color};
            border: 1px solid #B0B0B0;
            border-radius: {radius}px;
            font-size: {font_size}px;
            font-weight: bold;
        }}
        QLabel:hover {{
            background-color: {color};
            border: 2px solid #0078D7;
        }}
        """
        self.setStyleSheet(style)

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self.showContextMenu(event.globalPos())

    def showContextMenu(self, pos):
        menu = QMenu(self)
        action_group = QActionGroup(self)
        action_group.setExclusive(True)
        
        for sample_type in SAMPLE_TYPES.keys():
            action = QAction(sample_type, self, checkable=True)
            if self.sample_type == sample_type:
                action.setChecked(True)
            menu.addAction(action)
            action_group.addAction(action)

        action_group.triggered.connect(self.assignSampleType)
        menu.exec_(pos)

    def assignSampleType(self, action):
        self.sample_type = action.text()
        self.setToolTip(f"Tray {self.tray_id} | Vial {self.position}\nType: {self.sample_type}")
        self.setDiameter(self.width())


class PicarroTrayWidget(QWidget):
    """
    A single tray grid widget. This widget now manages
    the resizing of all its child vials.
    """
    def __init__(self, tray_id, rows, cols, layout_type="AlphaNumeric", parent=None):
        super().__init__(parent)
        self.tray_id = tray_id
        self.rows = rows
        self.cols = cols
        self.layout_type = layout_type
        
        self.vials = {} 
        self.initUI()

    def initUI(self):
        self.layout = QGridLayout()
        self.layout.setSpacing(0)
        self.layout.setContentsMargins(0, 0, 0, 0)

        for r in range(self.rows):
            for c in range(self.cols):
                if self.layout_type == "AlphaNumeric":
                    row_letter = chr(ord('A') + r)
                    col_number = c + 1
                    pos_name = f"{row_letter}{col_number}"
                elif self.layout_type == "Numeric":
                    pos_name = (r * self.cols) + c + 1
                else:
                    pos_name = "?"
                
                vial = VialWidget(pos_name, self.tray_id)
                self.layout.addWidget(vial, r, c)
                self.vials[pos_name] = vial
        
        self.setLayout(self.layout)

    def resizeEvent(self, event):
        """
        Triggered when the PicarroTrayWidget is resized.
        Calculates and applies the new optimal vial size.
        """
        size = event.size()
        available_width = size.width()
        available_height = size.height()

        width_per_vial = available_width / self.cols
        height_per_vial = available_height / self.rows
        
        new_diameter = min(width_per_vial, height_per_vial)
        
        if new_diameter < 10:
            return

        for vial in self.vials.values():
            vial.setDiameter(new_diameter)
            
        super().resizeEvent(event)


class MultiTrayWidget(QWidget):
    """
    This is the main, embeddable component.
    MODIFIED to remove the close buttons from tabs.
    """
    def __init__(self, tray_configs, parent=None):
        super().__init__(parent)
        
        self.tray_id_counter = 1
        
        self.tab_widget = QTabWidget()
        
        # --- MODIFICATION: The following two lines are removed ---
        # self.tab_widget.setTabsClosable(True)
        # self.tab_widget.tabCloseRequested.connect(self.close_tab)
        # --- End of modification ---

        # High-contrast stylesheet (unchanged)
        self.tab_widget.setStyleSheet("""
            QTabBar::tab {
                /* Style for all inactive tabs */
                background: #E0E0E0;
                color: #888;
                font-weight: bold;
                padding: 10px 15px;
                border: 1px solid #C0C0C0;
                border-top-left-radius: 4px;
                border-top-right-radius: 4px;
                margin-right: 2px;
            }
            
            QTabBar::tab:hover {
                /* Slightly lighter on hover for inactive tabs */
                background: #E8E8E8;
            }
            
            QTabBar::tab:selected {
                /* Style for the ACTIVE tab */
                background: #FFFFFF; /* Bright white */
                color: #0078D7;      /* Bright blue text */
                border: 1px solid #B0B0B0;
                border-bottom-color: #FFFFFF; /* Hide bottom border */
            }
            
            QTabWidget::pane {
                /* The content area below the tabs */
                border: 1px solid #B0B0B0;
                border-top: none;
            }
        """)

        main_layout = QVBoxLayout()
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.addWidget(self.tab_widget)
        self.setLayout(main_layout)
        
        if not tray_configs:
            self.add_tray(8, 12, "AlphaNumeric")
        else:
            for rows, cols, layout_type in tray_configs:
                self.add_tray(rows, cols, layout_type)

    def add_tray(self, rows, cols, layout_type):
        tray_id = self.tray_id_counter
        self.tray_id_counter += 1
        
        tray_widget = PicarroTrayWidget(
            tray_id=tray_id, 
            rows=rows, 
            cols=cols, 
            layout_type=layout_type
        )
        
        tab_name = f"Tray {tray_id} ({rows}x{cols})"
        self.tab_widget.addTab(tray_widget, tab_name)

    def get_all_tray_data(self):
        data = {}
        for i in range(self.tab_widget.count()):
            tray_widget = self.tab_widget.widget(i)
            tab_name = self.tab_widget.tabText(i)
            
            tray_data = {}
            for pos_name, vial_widget in tray_widget.vials.items():
                tray_data[pos_name] = vial_widget.sample_type

            data[tab_name] = tray_data
        
        return data


# =============================================================================
# == EXAMPLE CALLING PROGRAM (Unchanged)
# =============================================================================

if __name__ == '__main__':
    
    # 1. --- Use argparse to parse CLI arguments ---
    parser = argparse.ArgumentParser(description="Picarro Multi-Tray Demo")
    
    parser.add_argument(
        '--tray-group', 
        action='append', 
        nargs=4, 
        metavar=('N', 'ROWS', 'COLS', 'TYPE'), 
        help='Define N identical trays. TYPE can be "alpha" or "num". '
             'Example: --tray-group 3 8 12 alpha'
    )
    
    parser.add_argument(
        '--tray', 
        action='append', 
        nargs=3, 
        metavar=('ROWS', 'COLS', 'TYPE'), 
        help='Define a single tray layout. '
             'Example: --tray 8 12 alpha'
    )
    
    args = parser.parse_args()
    
    # 2. --- Process the parsed arguments ---
    tray_configurations = []
    
    if args.tray_group:
        print("Loading tray groups from CLI arguments...")
        for n, r, c, t in args.tray_group:
            try:
                num = int(n)
                rows = int(r)
                cols = int(c)
                layout_type = "AlphaNumeric" if t.lower().startswith('a') else "Numeric"
                
                for _ in range(num):
                    tray_configurations.append((rows, cols, layout_type))
                print(f"  -> Added {num} x {rows}x{cols} ({layout_type}) trays.")
                
            except ValueError:
                print(f"Warning: Skipping invalid tray group input: {n} {r} {c} {t}")

    if args.tray:
        print("Loading single trays from CLI arguments...")
        for r, c, t in args.tray:
            try:
                rows = int(r)
                cols = int(c)
                layout_type = "AlphaNumeric" if t.lower().startswith('a') else "Numeric"
                
                tray_configurations.append((rows, cols, layout_type))
                print(f"  -> Added 1 x {rows}x{cols} ({layout_type}) tray.")
                
            except ValueError:
                print(f"Warning: Skipping invalid tray input: {r} {c} {t}")
    
    if not args.tray_group and not args.tray:
        print("No CLI arguments found. Loading default 8x12 tray.")
        tray_configurations.append((8, 12, "AlphaNumeric"))

    # 3. --- Your main application would start here ---
    app = QApplication(sys.argv)
    
    tray_component = MultiTrayWidget(tray_configurations)
    
    # --- For demonstration, put component in a main window ---
    demo_window = QMainWindow()
    demo_window.setCentralWidget(tray_component)
    demo_window.setWindowTitle("Main Application Window (Embedding Trays)")
    demo_window.resize(800, 600) 
    demo_window.show()
    
    sys.exit(app.exec())