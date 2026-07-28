"""
dymo_utils.py — DYMO label printer integration for IsoWorks.
Provides DymoLabelPrinter, which drives DYMO Label Software via OLE/COM
(pywin32) to print sample labels from .label template files.
"""
import os
import logging

# Try to import win32com for OLE automation
try:
    import win32com.client
    HAS_COM = True
except ImportError:
    HAS_COM = False
    logging.warning("pywin32 not installed. Dymo printing will not work.")

class DymoLabelPrinter:
    """
    Python equivalent of mDymoLabelWriter.bas
    Interacts with DYMO Label Software v[8] via OLE/COM.
    """
    def __init__(self):
        self.addin = None
        self.labels = None
        self.is_connected = False
        
        if not HAS_COM:
            raise ImportError("Required library 'pywin32' is missing.")

    def connect(self):
        """Creates the OLE Objects (replacing CreateOLEObjects)"""
        try:
            # These ProgIDs match your VBA: "Dymo.DymoAddIn" and "Dymo.DymoLabels"
            self.addin = win32com.client.Dispatch("Dymo.DymoAddIn")
            self.labels = win32com.client.Dispatch("Dymo.DymoLabels")
            self.is_connected = True
            return True
        except Exception as e:
            logging.error(f"Failed to connect to Dymo OLE: {e}")
            self.is_connected = False
            return False

    def print_label(self, label_path, field_data, copies=1):
        """
        :param label_path: Absolute path to .label file
        :param field_data: Dictionary { "FieldName": "Value", "Barcode": "12345" }
        :param copies: Number of copies
        """
        if not self.is_connected:
            if not self.connect():
                return False

        if not os.path.exists(label_path):
            logging.error(f"Label file not found: {label_path}")
            return False

        try:
            # Open the label template
            # VBA: DymoAddIn.Open strPath
            self.addin.Open(label_path)

            # Set Fields
            # VBA: DymoLabels.SetField strField1, strText1
            for field, text in field_data.items():
                if text is not None:
                    # Ensure text is string
                    self.labels.SetField(str(field), str(text))

            # Print
            # VBA: DymoAddIn.Print(1, -1) -> 1=Copies, TRUE/FALSE for Wait
            self.addin.Print(copies, True) 
            return True
            
        except Exception as e:
            logging.error(f"Error printing label: {e}")
            return False

    def close(self):
        """Destroys references (replacing DestroyOLEObjects)"""
        self.addin = None
        self.labels = None
        self.is_connected = False