"""
animated_sidebar.py — Animated collapsible sidebar widget for the IsoWorks application.
Provides AnimatedSidebar, a QWidget that slides between expanded and collapsed states
and emits signals when a navigation module or the settings button is selected.
"""
# animated_sidebar.py
from __future__ import annotations
import logging
from functools import partial
from typing import Optional

from PyQt5.QtCore import (
    Qt, QSize, QPoint, QPropertyAnimation, 
    pyqtProperty, QEasingCurve, pyqtSignal
)
from PyQt5.QtGui import QIcon, QFont
from PyQt5.QtWidgets import (
    QWidget, QStackedWidget, QHBoxLayout, QVBoxLayout, 
    QPushButton, QTreeWidget, QTreeWidgetItem, 
    QTreeWidgetItemIterator
)

from shared_utils import IconCache, ModuleSpec

class AnimatedSidebar(QWidget):
    """
    A standalone, animated sidebar widget.
    
    Emits:
        module_selected (str): When an item (tree or icon) is clicked.
        settings_requested (QPoint): When the settings button is clicked,
                                     emitting the button's global position.
    """
    
    module_selected = pyqtSignal(str)
    settings_requested = pyqtSignal(QPoint)
    
    SIDEBAR_WIDTH_EXPANDED = 240
    SIDEBAR_WIDTH_COLLAPSED = 60

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.is_panel_collapsed = False # Use "panel" to refer to the slide-out part
        self.icon_buttons = {} 

        self._setup_ui()
        self._setup_animation()
        self._connect_signals()
        self._apply_stylesheet()
        
    def _setup_ui(self):
        self.main_layout = QHBoxLayout(self)
        self.main_layout.setContentsMargins(0, 0, 0, 0)
        self.main_layout.setSpacing(0)
        self.setObjectName("SidebarContainer")

        # --- 1. Icon Bar (Permanent) ---
        self.icon_bar_container = QWidget()
        self.icon_bar_container.setFixedWidth(self.SIDEBAR_WIDTH_COLLAPSED)
        self.icon_bar_container.setObjectName("IconBarContainer")
        icon_bar_layout = QVBoxLayout(self.icon_bar_container)
        icon_bar_layout.setContentsMargins(5, 10, 5, 10) 
        icon_bar_layout.setSpacing(5)

        self.toggle_sidebar_button = QPushButton()
        self.toggle_sidebar_button.setIcon(IconCache.get_icon("menu"))
        self.toggle_sidebar_button.setIconSize(QSize(24, 24))
        self.toggle_sidebar_button.setFixedSize(QSize(40, 40))
        self.toggle_sidebar_button.setObjectName("ToggleSidebarButton")
        self.toggle_sidebar_button.setToolTip("Toggle Navigation")
        icon_bar_layout.addWidget(self.toggle_sidebar_button)

        # --- MODIFIED: Create Icon Stack for Goal 2 ---
        self.icon_stack = QStackedWidget()
        
        # Widget 0: Empty (shown when panel is expanded)
        self.empty_widget = QWidget()
        self.icon_stack.addWidget(self.empty_widget)
        
        # Widget 1: Real icons (shown when panel is collapsed)
        self.sidebar_icons = QWidget()
        self.sidebar_icons.setObjectName("SidebarIcons") 
        self.sidebar_icons_layout = QVBoxLayout(self.sidebar_icons)
        self.sidebar_icons_layout.setContentsMargins(0, 0, 0, 0)
        self.sidebar_icons_layout.setSpacing(5)
        self.sidebar_icons_layout.setAlignment(Qt.AlignTop)
        self.icon_stack.addWidget(self.sidebar_icons)
        
        # Add the stack and set it to blank
        icon_bar_layout.addWidget(self.icon_stack, 1) # 1 = stretch
        self.icon_stack.setCurrentIndex(0) # Start blank
        # --- END MODIFICATION ---

        # --- MODIFIED: Create Icon-Only Settings Button for Goal 1 ---
        self.settings_button_icon = QPushButton()
        self.settings_button_icon.setIcon(IconCache.get_icon("settings"))
        self.settings_button_icon.setIconSize(QSize(24, 24))
        self.settings_button_icon.setFixedSize(QSize(40, 40))
        self.settings_button_icon.setToolTip("Configure application and database settings")
        self.settings_button_icon.setObjectName("SettingsButtonIcon")
        
        icon_bar_layout.addWidget(self.settings_button_icon, 0)
        self.main_layout.addWidget(self.icon_bar_container)
        # --- END MODIFICATION ---

        # --- 2. Nav Panel (Animated) ---
        self.nav_panel_container = QWidget()
        self.nav_panel_container.setMinimumWidth(0)
        self.nav_panel_container.setMaximumWidth(self.SIDEBAR_WIDTH_EXPANDED)
        self.nav_panel_container.setObjectName("NavPanelContainer")
        nav_panel_layout = QVBoxLayout(self.nav_panel_container)
        nav_panel_layout.setContentsMargins(0, 0, 0, 0)
        nav_panel_layout.setSpacing(0)

        # --- NEW: Add alignment spacer ---
        # Height = icon_bar top margin (10) + toggle_button height (40) + layout spacing (5)
        dummy_header = QWidget()
        dummy_header.setFixedHeight(55)
        dummy_header.setObjectName("NavPanelHeaderSpacer")
        nav_panel_layout.addWidget(dummy_header)
        # --- END NEW ---

        self.sidebar_tree = QTreeWidget()
        self.sidebar_tree.setHeaderHidden(True)
        self.sidebar_tree.setObjectName("SidebarTree")
        self.sidebar_tree.setIconSize(QSize(24, 24))
        nav_panel_layout.addWidget(self.sidebar_tree, 1) # 1 = stretch

        # --- MODIFIED: Create Text Settings Button for Goal 1 ---
        self.settings_button_text = QPushButton(" Settings")
        self.settings_button_text.setIcon(IconCache.get_icon("settings"))
        self.settings_button_text.setIconSize(QSize(24, 24))
        self.settings_button_text.setToolTip("Configure application and database settings")
        self.settings_button_text.setObjectName("SettingsButtonText")
        nav_panel_layout.addWidget(self.settings_button_text, 0) # 0 = no stretch
        # --- END MODIFICATION ---

        self.main_layout.addWidget(self.nav_panel_container)

    def _setup_animation(self):
        self.panel_animation = QPropertyAnimation(self.nav_panel_container, b"maximumWidth")
        self.panel_animation.setDuration(250)
        self.panel_animation.setEasingCurve(QEasingCurve.InOutQuad)
        self.panel_animation.finished.connect(self._on_panel_animation_finished)

    def _connect_signals(self):
        self.toggle_sidebar_button.clicked.connect(self.toggle_nav_panel)
        self.sidebar_tree.currentItemChanged.connect(self._on_tree_module_changed)
        
        # --- MODIFIED: Connect both settings buttons ---
        self.settings_button_icon.clicked.connect(self._on_settings_button_clicked_icon)
        self.settings_button_text.clicked.connect(self._on_settings_button_clicked_text)
        
    def populate(self, modules: list[ModuleSpec]):
        # (Unchanged)
        self.sidebar_tree.blockSignals(True)
        self.sidebar_tree.clear()
        
        while self.sidebar_icons_layout.count():
            child = self.sidebar_icons_layout.takeAt(0)
            if child.widget():
                child.widget().deleteLater()
        
        self.icon_buttons.clear()
        
        font = QFont()
        font.setBold(True)
        font.setPointSize(11)

        for mod in modules:
            if mod.children:
                parent_item = QTreeWidgetItem(self.sidebar_tree, [mod.title])
                parent_item.setIcon(0, mod.icon or QIcon())
                parent_item.setFont(0, font)
                parent_item.setData(0, Qt.UserRole, mod.key)
                parent_item.setFlags(parent_item.flags() & ~Qt.ItemIsSelectable)
                parent_item.setData(0, Qt.UserRole + 1, mod.title)
                
                for child in mod.children:
                    child_item = QTreeWidgetItem(parent_item, [child.title])
                    child_item.setIcon(0, child.icon or QIcon())
                    child_item.setData(0, Qt.UserRole, child.key)
                    child_item.setData(0, Qt.UserRole + 1, child.title)
                    self._add_icon_button(child)
                    
            else: # Top-level item
                item = QTreeWidgetItem(self.sidebar_tree, [mod.title])
                item.setIcon(0, mod.icon or QIcon())
                item.setData(0, Qt.UserRole, mod.key)
                item.setData(0, Qt.UserRole + 1, mod.title)
                self._add_icon_button(mod)
        
        self.sidebar_tree.expandAll()
        self.sidebar_tree.blockSignals(False)

    def _add_icon_button(self, mod: ModuleSpec):
        # (Unchanged)
        if mod.key in self.icon_buttons: return

        button = QPushButton()
        button.setIcon(mod.icon or QIcon())
        button.setIconSize(QSize(24, 24))
        button.setFixedSize(QSize(40, 40))
        button.setToolTip(mod.title)
        button.setObjectName("SidebarIconButton")
        button.setCheckable(True)
        button.clicked.connect(partial(self._on_icon_button_clicked, mod.key))
        
        self.sidebar_icons_layout.addWidget(button)
        self.icon_buttons[mod.key] = button

    def set_current_module(self, key: str):
        # (Unchanged)
        for btn_key, button in self.icon_buttons.items():
            button.setChecked(btn_key == key)
            
        self.sidebar_tree.blockSignals(True)
        iterator = QTreeWidgetItemIterator(self.sidebar_tree)
        item_found = False
        while iterator.value():
            item = iterator.value()
            if item.data(0, Qt.UserRole) == key:
                self.sidebar_tree.setCurrentItem(item)
                item_found = True
                break
            iterator += 1
        
        if not item_found:
            self.sidebar_tree.clearSelection()
        self.sidebar_tree.blockSignals(False)

    # --- Slots and Animation ---

    def _on_tree_module_changed(self, item: QTreeWidgetItem, previous_item: QTreeWidgetItem):
        # (Unchanged)
        if item is None: return
        key = item.data(0, Qt.UserRole);
        if not key: return
        self.module_selected.emit(key)
        if not self.is_panel_collapsed and item.childCount() == 0:
            self.toggle_nav_panel()

    def _on_icon_button_clicked(self, key: str):
        # (Unchanged)
        self.module_selected.emit(key)
        if not self.is_panel_collapsed:
            self.toggle_nav_panel()

    # --- NEW: Separate slots for each settings button ---
    def _on_settings_button_clicked_icon(self):
        global_pos = self.settings_button_icon.mapToGlobal(QPoint(0, 0))
        self.settings_requested.emit(global_pos)

    def _on_settings_button_clicked_text(self):
        global_pos = self.settings_button_text.mapToGlobal(QPoint(0, 0))
        self.settings_requested.emit(global_pos)
    # --- END NEW ---

    def toggle_nav_panel(self):
        self.panel_animation.stop()
        
        if self.is_panel_collapsed:
            # --- EXPAND ---
            self.is_panel_collapsed = False
            self.nav_panel_container.setVisible(True)
            # --- MODIFIED: Show blank icon panel ---
            self.icon_stack.setCurrentIndex(0)
            
            self.setProperty("collapsed", False)
            self.settings_button_icon.setProperty("collapsed", False)
            self.toggle_sidebar_button.setProperty("collapsed", False)
            self._style_sidebar_widgets()
            
            target_width = self.SIDEBAR_WIDTH_EXPANDED
            self.panel_animation.setStartValue(0)
            self.panel_animation.setEndValue(target_width)
            self.panel_animation.start()
            
        else:
            # --- COLLAPSE ---
            self.is_panel_collapsed = True
            # --- MODIFIED: Show real icon panel ---
            self.icon_stack.setCurrentIndex(1)
            
            self.setProperty("collapsed", True)
            self.settings_button_icon.setProperty("collapsed", True)
            self.toggle_sidebar_button.setProperty("collapsed", True)
            self._style_sidebar_widgets()

            target_width = 0
            self.panel_animation.setStartValue(self.nav_panel_container.width())
            self.panel_animation.setEndValue(target_width)
            self.panel_animation.start()
            
    def _style_sidebar_widgets(self):
        # --- MODIFIED: Updated widget list ---
        widgets = [
            self, self.icon_bar_container, self.nav_panel_container,
            self.settings_button_icon, self.settings_button_text,
            self.toggle_sidebar_button, self.sidebar_icons
        ]
        widgets.extend(self.icon_buttons.values())
        
        for w in widgets:
            w.style().unpolish(w)
            w.style().polish(w)

    def _on_panel_animation_finished(self):
        # (Unchanged)
        if self.is_panel_collapsed:
            self.nav_panel_container.setVisible(False)
            self.nav_panel_container.setMaximumWidth(0)
        else:
            self.nav_panel_container.setMaximumWidth(self.SIDEBAR_WIDTH_EXPANDED)

    # --- MODIFIED: This method is no longer needed ---
    def _set_sidebar_text_visibility(self, visible: bool):
        pass # This logic is now handled by the two separate settings buttons
             # and the panel visibility.

    def _apply_stylesheet(self):
        # --- MODIFIED: Updated stylesheet ---
        self.setStyleSheet("""
            #IconBarContainer {
                background-color: #FFFFFF; 
                border-right: 1px solid #D1D5DB;
            }
            #NavPanelContainer {
                background-color: #FFFFFF; 
                border-right: 1px solid #D1D5DB;
            }
            #NavPanelHeaderSpacer {
                border-bottom: 1px solid #FFFFFF; /* Match bg */
            }
            
            #ToggleSidebarButton {
                border: none;
                border-radius: 4px;
                background-color: transparent;
                color: #4B5563;
            }
            #ToggleSidebarButton:hover {
                background-color: #F3F4F6;
            }
            #SidebarTree {
                background-color: #FFFFFF; 
                border: none;
                font-size: 14px;
                color: #374151;
            }
            #SidebarTree::item { 
                padding: 10px 18px; 
                border-radius: 4px; 
            }
            #SidebarTree::item:selected { background-color: #E5E7EB; color: #1F2937; }
            #SidebarTree::item:!selected:hover { background-color: #F9FAFB; }
            
            #SidebarIcons {
                background-color: #FFFFFF;
            }
            #SidebarIconButton {
                border: none;
                background-color: transparent;
                border-radius: 4px;
                padding: 5px;
                color: #4B5563;
            }
            #SidebarIconButton:hover {
                background-color: #F3F4F6;
            }
            #SidebarIconButton:checked {
                background-color: #E5E7EB;
            }
            
            /* --- NEW: Style for Icon-Only Settings Button --- */
            #SettingsButtonIcon {
                color: #4B5563;
                border-top: 1px solid #D1D5DB;
                background-color: #FFFFFF;
                border: none;
                border-top: 1px solid #D1D5DB;
                border-radius: 4px; /* Match other icon buttons */
                padding: 5px; /* Match other icon buttons */
            }
            #SettingsButtonIcon:hover {
                background-color: #F9FAFB;
            }

            /* --- NEW: Style for Text Settings Button --- */
            #SettingsButtonText {
                color: #374151;
                border-top: 1px solid #D1D5DB;
                background-color: #FFFFFF;
                font-size: 14px;
                padding: 10px 18px;
                text-align: left;
                border: none;
                border-top: 1px solid #D1D5DB;
            }
            #SettingsButtonText:hover {
                background-color: #F9FAFB;
            }
        """)