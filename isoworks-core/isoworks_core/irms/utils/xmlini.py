"""
irms/utils/xmlini.py — XML and INI file parsing utilities for the irms subpackage.
Provides try_parse_xml() and try_parse_ini() for tolerantly reading instrument
metadata stored in XML or INI-style configuration files.
"""
# This file is part of your project and is intended to be distributed under the GPL-2 license.
# See the GNU General Public License version 2 for details.

from __future__ import annotations
from pathlib import Path
import xml.etree.ElementTree as ET
import configparser

def try_parse_xml(path_or_file):
    try:
        if hasattr(path_or_file, "read"):
            tree = ET.parse(path_or_file)
        else:
            tree = ET.parse(path_or_file)
        return tree.getroot()
    except Exception as e:

        logging.warning(f"Exception caught: {e}"); return None

def try_parse_ini(path_or_stream):
    cp = configparser.ConfigParser()
    try:
        if hasattr(path_or_stream, "read"):
            cp.read_file(path_or_stream)
        else:
            cp.read(path_or_stream, encoding="utf-8")
        return cp
    except Exception as e:

        logging.warning(f"Exception caught: {e}"); return None
